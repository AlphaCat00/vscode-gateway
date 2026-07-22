"""Connect to configured hosts and provide SSH transport operations."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import shlex
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

import asyncssh
import structlog

from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.host_trust import HostTrustService, known_host_token
from vscode_gateway.models import HostKeyRole, SessionId
from vscode_gateway.settings import Settings
from vscode_gateway.ssh_keys import SshKeyService

logger = structlog.get_logger()
_MAX_PROXY_HOPS = 8
_CHAIN_CLOSE_TIMEOUT = 5.0


@dataclass
class CapturedHostKey:
    """Presented host key captured by ``_HostKeyCapturer`` before verification."""

    host: str
    addr: str
    port: int
    algorithm: str
    fingerprint: str
    public_key_text: str


class _HostKeyCapturer(asyncssh.SSHClient):
    """Capture untrusted host keys for explicit user confirmation."""

    def __init__(self) -> None:
        self.captured: dict[tuple[str, int], list[CapturedHostKey]] = {}

    def validate_host_public_key(
        self, host: str, addr: str, port: int, key: asyncssh.SSHKey
    ) -> bool:
        algorithm = key.get_algorithm()
        fingerprint = key.get_fingerprint("sha256")
        public_key_text = key.export_public_key("openssh").decode("utf-8").strip()
        captured = CapturedHostKey(
            host=host,
            addr=addr,
            port=port,
            algorithm=algorithm,
            fingerprint=fingerprint,
            public_key_text=public_key_text,
        )
        self.captured.setdefault((host, port), []).append(captured)
        logger.info(
            "ssh_host_key_presented",
            host=host,
            port=port,
            algorithm=algorithm,
            fingerprint=fingerprint,
        )
        return False


@dataclass(frozen=True)
class _ProxyJumpHop:
    """One validated endpoint from a ProxyJump value."""

    host: str
    user: str | None = None
    port: int | None = None


class _ProxyJumpConfigError(ValueError):
    """A ProxyJump value cannot be safely expanded."""


@dataclass(frozen=True)
class _ResolvedEndpoint:
    """An endpoint resolved from the gateway SSH config."""

    requested_host: str
    user: str
    port: int
    role: HostKeyRole
    options: asyncssh.SSHClientConnectionOptions


def _invalid_proxy_jump(message: str) -> _ProxyJumpConfigError:
    return _ProxyJumpConfigError(f"Invalid ProxyJump value: {message}")


def _parse_proxy_jump_port(value: str) -> int:
    if not value or not value.isascii() or not value.isdigit():
        raise _invalid_proxy_jump("port must be a decimal integer")
    port = int(value)
    if not 1 <= port <= 65535:
        raise _invalid_proxy_jump("port must be between 1 and 65535")
    return port


def _validate_proxy_jump_part(value: str, *, kind: str) -> str:
    if not value:
        raise _invalid_proxy_jump(f"{kind} must not be empty")
    if any(char.isspace() or ord(char) < 0x20 for char in value):
        raise _invalid_proxy_jump(f"{kind} must not contain whitespace")
    invalid_delimiters = "," if kind == "user" else ",@[]/"
    if any(char in value for char in invalid_delimiters):
        raise _invalid_proxy_jump(f"{kind} contains an invalid delimiter")
    return value


def _parse_proxy_jump_endpoint(value: str) -> _ProxyJumpHop:
    if not value or any(char.isspace() for char in value):
        raise _invalid_proxy_jump("whitespace is not allowed")

    user: str | None = None
    if "@" in value:
        user, value = value.rsplit("@", 1)
        _validate_proxy_jump_part(user, kind="user")

    if value.startswith("["):
        end = value.find("]")
        if end <= 1 or value.count("[") != 1 or value.count("]") != 1:
            raise _invalid_proxy_jump("bracketed IPv6 host is malformed")
        host = value[1:end]
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise _invalid_proxy_jump("bracketed host must be a valid IPv6 address") from exc
        suffix = value[end + 1 :]
        if suffix:
            if not suffix.startswith(":"):
                raise _invalid_proxy_jump("unexpected data after bracketed host")
            port = _parse_proxy_jump_port(suffix[1:])
        else:
            port = None
    else:
        if "[" in value or "]" in value:
            raise _invalid_proxy_jump("unmatched IPv6 bracket")
        colon_count = value.count(":")
        if colon_count > 1:
            raise _invalid_proxy_jump("IPv6 addresses must be bracketed")
        if colon_count == 1:
            host, port_text = value.rsplit(":", 1)
            port = _parse_proxy_jump_port(port_text)
        else:
            host = value
            port = None
        _validate_proxy_jump_part(host, kind="host")

    return _ProxyJumpHop(host=host, user=user, port=port)


def _parse_proxy_jump(value: object) -> tuple[_ProxyJumpHop, ...]:
    """Parse AsyncSSH's resolved ProxyJump value without native expansion."""
    if value is None:
        return ()
    if not isinstance(value, str):
        raise _invalid_proxy_jump("value must be a string or none")
    if value.casefold() == "none":
        return ()
    if not value:
        raise _invalid_proxy_jump("value must not be empty")
    if any(char.isspace() for char in value):
        raise _invalid_proxy_jump("whitespace is not allowed")

    parts = value.split(",")
    if any(not part for part in parts):
        raise _invalid_proxy_jump("empty endpoint")
    if any(part.casefold() == "none" for part in parts):
        raise _invalid_proxy_jump("none must be the only endpoint")
    return tuple(_parse_proxy_jump_endpoint(part) for part in parts)


def _allocate_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class SshConnection:
    """Owned AsyncSSH connection and optional forwarding listener."""

    conn: asyncssh.SSHClientConnection
    listener: asyncssh.SSHListener | None
    local_port: int
    remote_port: int
    alias: str
    capturer: _HostKeyCapturer
    connections: tuple[asyncssh.SSHClientConnection, ...] = ()

    def __post_init__(self) -> None:
        if not self.connections:
            self.connections = (self.conn,)
        elif self.connections[-1] is not self.conn:
            raise ValueError("SSH connection chain must end with the target connection")

    @property
    def chain(self) -> tuple[asyncssh.SSHClientConnection, ...]:
        """Return connections ordered from the first jump to the target."""
        return self.connections

    @property
    def tunnel_pid(self) -> int:
        return 0


class SshConnectionService:
    """Connect using uploaded keys and gateway-owned host trust."""

    def __init__(
        self,
        settings: Settings,
        key_service: SshKeyService,
        host_trust_service: HostTrustService,
    ) -> None:
        self._settings = settings
        self._key_service = key_service
        self._host_trust_service = host_trust_service

    async def connect_for_session(
        self,
        *,
        session_id: SessionId,
        alias: str,
    ) -> SshConnection:
        """Open a connection chain for ``alias``, capturing host-key challenges."""
        keys = self._key_service.load_present_keys()
        if not keys:
            raise GatewayError(
                ErrorCode.SSH_NO_UPLOADED_KEYS,
                "No SSH private keys have been uploaded; "
                "upload at least one key before opening a session",
                status_code=409,
            )

        deadline = asyncio.get_running_loop().time() + self._settings.ssh_connect_timeout
        connections: list[asyncssh.SSHClientConnection] = []
        last_capturer: _HostKeyCapturer | None = None
        try:
            async with asyncio.timeout_at(deadline):
                endpoints = await self._resolve_route(alias=alias, keys=keys)
                for endpoint in endpoints:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError

                    capturer = _HostKeyCapturer()
                    last_capturer = capturer
                    try:
                        conn = await asyncssh.connect(
                            endpoint.requested_host,
                            endpoint.port,
                            **self._connect_kwargs(
                                endpoint=endpoint,
                                keys=keys,
                                capturer=capturer,
                                tunnel=connections[-1] if connections else None,
                                connect_timeout=remaining,
                            ),
                        )
                    except asyncssh.HostKeyNotVerifiable as exc:
                        code = await self._record_host_key_challenge(
                            session_id=session_id,
                            capturer=capturer,
                            alias=alias,
                            role=endpoint.role,
                        )
                        raise GatewayError(
                            code,
                            f"SSH host key verification failed for alias {alias!r}",
                            status_code=502,
                        ) from exc

                    connections.append(conn)
        except BaseException as exc:
            await self._close_connection_chain(connections)
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, GatewayError):
                raise
            if isinstance(exc, asyncssh.KeyImportError):
                raise GatewayError(
                    ErrorCode.SSH_KEY_INVALID,
                    f"An uploaded key could not be loaded: {exc}",
                    status_code=500,
                ) from exc
            if isinstance(exc, asyncssh.PermissionDenied):
                message = str(exc)
                if "publickey" in message.lower() or (
                    "no supported authentication" in message.lower()
                ):
                    raise GatewayError(
                        ErrorCode.SSH_NO_UPLOADED_KEY_ACCEPTED,
                        "None of the uploaded SSH keys was accepted by this host",
                        status_code=502,
                    ) from exc
                raise GatewayError(
                    ErrorCode.SSH_UNREACHABLE,
                    "SSH authentication failed",
                    status_code=502,
                ) from exc
            if isinstance(exc, _ProxyJumpConfigError):
                raise GatewayError(
                    ErrorCode.SSH_CONFIG_INVALID,
                    str(exc),
                    status_code=502,
                ) from exc
            if isinstance(exc, ValueError):
                raise GatewayError(
                    ErrorCode.SSH_CONFIG_INVALID,
                    f"SSH configuration is invalid: {exc}",
                    status_code=502,
                ) from exc
            if isinstance(exc, asyncssh.ChannelOpenError):
                raise GatewayError(
                    ErrorCode.SSH_UNREACHABLE,
                    f"SSH channel could not be opened: {exc}",
                    status_code=502,
                ) from exc
            if isinstance(exc, (asyncssh.ConnectionLost, asyncssh.DisconnectError)):
                raise GatewayError(
                    ErrorCode.SSH_UNREACHABLE,
                    f"SSH connection lost: {exc}",
                    status_code=502,
                ) from exc
            if isinstance(exc, TimeoutError):
                raise GatewayError(
                    ErrorCode.SSH_UNREACHABLE,
                    f"SSH connection timed out after {self._settings.ssh_connect_timeout}s",
                    status_code=502,
                ) from exc
            if isinstance(exc, OSError):
                raise GatewayError(
                    ErrorCode.SSH_UNREACHABLE,
                    f"SSH connection failed: {exc}",
                    status_code=502,
                ) from exc
            raise

        assert connections
        assert last_capturer is not None
        return SshConnection(
            conn=connections[-1],
            listener=None,
            local_port=0,
            remote_port=0,
            alias=alias,
            capturer=last_capturer,
            connections=tuple(connections),
        )

    def _policy_kwargs(self, keys: list[asyncssh.SSHKey]) -> dict[str, object]:
        """Return explicit gateway policy options for one SSH endpoint."""
        return {
            "client_keys": keys,
            "client_certs": [],
            "known_hosts": str(self._settings.ssh_known_hosts_path),
            "agent_path": None,
            "agent_identities": [],
            "agent_forwarding": False,
            "password": None,
            "password_auth": False,
            "kbdint_auth": False,
            "host_based_auth": False,
            "client_host_keysign": False,
            "client_host_keys": [],
            "client_host_certs": [],
            "gss_host": None,
            "gss_auth": False,
            "gss_kex": False,
            "gss_delegate_creds": False,
            "public_key_auth": True,
            "preferred_auth": ["publickey"],
            "disable_trivial_auth": True,
            "pkcs11_provider": None,
            "pkcs11_pin": None,
            "x509_trusted_certs": None,
            "x509_trusted_cert_paths": [],
            "proxy_command": None,
        }

    async def _resolve_route(
        self,
        *,
        alias: str,
        keys: list[asyncssh.SSHKey],
    ) -> list[_ResolvedEndpoint]:
        endpoints: list[_ResolvedEndpoint] = []
        jump_count = 0
        seen_jumps: set[str] = set()

        async def visit(
            host: str,
            user: str | None,
            port: int | None,
            role: HostKeyRole,
            path: tuple[str, ...],
        ) -> None:
            nonlocal jump_count
            if host in path:
                cycle = " -> ".join((*path, host))
                raise _invalid_proxy_jump(f"cycle detected ({cycle})")
            if role == "jump":
                if host in seen_jumps:
                    raise _invalid_proxy_jump(f"repeated jump host {host!r}")
                if jump_count >= _MAX_PROXY_HOPS:
                    raise _invalid_proxy_jump(f"route exceeds the {_MAX_PROXY_HOPS}-hop maximum")
                seen_jumps.add(host)
                jump_count += 1

            policy = self._policy_kwargs(keys)
            policy.update(
                {
                    "config": [str(self._settings.ssh_config_path)],
                    "host": host,
                    "port": port if port is not None else (),
                    "username": user if user is not None else (),
                    "connect_timeout": self._settings.ssh_connect_timeout,
                }
            )
            options = await self._construct_options(**policy)
            for nested in _parse_proxy_jump(options.tunnel):
                await visit(
                    nested.host,
                    nested.user,
                    nested.port,
                    "jump",
                    (*path, host),
                )

            endpoints.append(
                _ResolvedEndpoint(
                    requested_host=host,
                    user=options.username,
                    port=options.port,
                    role=role,
                    options=options,
                )
            )

        await visit(alias, None, None, "target", ())
        return endpoints

    @staticmethod
    async def _construct_options(
        **kwargs: object,
    ) -> asyncssh.SSHClientConnectionOptions:
        options_class = cast(Any, asyncssh.SSHClientConnectionOptions)
        construct = cast(
            Callable[..., Awaitable[asyncssh.SSHClientConnectionOptions]],
            options_class.construct,
        )
        return await construct(**kwargs)

    def _connect_kwargs(
        self,
        *,
        endpoint: _ResolvedEndpoint,
        keys: list[asyncssh.SSHKey],
        capturer: _HostKeyCapturer,
        tunnel: asyncssh.SSHClientConnection | None,
        connect_timeout: float,
    ) -> dict[str, object]:
        kwargs = self._policy_kwargs(keys)
        kwargs.update(
            {
                "config": [str(self._settings.ssh_config_path)],
                "options": endpoint.options,
                "tunnel": tunnel,
                "client_factory": lambda: capturer,
                "username": endpoint.user,
                "connect_timeout": connect_timeout,
            }
        )
        return kwargs

    async def _close_connection_chain(
        self,
        connections: list[asyncssh.SSHClientConnection],
    ) -> None:
        """Close all already-open chain connections, target first."""
        for conn in reversed(connections):
            with contextlib.suppress(Exception):
                conn.close()
            try:
                await asyncio.wait_for(conn.wait_closed(), timeout=_CHAIN_CLOSE_TIMEOUT)
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def forward_local_port(
        self,
        ssh_conn: SshConnection,
        remote_port: int,
    ) -> tuple[asyncssh.SSHListener, int]:
        """Open a loopback forward from a random local port to ``remote_port``."""
        local_port = _allocate_local_port()
        try:
            listener = await ssh_conn.conn.forward_local_port(
                "127.0.0.1",
                local_port,
                "127.0.0.1",
                remote_port,
            )
        except asyncssh.ChannelListenError as exc:
            raise GatewayError(
                ErrorCode.TUNNEL_START_FAILED,
                f"Failed to start SSH forwarding: {exc}",
                status_code=502,
            ) from exc
        ssh_conn.listener = listener
        ssh_conn.local_port = local_port
        ssh_conn.remote_port = remote_port
        return listener, local_port

    async def _record_host_key_challenge(
        self,
        *,
        session_id: SessionId,
        capturer: _HostKeyCapturer,
        alias: str,
        role: HostKeyRole,
    ) -> ErrorCode:
        failing = self._failing_hop(capturer)
        if failing is None:
            return ErrorCode.SSH_UNREACHABLE

        host, port, algorithm, fingerprint, public_key_text = failing
        is_changed = self._is_known_host(host, port)
        code = ErrorCode.SSH_HOST_CHANGED if is_changed else ErrorCode.SSH_HOST_UNKNOWN
        await self._host_trust_service.record_challenge(
            session_id=session_id,
            role=role,
            alias=alias,
            host=host,
            port=port,
            algorithm=algorithm,
            fingerprint=fingerprint,
            public_key=public_key_text,
        )
        logger.info(
            "ssh_host_key_challenge_recorded",
            session_id=str(session_id),
            alias=alias,
            host=host,
            port=port,
            role=role,
            is_changed=is_changed,
            code=code.value,
        )
        return code

    def _failing_hop(self, capturer: _HostKeyCapturer) -> tuple[str, int, str, str, str] | None:
        if not capturer.captured:
            return None
        (host, port), entries = list(capturer.captured.items())[-1]
        latest = entries[-1]
        return host, port, latest.algorithm, latest.fingerprint, latest.public_key_text

    def _is_known_host(self, host: str, port: int) -> bool:
        """Return True if ``known_hosts`` already records any entry for this host/port.

        Used to distinguish ``ssh_host_unknown`` from ``ssh_host_changed``.
        """
        path = self._settings.ssh_known_hosts_path
        if not path.exists():
            return False
        host_token = known_host_token(host, port)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return False
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) < 3:
                continue
            host_field = parts[0]
            host_candidates = host_field.split(",")
            if host_token in host_candidates:
                return True
        return False

    @staticmethod
    async def run_command(
        conn: asyncssh.SSHClientConnection,
        argv: list[str],
        *,
        timeout: float,
        stdin: bytes | None = None,
    ) -> asyncssh.SSHCompletedProcess:
        if not argv:
            raise ValueError("remote command argv must not be empty")
        if any("\x00" in item for item in argv):
            raise ValueError("remote command argv must not contain NUL bytes")

        command = shlex.join(argv)
        if stdin is None:
            return await conn.run(command, check=False, timeout=timeout, encoding=None)
        return await conn.run(
            command,
            check=False,
            timeout=timeout,
            input=stdin,
            encoding=None,
        )

    @staticmethod
    async def sftp_put(
        conn: asyncssh.SSHClientConnection,
        local_path: str,
        remote_path: str,
    ) -> None:
        async with conn.start_sftp_client() as sftp:
            await sftp.put(local_path, remote_path)


__all__ = ["SshConnection", "SshConnectionService"]

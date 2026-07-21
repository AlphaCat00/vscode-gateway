"""Connect to configured hosts and provide SSH transport operations."""

from __future__ import annotations

import shlex
import socket
from dataclasses import dataclass

import asyncssh
import structlog

from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.host_trust import HostTrustService, known_host_token
from vscode_gateway.models import HostKeyRole, SessionId
from vscode_gateway.settings import Settings
from vscode_gateway.ssh_keys import SshKeyService

logger = structlog.get_logger()


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
        role: HostKeyRole = "target",
    ) -> SshConnection:
        """Open a connection for ``alias``, capturing host-key challenges."""
        keys = self._key_service.load_present_keys()
        if not keys:
            raise GatewayError(
                ErrorCode.SSH_NO_UPLOADED_KEYS,
                "No SSH private keys have been uploaded; "
                "upload at least one key before opening a session",
                status_code=409,
            )

        capturer = _HostKeyCapturer()
        try:
            conn = await asyncssh.connect(
                alias,
                config=[str(self._settings.ssh_config_path)],
                client_factory=lambda: capturer,
                client_keys=keys,
                known_hosts=str(self._settings.ssh_known_hosts_path),
                agent_path=None,
                password=None,
                password_auth=False,
                kbdint_auth=False,
                host_based_auth=False,
                gss_auth=False,
                gss_kex=False,
                connect_timeout=self._settings.ssh_connect_timeout,
            )
        except asyncssh.HostKeyNotVerifiable as exc:
            code = await self._record_host_key_challenge(
                session_id=session_id,
                capturer=capturer,
                alias=alias,
                role=role,
            )
            raise GatewayError(
                code,
                f"SSH host key verification failed for alias {alias!r}",
                status_code=502,
            ) from exc
        except asyncssh.KeyImportError as exc:
            raise GatewayError(
                ErrorCode.SSH_KEY_INVALID,
                f"An uploaded key could not be loaded: {exc}",
                status_code=500,
            ) from exc
        except asyncssh.PermissionDenied as exc:
            message = str(exc)
            if "publickey" in message.lower() or "no supported authentication" in message.lower():
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
        except asyncssh.ChannelOpenError as exc:
            raise GatewayError(
                ErrorCode.SSH_UNREACHABLE,
                f"SSH channel could not be opened: {exc}",
                status_code=502,
            ) from exc
        except (asyncssh.ConnectionLost, asyncssh.DisconnectError) as exc:
            raise GatewayError(
                ErrorCode.SSH_UNREACHABLE,
                f"SSH connection lost: {exc}",
                status_code=502,
            ) from exc
        except TimeoutError as exc:
            raise GatewayError(
                ErrorCode.SSH_UNREACHABLE,
                f"SSH connection timed out after {self._settings.ssh_connect_timeout}s",
                status_code=502,
            ) from exc
        except OSError as exc:
            raise GatewayError(
                ErrorCode.SSH_UNREACHABLE,
                f"SSH connection failed: {exc}",
                status_code=502,
            ) from exc

        return SshConnection(
            conn=conn,
            listener=None,
            local_port=0,
            remote_port=0,
            alias=alias,
            capturer=capturer,
        )

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

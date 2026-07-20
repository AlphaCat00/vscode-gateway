from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import socket
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from vscode_gateway.models import CatalogSnapshot, ProcessResult, TunnelIdentity
from vscode_gateway.settings import Settings

ALIAS_RE = re.compile(r"^\s*Host\s+(.+)$", re.IGNORECASE | re.MULTILINE)
ALIAS_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9._\-]{0,252}")
ALIAS_MAX_LENGTH = 253
WILDCARD_CHARS = frozenset("*?[")

# Directives that turn the dedicated gateway config into a command-execution or
# forwarding tool. They are rejected before any candidate is committed, in
# addition to syntax validation by ``ssh -G``.
UNSAFE_DIRECTIVES: frozenset[str] = frozenset(
    {
        "include",
        "match",
        "proxycommand",
        "localcommand",
        "permitlocalcommand",
        "remotecommand",
        "localforward",
        "remoteforward",
        "dynamicforward",
        "tunnel",
        "canonicalizehostname",
        "knownhostscommand",
        "pkcs11provider",
        "securitykeyprovider",
    }
)

# Process-wide serial lock for config writes. Asyncio locks are safe to
# construct without a running loop on Python 3.10+.
_CONFIG_WRITE_LOCK = asyncio.Lock()

_DIRECTIVE_LINE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9-]*)\s")


async def run_process(
    argv: list[str],
    *,
    timeout: float,
    stdin: bytes | None = None,
    max_stdout: int = 1_000_000,
    max_stderr: int = 1_000_000,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        argv[0],
        *argv[1:],
        stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=stdin),
            timeout=timeout,
        )
    except TimeoutError:
        try:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        except ProcessLookupError:
            pass
        duration = time.monotonic() - start
        return ProcessResult(
            exit_code=-1,
            stdout=b"",
            stderr=b"subprocess timed out",
            duration=duration,
            timed_out=True,
        )

    duration = time.monotonic() - start
    return ProcessResult(
        exit_code=proc.returncode or 0,
        stdout=stdout[:max_stdout] if stdout else b"",
        stderr=stderr[:max_stderr] if stderr else b"",
        duration=duration,
        timed_out=False,
    )


def discover_aliases(config_text: str) -> list[str]:
    seen: set[str] = set()
    aliases: list[str] = []
    for line in config_text.splitlines():
        m = ALIAS_RE.match(line)
        if m is None:
            continue
        tokens = m.group(1).strip().split()
        for token in tokens:
            token = token.strip()
            if not token or token in seen:
                continue
            if token == "*":
                continue
            if token.startswith("!"):
                continue
            if any(c in token for c in WILDCARD_CHARS):
                continue
            try:
                token.encode("utf-8")
            except UnicodeEncodeError:
                continue
            if len(token) > ALIAS_MAX_LENGTH:
                continue
            seen.add(token)
            aliases.append(token)
    return aliases


async def validate_alias(settings: Settings, alias: str, config_path: Path | None = None) -> bool:
    candidate = config_path if config_path is not None else settings.ssh_config_path
    result = await run_process(
        [settings.ssh_executable, "-F", str(candidate), "-G", alias],
        timeout=settings.subprocess_timeout,
    )
    return result.exit_code == 0


def find_unsafe_directives(text: str) -> list[str]:
    """Return the list of unsafe directives found in ``text`` (case-insensitive).

    Only directive keywords at the start of a line are inspected. Values
    (including ``Host`` tokens) are not interpreted as directives.
    """
    found: list[str] = []
    for line in text.splitlines():
        m = _DIRECTIVE_LINE_RE.match(line)
        if m is None:
            continue
        keyword = m.group(1).lower()
        if keyword in UNSAFE_DIRECTIVES:
            found.append(keyword)
    return found


async def probe_capabilities(
    settings: Settings,
    alias: str,
    helper_path: str,
    connect_timeout: int | None = None,
) -> ProcessResult:
    timeout_val = connect_timeout or settings.ssh_connect_timeout
    return await run_process(
        [
            settings.ssh_executable,
            "-F",
            str(settings.ssh_config_path),
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={int(timeout_val)}",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=2",
            "--",
            alias,
            "/bin/sh",
            helper_path,
            "capabilities",
        ],
        timeout=settings.subprocess_timeout,
    )


async def scp_transfer(
    settings: Settings,
    alias: str,
    local_path: Path,
    remote_path: str,
) -> ProcessResult:
    return await run_process(
        [
            settings.scp_executable,
            "-F",
            str(settings.ssh_config_path),
            "-B",
            str(local_path),
            f"{alias}:{remote_path}",
        ],
        timeout=settings.subprocess_timeout * 2,
    )


async def start_local_forward(
    settings: Settings,
    alias: str,
    remote_port: int,
) -> tuple[TunnelIdentity, asyncio.subprocess.Process]:
    local_port = _allocate_local_port()
    proc = await asyncio.create_subprocess_exec(
        settings.ssh_executable,
        "-F",
        str(settings.ssh_config_path),
        "-N",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=2",
        "-L",
        f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
        "--",
        alias,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.sleep(0.5)
    if proc.returncode is not None:
        stderr = b""
        if proc.stderr:
            with contextlib.suppress(TimeoutError):
                stderr = await asyncio.wait_for(proc.stderr.read(), timeout=2.0)
        msg = f"SSH tunnel exited immediately with code {proc.returncode}: {stderr[:500]!r}"
        from vscode_gateway.errors import ErrorCode, GatewayError

        raise GatewayError(ErrorCode.TUNNEL_START_FAILED, msg, status_code=502)

    identity = TunnelIdentity(local_port=local_port, pid=proc.pid or 0)
    return identity, proc


def _allocate_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def compute_config_revision(config_text: str) -> str:
    return f"sha256:{hashlib.sha256(config_text.encode('utf-8')).hexdigest()}"


class SshCatalog:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._snapshot: CatalogSnapshot | None = None
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> CatalogSnapshot | None:
        return self._snapshot

    def set_snapshot(self, snapshot: CatalogSnapshot) -> None:
        self._snapshot = snapshot

    async def refresh(self) -> CatalogSnapshot:
        async with self._lock:
            snapshot = await self._load_catalog()
            self._snapshot = snapshot
            return snapshot

    async def _load_catalog(self) -> CatalogSnapshot:
        config_path = self._settings.ssh_config_path
        try:
            text = config_path.read_text(encoding="utf-8")
        except OSError as exc:
            revision = compute_config_revision("")
            return CatalogSnapshot(
                revision=revision,
                aliases=(),
                loaded_at=datetime.now(UTC),
                error=str(exc),
            )

        revision = compute_config_revision(text)
        candidates = discover_aliases(text)

        valid: list[str] = []
        for alias in candidates:
            try:
                ok = await validate_alias(self._settings, alias)
            except Exception:
                continue
            if ok:
                valid.append(alias)

        return CatalogSnapshot(
            revision=revision,
            aliases=tuple(sorted(valid)),
            loaded_at=datetime.now(UTC),
        )

    def is_valid_alias(self, alias: str) -> bool:
        if self._snapshot is None:
            return False
        return alias in self._snapshot.aliases


async def validate_and_save_config(
    settings: Settings,
    text: str,
    expected_revision: str | None = None,
) -> CatalogSnapshot:
    from vscode_gateway.errors import ErrorCode, GatewayError

    config_path = settings.ssh_config_path

    if "\x00" in text:
        raise GatewayError(
            ErrorCode.SSH_CONFIG_INVALID, "Config contains NUL bytes", status_code=400
        )

    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise GatewayError(
            ErrorCode.SSH_CONFIG_INVALID, "Config is not valid UTF-8", status_code=400
        ) from exc

    if len(encoded) > 1_000_000:
        raise GatewayError(
            ErrorCode.SSH_CONFIG_INVALID, "Config exceeds size limit", status_code=400
        )

    unsafe = find_unsafe_directives(text)
    if unsafe:
        raise GatewayError(
            ErrorCode.SSH_CONFIG_INVALID,
            f"Config contains prohibited directives: {sorted(set(unsafe))}",
            status_code=400,
        )

    aliases = discover_aliases(text)
    if not aliases:
        # An empty candidate with zero aliases is allowed; ``ssh -G`` would
        # otherwise reject nothing meaningful. Publish an empty catalog.
        pass

    # Serialize all writes through one process-wide lock so concurrent saves
    # cannot race on the same target file or its revision.
    async with _CONFIG_WRITE_LOCK:
        if expected_revision is not None and config_path.exists():
            current_text = config_path.read_text(encoding="utf-8")
            current_revision = compute_config_revision(current_text)
            if expected_revision != current_revision:
                raise GatewayError(
                    ErrorCode.CONFLICT,
                    "Config was modified; refresh and retry",
                    status_code=409,
                )

        # Reject line/alias-count limits before invoking external tools.
        lines = text.splitlines()
        if len(lines) > 10_000:
            raise GatewayError(
                ErrorCode.SSH_CONFIG_INVALID, "Config exceeds line limit", status_code=400
            )
        if len(aliases) > 1000:
            raise GatewayError(
                ErrorCode.SSH_CONFIG_INVALID, "Config exceeds alias count limit", status_code=400
            )

        # Create a unique temporary file in the target directory using exclusive
        # creation so concurrent saves never share a path. Mode 0600.
        parent = config_path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise GatewayError(
                ErrorCode.CONFIG_EDIT_FAILED,
                f"Cannot access config directory: {exc}",
                status_code=500,
            ) from exc

        fd, tmp_name = tempfile.mkstemp(prefix=".cfg.", suffix=".tmp", dir=str(parent))
        tmp_path = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(encoded)
                f.flush()
                os.fsync(f.fileno())

            # Validate every candidate alias using the candidate path, not the
            # live file. Reject on the first failure.
            for alias in aliases:
                try:
                    ok = await validate_alias(settings, alias, config_path=tmp_path)
                except Exception as exc:
                    raise GatewayError(
                        ErrorCode.SSH_CONFIG_INVALID,
                        f"Failed to validate alias '{alias}': {exc}",
                        status_code=400,
                    ) from exc
                if not ok:
                    raise GatewayError(
                        ErrorCode.SSH_CONFIG_INVALID,
                        f"Alias '{alias}' failed validation",
                        status_code=400,
                    )

            os.replace(tmp_path, config_path)
            tmp_path = None  # ownership transferred; do not clean up below

            # fsync the parent directory so the rename is durable on disk.
            dir_fd = os.open(str(parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            # Cleanup on any failure path (validation, OS error, cancellation).
            if tmp_path is not None and tmp_path.exists():
                with contextlib.suppress(OSError):
                    tmp_path.unlink()
            raise

    # Re-publish the catalog from the committed file so the published snapshot
    # does not describe candidate-only state. Operate outside the write lock
    # to keep the lock critical section minimal.
    try:
        committed_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GatewayError(
            ErrorCode.CONFIG_EDIT_FAILED,
            f"Failed to re-read committed config: {exc}",
            status_code=500,
        ) from exc

    committed_aliases = discover_aliases(committed_text)
    catalog = CatalogSnapshot(
        revision=compute_config_revision(committed_text),
        aliases=tuple(sorted(committed_aliases)),
        loaded_at=datetime.now(UTC),
    )
    return catalog

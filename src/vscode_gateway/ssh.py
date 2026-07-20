from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import socket
import time
from datetime import UTC, datetime
from pathlib import Path

from vscode_gateway.models import CatalogSnapshot, ProcessResult, TunnelIdentity
from vscode_gateway.settings import Settings

ALIAS_RE = re.compile(r"^\s*Host\s+(.+)$", re.IGNORECASE | re.MULTILINE)
ALIAS_TOKEN_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9._\-]{0,252}")
ALIAS_MAX_LENGTH = 253
WILDCARD_CHARS = frozenset("*?[")


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


async def validate_alias(settings: Settings, alias: str) -> bool:
    result = await run_process(
        [settings.ssh_executable, "-F", str(settings.ssh_config_path), "-G", alias],
        timeout=settings.subprocess_timeout,
    )
    return result.exit_code == 0


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
    config_path = settings.ssh_config_path

    if "\x00" in text:
        from vscode_gateway.errors import ErrorCode, GatewayError

        raise GatewayError(
            ErrorCode.SSH_CONFIG_INVALID, "Config contains NUL bytes", status_code=400
        )

    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        from vscode_gateway.errors import ErrorCode, GatewayError

        raise GatewayError(
            ErrorCode.SSH_CONFIG_INVALID, "Config is not valid UTF-8", status_code=400
        ) from exc

    if len(text.encode("utf-8")) > 1_000_000:
        from vscode_gateway.errors import ErrorCode, GatewayError

        raise GatewayError(
            ErrorCode.SSH_CONFIG_INVALID, "Config exceeds size limit", status_code=400
        )

    if expected_revision is not None and config_path.exists():
        current_text = config_path.read_text(encoding="utf-8")
        current_revision = compute_config_revision(current_text)
        if expected_revision != current_revision:
            from vscode_gateway.errors import ErrorCode, GatewayError

            raise GatewayError(
                ErrorCode.CONFLICT,
                "Config was modified; refresh and retry",
                status_code=409,
            )

    tmp_path = Path(str(config_path) + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.chmod(tmp_path, 0o600)

    catalog = CatalogSnapshot(
        revision=compute_config_revision(text),
        aliases=tuple(sorted(discover_aliases(text))),
        loaded_at=datetime.now(UTC),
    )

    for alias in catalog.aliases:
        try:
            ok = await validate_alias(settings, alias)
        except Exception:
            continue
        if not ok:
            pass

    with tmp_path.open("r+") as f:
        os.fsync(f.fileno())
    os.replace(tmp_path, config_path)
    dir_fd = os.open(str(config_path.parent), os.O_RDONLY)
    os.fsync(dir_fd)
    os.close(dir_fd)

    return catalog

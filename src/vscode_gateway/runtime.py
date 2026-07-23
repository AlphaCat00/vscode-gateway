"""Manage the remote OpenVSCode runtime over an AsyncSSH connection."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, cast

import asyncssh
import httpx

from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.models import RuntimeCapabilities, RuntimeIdentity
from vscode_gateway.settings import Settings
from vscode_gateway.ssh_connection import SshConnectionService

HELPER_PATH = "/tmp/gateway-helper-v1.sh"


def _helper_path() -> str:
    src = Path(__file__).parent / "remote" / "gateway-helper-v1.sh"
    return str(src.resolve())


def _parse_json_dict(
    raw: bytes | str | None, *, default: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Parse JSON bytes into a dict, returning ``default`` (or {}) on missing/invalid input."""
    fallback = default if default is not None else {}
    if raw is None:
        return fallback
    if isinstance(raw, bytes):
        if not raw:
            return fallback
        try:
            text = raw.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            return fallback
    else:
        text = raw
        if not text:
            return fallback
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return fallback
    if isinstance(parsed, dict):
        return cast(dict[str, Any], parsed)
    return fallback


def _check_run_result(result: asyncssh.SSHCompletedProcess, *, code: ErrorCode, what: str) -> None:
    if result.exit_status is None or result.exit_status != 0:
        stderr = (result.stderr or b"")[:500]
        raise GatewayError(
            code,
            f"{what}: {stderr!r}",
            status_code=502,
        )


async def ensure_helper_installed(settings: Settings, conn: asyncssh.SSHClientConnection) -> None:
    local = _helper_path()
    try:
        await SshConnectionService.sftp_put(conn, local, HELPER_PATH)
    except asyncssh.SFTPError as exc:
        raise GatewayError(
            ErrorCode.SSH_UNREACHABLE,
            f"Failed to upload helper: {exc}",
            status_code=502,
        ) from exc

    result = await SshConnectionService.run_command(
        conn,
        ["chmod", "+x", HELPER_PATH],
        timeout=settings.subprocess_timeout,
    )
    _check_run_result(result, code=ErrorCode.SSH_UNREACHABLE, what="Failed to chmod helper")


async def get_capabilities(
    settings: Settings, conn: asyncssh.SSHClientConnection
) -> RuntimeCapabilities:
    await ensure_helper_installed(settings, conn)
    result = await SshConnectionService.run_command(
        conn,
        ["/bin/sh", HELPER_PATH, "capabilities"],
        timeout=settings.subprocess_timeout,
    )
    _check_run_result(result, code=ErrorCode.SSH_UNREACHABLE, what="SSH probe failed")
    data = _parse_json_dict(result.stdout)

    if not data.get("available"):
        raise GatewayError(
            ErrorCode.REMOTE_UNSUPPORTED,
            str(data.get("reason", "Remote host is not supported")),
            status_code=502,
        )

    return RuntimeCapabilities(
        platform=str(data.get("platform", "linux")),
        arch=str(data.get("arch", "unknown")),
        helper_version=str(data.get("helper_version", "unknown")),
        available=True,
    )


async def ensure_installed(
    settings: Settings,
    conn: asyncssh.SSHClientConnection,
    platform: str,
) -> None:
    arch = (await get_capabilities(settings, conn)).arch if platform == "linux" else "unknown"

    if arch == "aarch64":
        url = settings.openvscode_linux_arm64_url
        sha256 = settings.openvscode_linux_arm64_sha256
    else:
        url = settings.openvscode_linux_x64_url
        sha256 = settings.openvscode_linux_x64_sha256

    if not url or not sha256:
        raise GatewayError(
            ErrorCode.RUNTIME_INSTALL_FAILED,
            f"No OpenVSCode artifact configured for arch {arch}",
            status_code=500,
        )

    result = await SshConnectionService.run_command(
        conn,
        ["/bin/sh", HELPER_PATH, "runtime-inspect", sha256, settings.openvscode_version],
        timeout=settings.subprocess_timeout,
    )
    _check_run_result(result, code=ErrorCode.REMOTE_UNSUPPORTED, what="runtime-inspect failed")

    data = _parse_json_dict(result.stdout)
    if data.get("installed"):
        return

    local_archive = await _download_and_verify(settings, url, sha256)

    remote_tmp = f"/tmp/ovs-{uuid.uuid4().hex[:12]}.tar.gz"
    try:
        await SshConnectionService.sftp_put(conn, str(local_archive), remote_tmp)
    except asyncssh.SFTPError as exc:
        raise GatewayError(
            ErrorCode.RUNTIME_INSTALL_FAILED,
            f"Failed to upload runtime: {exc}",
            status_code=502,
        ) from exc

    result = await SshConnectionService.run_command(
        conn,
        [
            "/bin/sh",
            HELPER_PATH,
            "runtime-install",
            remote_tmp,
            sha256,
            settings.openvscode_version,
        ],
        timeout=settings.subprocess_timeout * 2,
    )
    if result.exit_status is None or result.exit_status != 0:
        raise GatewayError(
            ErrorCode.RUNTIME_INSTALL_FAILED,
            f"Remote install failed: {(result.stderr or b'')[:500]!r}",
            status_code=502,
        )
    data = _parse_json_dict(result.stdout)
    if not data.get("installed"):
        raise GatewayError(
            ErrorCode.RUNTIME_INSTALL_FAILED,
            "Remote install did not confirm installation",
            status_code=502,
        )


async def _download_and_verify(settings: Settings, url: str, expected_sha256: str) -> Path:
    cache_dir = settings.runtime_dir / "artifacts"
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive_path = cache_dir / f"{expected_sha256}.tar.gz"

    if archive_path.exists():
        computed = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        if computed == expected_sha256:
            return archive_path

    tmp_path = archive_path.with_suffix(".tmp.tar.gz")
    hasher = hashlib.sha256()

    async with (
        httpx.AsyncClient() as client,
        client.stream("GET", url, follow_redirects=True) as response,
    ):
        response.raise_for_status()
        with open(tmp_path, "wb") as f:
            async for chunk in response.aiter_bytes(chunk_size=65536):
                hasher.update(chunk)
                f.write(chunk)

    computed = hasher.hexdigest()
    if computed != expected_sha256:
        tmp_path.unlink(missing_ok=True)
        raise GatewayError(
            ErrorCode.RUNTIME_DIGEST_MISMATCH,
            f"Expected {expected_sha256}, got {computed}",
            status_code=502,
        )

    tmp_path.rename(archive_path)
    return archive_path


async def start_session(
    settings: Settings,
    conn: asyncssh.SSHClientConnection,
    session_id: uuid.UUID,
    alias: str,
) -> RuntimeIdentity:
    profile_id = hashlib.sha256(alias.encode("utf-8")).hexdigest()
    result = await SshConnectionService.run_command(
        conn,
        ["/bin/sh", HELPER_PATH, "session-start", str(session_id), profile_id],
        timeout=settings.startup_timeout,
    )
    if result.exit_status is None or result.exit_status != 0:
        raise GatewayError(
            ErrorCode.REMOTE_START_FAILED,
            f"Remote start failed: {(result.stderr or b'')[:500]!r}",
            status_code=502,
        )

    data = _parse_json_dict(result.stdout)
    if "error" in data:
        raise GatewayError(
            ErrorCode.REMOTE_START_FAILED,
            str(data.get("detail", data["error"])),
            status_code=502,
        )

    return RuntimeIdentity(
        pid=int(data["pid"]),
        port=int(data["port"]),
        boot_id=str(data["boot_id"]),
        process_start_id=str(data["process_start_id"]),
        executable=str(data["executable"]),
        session_dir=data.get("session_dir"),
    )


async def inspect_session(
    settings: Settings,
    conn: asyncssh.SSHClientConnection,
    session_id: uuid.UUID,
) -> dict[str, Any]:
    # Inspection is also used after reconnecting during retry and recovery,
    # when the helper may never have been uploaded or may have been lost from /tmp.
    await ensure_helper_installed(settings, conn)
    result = await SshConnectionService.run_command(
        conn,
        ["/bin/sh", HELPER_PATH, "session-inspect", str(session_id)],
        timeout=settings.subprocess_timeout,
    )
    _check_run_result(
        result,
        code=ErrorCode.RECOVERY_FAILED,
        what="Remote session inspection failed",
    )
    data = _parse_json_dict(result.stdout)
    if "error" in data:
        raise GatewayError(
            ErrorCode.RECOVERY_FAILED,
            str(data.get("detail", data["error"])),
            status_code=502,
        )
    if not isinstance(data.get("running"), bool):
        raise GatewayError(
            ErrorCode.RECOVERY_FAILED,
            "Remote session inspection returned an invalid response",
            status_code=502,
        )
    return data


async def stop_session(
    settings: Settings,
    conn: asyncssh.SSHClientConnection,
    session_id: uuid.UUID,
) -> bool:
    result = await SshConnectionService.run_command(
        conn,
        ["/bin/sh", HELPER_PATH, "session-stop", str(session_id)],
        timeout=settings.stop_timeout,
    )
    _check_run_result(result, code=ErrorCode.STOP_FAILED, what="Remote session stop failed")
    data = _parse_json_dict(result.stdout)
    if "error" in data:
        raise GatewayError(
            ErrorCode.STOP_FAILED,
            str(data.get("detail", data["error"])),
            status_code=502,
        )
    if data.get("stopped") is not True:
        raise GatewayError(
            ErrorCode.STOP_FAILED,
            "Remote session stop did not confirm process absence",
            status_code=502,
        )
    return True


async def remove_session(
    settings: Settings,
    conn: asyncssh.SSHClientConnection,
    session_id: uuid.UUID,
) -> bool:
    result = await SshConnectionService.run_command(
        conn,
        ["/bin/sh", HELPER_PATH, "session-remove", str(session_id)],
        timeout=settings.subprocess_timeout,
    )
    _check_run_result(result, code=ErrorCode.STOP_FAILED, what="Remote session removal failed")
    data = _parse_json_dict(result.stdout)
    if "error" in data:
        raise GatewayError(
            ErrorCode.STOP_FAILED,
            str(data.get("detail", data["error"])),
            status_code=502,
        )
    if data.get("removed") is not True:
        raise GatewayError(
            ErrorCode.STOP_FAILED,
            "Remote session removal was not confirmed",
            status_code=502,
        )
    return True


async def list_sessions(
    settings: Settings,
    conn: asyncssh.SSHClientConnection,
) -> list[str]:
    result = await SshConnectionService.run_command(
        conn,
        ["/bin/sh", HELPER_PATH, "session-list"],
        timeout=settings.subprocess_timeout,
    )
    _check_run_result(result, code=ErrorCode.RECOVERY_FAILED, what="Remote session list failed")
    data = _parse_json_dict(result.stdout)
    sessions = data.get("sessions", [])
    if isinstance(sessions, list):
        return [str(s) for s in cast(list[Any], sessions)]
    return []


class RuntimeService:
    """Orchestrates the remote helper script via a passed-in AsyncSSH connection."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def capabilities(self, conn: asyncssh.SSHClientConnection) -> RuntimeCapabilities:
        return await get_capabilities(self.settings, conn)

    async def ensure_installed(self, conn: asyncssh.SSHClientConnection, platform: str) -> None:
        await ensure_installed(self.settings, conn, platform)

    async def start_session(
        self,
        conn: asyncssh.SSHClientConnection,
        session_id: uuid.UUID,
        alias: str,
    ) -> RuntimeIdentity:
        return await start_session(self.settings, conn, session_id, alias)

    async def inspect_session(
        self, conn: asyncssh.SSHClientConnection, session_id: uuid.UUID
    ) -> dict[str, Any]:
        return await inspect_session(self.settings, conn, session_id)

    async def stop_session(self, conn: asyncssh.SSHClientConnection, session_id: uuid.UUID) -> bool:
        return await stop_session(self.settings, conn, session_id)

    async def remove_session(
        self, conn: asyncssh.SSHClientConnection, session_id: uuid.UUID
    ) -> bool:
        return await remove_session(self.settings, conn, session_id)

    async def list_sessions(self, conn: asyncssh.SSHClientConnection) -> list[str]:
        return await list_sessions(self.settings, conn)


__all__ = ["RuntimeService"]

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, cast

import httpx

from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.models import RuntimeCapabilities, RuntimeIdentity
from vscode_gateway.settings import Settings
from vscode_gateway.ssh import (
    probe_capabilities,
    run_process,
    scp_transfer,
)

HELPER_PATH = "/tmp/gateway-helper-v1.sh"


def _helper_path() -> str:
    src = Path(__file__).parent / "remote" / "gateway-helper-v1.sh"
    return str(src.resolve())


def _parse_json_dict(raw: bytes, *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Parse JSON bytes into a dict, returning ``default`` (or {}) on missing/invalid input."""
    fallback = default if default is not None else {}
    if not raw:
        return fallback
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return fallback
    if isinstance(parsed, dict):
        return cast(dict[str, Any], parsed)
    return fallback


async def ensure_helper_installed(settings: Settings, alias: str) -> None:
    local = _helper_path()
    result = await scp_transfer(settings, alias, Path(local), HELPER_PATH)
    if result.exit_code != 0:
        raise GatewayError(
            ErrorCode.SSH_UNREACHABLE,
            f"Failed to upload helper: {result.stderr[:500]!r}",
            status_code=502,
        )
    await run_process(
        [
            settings.ssh_executable,
            "-F",
            str(settings.ssh_config_path),
            "--",
            alias,
            "chmod",
            "+x",
            HELPER_PATH,
        ],
        timeout=settings.subprocess_timeout,
    )


async def get_capabilities(settings: Settings, alias: str) -> RuntimeCapabilities:
    await ensure_helper_installed(settings, alias)
    result = await probe_capabilities(settings, alias, HELPER_PATH)
    if result.exit_code != 0:
        raise GatewayError(
            ErrorCode.SSH_UNREACHABLE,
            f"SSH probe failed: {result.stderr[:500]!r}",
            status_code=502,
        )
    try:
        data = _parse_json_dict(result.stdout)
    except json.JSONDecodeError as exc:
        raise GatewayError(
            ErrorCode.REMOTE_UNSUPPORTED,
            f"Invalid helper response: {result.stdout[:200]!r}",
            status_code=502,
        ) from exc

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
    alias: str,
    platform: str,
) -> None:
    await ensure_helper_installed(settings, alias)

    arch = (await get_capabilities(settings, alias)).arch if platform == "linux" else "unknown"

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

    result = await run_process(
        [
            settings.ssh_executable,
            "-F",
            str(settings.ssh_config_path),
            "--",
            alias,
            "/bin/sh",
            HELPER_PATH,
            "runtime-inspect",
            sha256,
            settings.openvscode_version,
        ],
        timeout=settings.subprocess_timeout,
    )

    data = _parse_json_dict(result.stdout)

    if data.get("installed"):
        return

    local_archive = await _download_and_verify(settings, url, sha256)

    remote_tmp = f"/tmp/ovs-{uuid.uuid4().hex[:12]}.tar.gz"
    result = await scp_transfer(settings, alias, local_archive, remote_tmp)
    if result.exit_code != 0:
        raise GatewayError(
            ErrorCode.RUNTIME_INSTALL_FAILED,
            f"Failed to upload runtime: {result.stderr[:500]!r}",
            status_code=502,
        )

    result = await run_process(
        [
            settings.ssh_executable,
            "-F",
            str(settings.ssh_config_path),
            "--",
            alias,
            "/bin/sh",
            HELPER_PATH,
            "runtime-install",
            remote_tmp,
            sha256,
            settings.openvscode_version,
        ],
        timeout=settings.subprocess_timeout * 2,
    )

    if result.exit_code != 0:
        raise GatewayError(
            ErrorCode.RUNTIME_INSTALL_FAILED,
            f"Remote install failed: {result.stderr[:500]!r}",
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
    alias: str,
    session_id: uuid.UUID,
) -> RuntimeIdentity:
    result = await run_process(
        [
            settings.ssh_executable,
            "-F",
            str(settings.ssh_config_path),
            "--",
            alias,
            "/bin/sh",
            HELPER_PATH,
            "session-start",
            str(session_id),
        ],
        timeout=settings.startup_timeout,
    )

    if result.exit_code != 0:
        raise GatewayError(
            ErrorCode.REMOTE_START_FAILED,
            f"Remote start failed: {result.stderr[:500]!r}",
            status_code=502,
        )

    try:
        data = _parse_json_dict(result.stdout)
    except json.JSONDecodeError as exc:
        raise GatewayError(
            ErrorCode.REMOTE_START_FAILED,
            f"Invalid start response: {result.stdout[:200]!r}",
            status_code=502,
        ) from exc

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
    alias: str,
    session_id: uuid.UUID,
) -> dict[str, Any]:
    result = await run_process(
        [
            settings.ssh_executable,
            "-F",
            str(settings.ssh_config_path),
            "--",
            alias,
            "/bin/sh",
            HELPER_PATH,
            "session-inspect",
            str(session_id),
        ],
        timeout=settings.subprocess_timeout,
    )

    data = _parse_json_dict(result.stdout)
    if not data:
        return {"error": "invalid_response"}
    return data


async def stop_session(
    settings: Settings,
    alias: str,
    session_id: uuid.UUID,
) -> bool:
    result = await run_process(
        [
            settings.ssh_executable,
            "-F",
            str(settings.ssh_config_path),
            "--",
            alias,
            "/bin/sh",
            HELPER_PATH,
            "session-stop",
            str(session_id),
        ],
        timeout=settings.stop_timeout,
    )

    data = _parse_json_dict(result.stdout)

    if "error" in data:
        raise GatewayError(
            ErrorCode.STOP_FAILED,
            str(data.get("detail", data["error"])),
            status_code=502,
        )

    return bool(data.get("stopped", False))


async def remove_session(
    settings: Settings,
    alias: str,
    session_id: uuid.UUID,
) -> None:
    await run_process(
        [
            settings.ssh_executable,
            "-F",
            str(settings.ssh_config_path),
            "--",
            alias,
            "/bin/sh",
            HELPER_PATH,
            "session-remove",
            str(session_id),
        ],
        timeout=settings.subprocess_timeout,
    )


async def list_sessions(
    settings: Settings,
    alias: str,
) -> list[str]:
    result = await run_process(
        [
            settings.ssh_executable,
            "-F",
            str(settings.ssh_config_path),
            "--",
            alias,
            "/bin/sh",
            HELPER_PATH,
            "session-list",
        ],
        timeout=settings.subprocess_timeout,
    )

    data = _parse_json_dict(result.stdout)
    sessions = data.get("sessions", [])
    if isinstance(sessions, list):
        return [str(s) for s in cast(list[Any], sessions)]
    return []


class RuntimeService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def capabilities(self, alias: str) -> RuntimeCapabilities:
        return await get_capabilities(self.settings, alias)

    async def ensure_installed(self, alias: str, platform: str) -> None:
        await ensure_installed(self.settings, alias, platform)

    async def start_session(self, alias: str, session_id: uuid.UUID) -> RuntimeIdentity:
        return await start_session(self.settings, alias, session_id)

    async def inspect_session(self, alias: str, session_id: uuid.UUID) -> dict[str, Any]:
        return await inspect_session(self.settings, alias, session_id)

    async def stop_session(self, alias: str, session_id: uuid.UUID) -> bool:
        return await stop_session(self.settings, alias, session_id)

    async def remove_session(self, alias: str, session_id: uuid.UUID) -> None:
        await remove_session(self.settings, alias, session_id)

    async def list_sessions(self, alias: str) -> list[str]:
        return await list_sessions(self.settings, alias)

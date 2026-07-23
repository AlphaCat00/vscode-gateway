"""Runtime helper response validation for durable cleanup decisions."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast
from uuid import uuid4

import asyncssh
import pytest

from tests.unit.ssh_backend_test_helpers import make_settings
from vscode_gateway import runtime as runtime_module
from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.models import RuntimeCapabilities
from vscode_gateway.runtime import RuntimeService
from vscode_gateway.ssh_connection import SshConnectionService


class _CompletedProcess:
    def __init__(self, *, exit_status: int, stdout: bytes, stderr: bytes = b"") -> None:
        self.exit_status = exit_status
        self.stdout = stdout
        self.stderr = stderr


def _install_result(
    monkeypatch: pytest.MonkeyPatch,
    result: _CompletedProcess,
) -> None:
    async def _ensure_helper(
        settings: object,
        conn: asyncssh.SSHClientConnection,
    ) -> None:
        del settings, conn

    async def _run_command(
        conn: asyncssh.SSHClientConnection,
        argv: list[str],
        *,
        timeout: float,
        stdin: bytes | None = None,
    ) -> asyncssh.SSHCompletedProcess:
        del conn, argv, timeout, stdin
        return cast(asyncssh.SSHCompletedProcess, result)

    monkeypatch.setattr(runtime_module, "ensure_helper_installed", _ensure_helper)
    monkeypatch.setattr(SshConnectionService, "run_command", staticmethod(_run_command))


def _connection() -> asyncssh.SSHClientConnection:
    return cast(asyncssh.SSHClientConnection, object())


@pytest.mark.asyncio
async def test_helper_is_uploaded_to_private_remote_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []
    uploads: list[tuple[str, str]] = []

    async def _run_command(
        conn: asyncssh.SSHClientConnection,
        argv: list[str],
        *,
        timeout: float,
        stdin: bytes | None = None,
    ) -> asyncssh.SSHCompletedProcess:
        del conn, timeout, stdin
        commands.append(argv)
        return cast(
            asyncssh.SSHCompletedProcess,
            _CompletedProcess(exit_status=0, stdout=b""),
        )

    async def _sftp_put(
        conn: asyncssh.SSHClientConnection,
        local_path: str,
        remote_path: str,
    ) -> None:
        del conn
        uploads.append((local_path, remote_path))

    monkeypatch.setattr(SshConnectionService, "run_command", staticmethod(_run_command))
    monkeypatch.setattr(SshConnectionService, "sftp_put", staticmethod(_sftp_put))
    settings = make_settings(tmp_path)

    await runtime_module.ensure_helper_installed(settings, _connection())

    assert commands == [
        ["mkdir", "-p", "--", ".vscode-gateway"],
        ["chmod", "700", "--", ".vscode-gateway"],
        [
            "chmod",
            "700",
            "--",
            ".vscode-gateway/gateway-helper-v1.sh",
        ],
    ]
    assert len(uploads) == 1
    assert Path(uploads[0][0]).name == "gateway-helper-v1.sh"
    assert uploads[0][1] == ".vscode-gateway/gateway-helper-v1.sh"


@pytest.mark.asyncio
async def test_runtime_archive_uses_github_filename_in_remote_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    settings.openvscode_version = "9.9.9"
    settings.openvscode_linux_x64_url = (
        "https://github.com/gitpod-io/openvscode-server/releases/download/"
        "openvscode-server-v9.9.9/openvscode-server-v9.9.9-linux-x64.tar.gz?download=1"
    )
    settings.openvscode_linux_x64_sha256 = "a" * 64
    local_archive = tmp_path / "cached-by-digest.tar.gz"
    local_archive.write_bytes(b"archive")
    commands: list[list[str]] = []
    uploads: list[tuple[str, str]] = []

    async def _capabilities(
        settings: object,
        conn: asyncssh.SSHClientConnection,
    ) -> RuntimeCapabilities:
        del settings, conn
        return RuntimeCapabilities(
            platform="linux",
            arch="x86_64",
            helper_version="1",
            available=True,
        )

    async def _download(
        settings: object,
        url: str,
        expected_sha256: str,
    ) -> Path:
        del settings, url, expected_sha256
        return local_archive

    async def _run_command(
        conn: asyncssh.SSHClientConnection,
        argv: list[str],
        *,
        timeout: float,
        stdin: bytes | None = None,
    ) -> asyncssh.SSHCompletedProcess:
        del conn, timeout, stdin
        commands.append(argv)
        stdout = b'{"installed":false}' if "runtime-inspect" in argv else b'{"installed":true}'
        return cast(
            asyncssh.SSHCompletedProcess,
            _CompletedProcess(exit_status=0, stdout=stdout),
        )

    async def _sftp_put(
        conn: asyncssh.SSHClientConnection,
        local_path: str,
        remote_path: str,
    ) -> None:
        del conn
        uploads.append((local_path, remote_path))

    monkeypatch.setattr(runtime_module, "get_capabilities", _capabilities)
    monkeypatch.setattr(runtime_module, "_download_and_verify", _download)
    monkeypatch.setattr(SshConnectionService, "run_command", staticmethod(_run_command))
    monkeypatch.setattr(SshConnectionService, "sftp_put", staticmethod(_sftp_put))

    await runtime_module.ensure_installed(settings, _connection(), "linux")

    remote_archive = ".vscode-gateway/openvscode-server-v9.9.9-linux-x64.tar.gz"
    assert uploads == [(str(local_archive), remote_archive)]
    assert commands[-1] == [
        "/bin/sh",
        runtime_module.HELPER_PATH,
        "runtime-install",
        remote_archive,
        "a" * 64,
        "9.9.9",
    ]


@pytest.mark.asyncio
async def test_start_uses_stable_alias_hash_for_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    async def _run_command(
        conn: asyncssh.SSHClientConnection,
        argv: list[str],
        *,
        timeout: float,
        stdin: bytes | None = None,
    ) -> asyncssh.SSHCompletedProcess:
        del conn, timeout, stdin
        commands.append(argv)
        return cast(
            asyncssh.SSHCompletedProcess,
            _CompletedProcess(
                exit_status=0,
                stdout=(
                    b'{"pid":42,"port":9876,"boot_id":"boot",'
                    b'"process_start_id":"start","executable":"/opt/node",'
                    b'"session_dir":"/remote/session"}'
                ),
            ),
        )

    monkeypatch.setattr(SshConnectionService, "run_command", staticmethod(_run_command))
    runtime = RuntimeService(make_settings(tmp_path))
    first_id = uuid4()
    second_id = uuid4()

    await runtime.start_session(_connection(), first_id, "host-a")
    await runtime.start_session(_connection(), second_id, "host-a")

    profile_id = hashlib.sha256(b"host-a").hexdigest()
    assert commands == [
        [
            "/bin/sh",
            runtime_module.HELPER_PATH,
            "session-start",
            str(first_id),
            profile_id,
        ],
        [
            "/bin/sh",
            runtime_module.HELPER_PATH,
            "session-start",
            str(second_id),
            profile_id,
        ],
    ]


@pytest.mark.asyncio
async def test_inspect_rejects_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_result(
        monkeypatch,
        _CompletedProcess(exit_status=1, stdout=b"", stderr=b"inspect failed"),
    )
    runtime = RuntimeService(make_settings(tmp_path))

    with pytest.raises(GatewayError) as exc_info:
        await runtime.inspect_session(_connection(), uuid4())

    assert exc_info.value.code == ErrorCode.RECOVERY_FAILED


@pytest.mark.asyncio
async def test_inspect_requires_explicit_running_boolean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_result(monkeypatch, _CompletedProcess(exit_status=0, stdout=b"{}"))
    runtime = RuntimeService(make_settings(tmp_path))

    with pytest.raises(GatewayError) as exc_info:
        await runtime.inspect_session(_connection(), uuid4())

    assert exc_info.value.code == ErrorCode.RECOVERY_FAILED


@pytest.mark.asyncio
async def test_stop_requires_explicit_absence_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_result(
        monkeypatch,
        _CompletedProcess(exit_status=0, stdout=b'{"stopped":false}'),
    )
    runtime = RuntimeService(make_settings(tmp_path))

    with pytest.raises(GatewayError) as exc_info:
        await runtime.stop_session(_connection(), uuid4())

    assert exc_info.value.code == ErrorCode.STOP_FAILED


@pytest.mark.asyncio
async def test_remove_requires_explicit_removal_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_result(monkeypatch, _CompletedProcess(exit_status=0, stdout=b"{}"))
    runtime = RuntimeService(make_settings(tmp_path))

    with pytest.raises(GatewayError) as exc_info:
        await runtime.remove_session(_connection(), uuid4())

    assert exc_info.value.code == ErrorCode.STOP_FAILED

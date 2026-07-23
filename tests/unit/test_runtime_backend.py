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

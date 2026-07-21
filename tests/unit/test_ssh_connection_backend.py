"""Focused tests for AsyncSSH connection mapping and local forwarding."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import asyncssh
import pytest

from tests.unit.ssh_backend_test_helpers import (
    add_session,
    generate_key,
    make_settings,
    migrated_database,
)
from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.host_trust import HostTrustService
from vscode_gateway.models import SessionId
from vscode_gateway.settings import Settings
from vscode_gateway.ssh_connection import (
    CapturedHostKey,
    SshConnection,
    SshConnectionService,
    _HostKeyCapturer,
)
from vscode_gateway.ssh_keys import SshKeyService


class _FakeKeyService:
    def __init__(self, keys: list[asyncssh.SSHKey]) -> None:
        self.keys = keys

    def load_present_keys(self) -> list[asyncssh.SSHKey]:
        return self.keys


class _FakeConnection:
    def __init__(self) -> None:
        self.forward_calls: list[tuple[str, int, str, int]] = []
        self.run_calls: list[tuple[str, bool, float, None, bytes | None]] = []
        self.run_result = object()
        self.closed = False
        self.listener = object()

    async def forward_local_port(
        self, local_host: str, local_port: int, remote_host: str, remote_port: int
    ) -> object:
        self.forward_calls.append((local_host, local_port, remote_host, remote_port))
        return self.listener

    def close(self) -> None:
        self.closed = True

    async def run(
        self,
        command: str,
        *,
        check: bool,
        timeout: float,
        encoding: None,
        input: bytes | None = None,
    ) -> object:
        self.run_calls.append((command, check, timeout, encoding, input))
        return self.run_result


def _connection_service(
    settings: Settings,
    key_service: Any,
    trust_service: Any = None,
) -> SshConnectionService:
    return SshConnectionService(
        settings,
        cast(SshKeyService, key_service),
        cast(HostTrustService, trust_service or object()),
    )


@pytest.mark.asyncio
async def test_no_uploaded_keys_maps_to_gateway_error_without_connecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    async with migrated_database(tmp_path) as database:
        key_service = SshKeyService(settings, database)
        trust_service = HostTrustService(settings, database)
        service = _connection_service(settings, key_service, trust_service)
        calls = 0

        async def fail_if_called(*args: Any, **kwargs: Any) -> None:
            nonlocal calls
            calls += 1
            raise AssertionError("network connection should not be attempted")

        monkeypatch.setattr("vscode_gateway.ssh_connection.asyncssh.connect", fail_if_called)
        with pytest.raises(GatewayError) as exc_info:
            await service.connect_for_session(
                session_id=SessionId("2d3d9e3d-4b80-4f83-98c4-32bc8f0e1f5a"),
                alias="production",
            )

    assert exc_info.value.code == ErrorCode.SSH_NO_UPLOADED_KEYS
    assert calls == 0


@pytest.mark.asyncio
async def test_all_uploaded_keys_are_passed_to_one_asyncssh_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    keys = [
        generate_key("ssh-ed25519"),
        generate_key("ecdsa-sha2-nistp256"),
        generate_key("ssh-rsa"),
    ]
    fake_connection = _FakeConnection()
    captured: dict[str, Any] = {}

    async def fake_connect(*args: Any, **kwargs: Any) -> _FakeConnection:
        captured["args"] = args
        captured.update(kwargs)
        return fake_connection

    monkeypatch.setattr("vscode_gateway.ssh_connection.asyncssh.connect", fake_connect)
    service = _connection_service(settings, _FakeKeyService(keys))
    result = await service.connect_for_session(
        session_id=SessionId("2d3d9e3d-4b80-4f83-98c4-32bc8f0e1f5a"),
        alias="production",
    )

    assert result.conn is fake_connection
    assert captured["args"] == ("production",)
    assert captured["config"] == [str(settings.ssh_config_path)]
    assert captured["client_keys"] == keys
    assert captured["known_hosts"] == str(settings.ssh_known_hosts_path)
    assert captured["agent_path"] is None
    assert captured["password_auth"] is False
    assert captured["kbdint_auth"] is False
    assert captured["host_based_auth"] is False
    assert captured["gss_auth"] is False
    assert captured["gss_kex"] is False


@pytest.mark.asyncio
async def test_permission_denied_maps_to_key_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    key = generate_key("ssh-ed25519")

    async def deny(*args: Any, **kwargs: Any) -> None:
        raise asyncssh.PermissionDenied("publickey authentication failed")

    monkeypatch.setattr("vscode_gateway.ssh_connection.asyncssh.connect", deny)
    service = _connection_service(settings, _FakeKeyService([key]))

    with pytest.raises(GatewayError) as exc_info:
        await service.connect_for_session(
            session_id=SessionId("2d3d9e3d-4b80-4f83-98c4-32bc8f0e1f5a"),
            alias="production",
        )

    assert exc_info.value.code == ErrorCode.SSH_NO_UPLOADED_KEY_ACCEPTED


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [OSError("connection refused"), TimeoutError()])
async def test_oserror_and_timeout_map_to_ssh_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    settings = make_settings(tmp_path)

    async def fail(*args: Any, **kwargs: Any) -> None:
        raise failure

    monkeypatch.setattr("vscode_gateway.ssh_connection.asyncssh.connect", fail)
    service = _connection_service(
        settings,
        _FakeKeyService([generate_key("ssh-ed25519")]),
    )

    with pytest.raises(GatewayError) as exc_info:
        await service.connect_for_session(
            session_id=SessionId("2d3d9e3d-4b80-4f83-98c4-32bc8f0e1f5a"),
            alias="production",
        )

    assert exc_info.value.code == ErrorCode.SSH_UNREACHABLE


@pytest.mark.asyncio
async def test_host_key_challenge_is_recorded_from_connect_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    presented = generate_key("ssh-ed25519")
    old_key = generate_key("ssh-rsa")
    settings.ssh_known_hosts_path.write_text(
        f"remote.example.test {old_key.export_public_key('openssh').decode().strip()}\n",
        encoding="utf-8",
    )

    async with migrated_database(tmp_path) as database:
        session_id = await add_session(database)
        trust_service = HostTrustService(settings, database)
        service = _connection_service(
            settings,
            _FakeKeyService([generate_key("ssh-ed25519")]),
            trust_service,
        )

        async def reject_unknown_host(*args: Any, **kwargs: Any) -> None:
            factory = kwargs["client_factory"]
            capturer: _HostKeyCapturer = factory()
            public_key = presented.export_public_key("openssh").decode().strip()
            capturer.captured[("remote.example.test", 22)] = [
                CapturedHostKey(
                    host="remote.example.test",
                    addr="203.0.113.10",
                    port=22,
                    algorithm=presented.get_algorithm(),
                    fingerprint=presented.get_fingerprint("sha256"),
                    public_key_text=public_key,
                )
            ]
            raise asyncssh.HostKeyNotVerifiable("host key changed")

        monkeypatch.setattr("vscode_gateway.ssh_connection.asyncssh.connect", reject_unknown_host)
        with pytest.raises(GatewayError) as exc_info:
            await service.connect_for_session(
                session_id=session_id,
                alias="production",
            )

        challenge = await trust_service.get_challenge(session_id)

    assert exc_info.value.code == ErrorCode.SSH_HOST_CHANGED
    assert challenge is not None
    assert challenge.host == "remote.example.test"
    assert challenge.port == 22
    assert challenge.algorithm == "ssh-ed25519"
    assert challenge.fingerprint == presented.get_fingerprint("sha256")
    assert challenge.public_key == presented.export_public_key("openssh").decode().strip()


@pytest.mark.asyncio
async def test_unknown_host_key_is_reported_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    presented = generate_key("ssh-ed25519")

    async with migrated_database(tmp_path) as database:
        session_id = await add_session(database)
        trust_service = HostTrustService(settings, database)
        service = _connection_service(
            settings,
            _FakeKeyService([generate_key("ssh-ed25519")]),
            trust_service,
        )

        async def reject_unknown_host(*args: Any, **kwargs: Any) -> None:
            capturer: _HostKeyCapturer = kwargs["client_factory"]()
            capturer.captured[("new.example.test", 22)] = [
                CapturedHostKey(
                    host="new.example.test",
                    addr="203.0.113.11",
                    port=22,
                    algorithm=presented.get_algorithm(),
                    fingerprint=presented.get_fingerprint("sha256"),
                    public_key_text=presented.export_public_key("openssh").decode().strip(),
                )
            ]
            raise asyncssh.HostKeyNotVerifiable("host key unknown")

        monkeypatch.setattr("vscode_gateway.ssh_connection.asyncssh.connect", reject_unknown_host)
        with pytest.raises(GatewayError) as exc_info:
            await service.connect_for_session(session_id=session_id, alias="production")

    assert exc_info.value.code == ErrorCode.SSH_HOST_UNKNOWN


@pytest.mark.asyncio
async def test_local_forward_uses_loopback_and_records_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    fake_connection = _FakeConnection()
    ssh_connection = SshConnection(
        conn=cast(asyncssh.SSHClientConnection, fake_connection),
        listener=None,
        local_port=0,
        remote_port=0,
        alias="production",
        capturer=_HostKeyCapturer(),
    )
    monkeypatch.setattr("vscode_gateway.ssh_connection._allocate_local_port", lambda: 41234)
    service = _connection_service(settings, _FakeKeyService([]))

    listener, local_port = await service.forward_local_port(ssh_connection, 8765)

    assert listener is fake_connection.listener
    assert local_port == 41234
    assert fake_connection.forward_calls == [("127.0.0.1", 41234, "127.0.0.1", 8765)]
    assert ssh_connection.listener is fake_connection.listener
    assert ssh_connection.local_port == 41234
    assert ssh_connection.remote_port == 8765
    assert ssh_connection.tunnel_pid == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argv", "expected_command"),
    [
        (["/bin/echo", "hello world"], "/bin/echo 'hello world'"),
        (
            ["helper", "a'b", "$(touch /tmp/pwned); $HOME"],
            "helper 'a'\"'\"'b' '$(touch /tmp/pwned); $HOME'",
        ),
    ],
)
async def test_run_command_uses_one_shell_quoted_remote_command(
    argv: list[str], expected_command: str
) -> None:
    fake_connection = _FakeConnection()

    result = await SshConnectionService.run_command(
        cast(asyncssh.SSHClientConnection, fake_connection),
        argv,
        timeout=3.5,
        stdin=b"input",
    )

    assert result is fake_connection.run_result
    assert fake_connection.run_calls == [(expected_command, False, 3.5, None, b"input")]


@pytest.mark.asyncio
@pytest.mark.parametrize("argv", [[], ["contains\x00nul"]])
async def test_run_command_rejects_empty_or_nul_arguments(argv: list[str]) -> None:
    fake_connection = _FakeConnection()

    with pytest.raises(ValueError):
        await SshConnectionService.run_command(
            cast(asyncssh.SSHClientConnection, fake_connection), argv, timeout=3.5
        )

    assert fake_connection.run_calls == []

"""Focused tests for AsyncSSH connection mapping and local forwarding."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
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
    _parse_proxy_jump,
    _ProxyJumpConfigError,
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
        self.waited = False
        self.listener = object()

    async def forward_local_port(
        self, local_host: str, local_port: int, remote_host: str, remote_port: int
    ) -> object:
        self.forward_calls.append((local_host, local_port, remote_host, remote_port))
        return self.listener

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True

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
    assert captured["args"] == ("production", 22)
    assert captured["config"] == [str(settings.ssh_config_path)]
    assert captured["tunnel"] is None
    assert captured["options"].host == "production"
    assert captured["client_keys"] == keys
    assert captured["known_hosts"] == str(settings.ssh_known_hosts_path)
    assert captured["agent_path"] is None
    assert captured["agent_identities"] == []
    assert captured["agent_forwarding"] is False
    assert captured["password"] is None
    assert captured["password_auth"] is False
    assert captured["kbdint_auth"] is False
    assert captured["host_based_auth"] is False
    assert captured["client_host_keysign"] is False
    assert captured["client_host_keys"] == []
    assert captured["gss_auth"] is False
    assert captured["gss_kex"] is False
    assert captured["gss_host"] is None
    assert captured["gss_delegate_creds"] is False
    assert captured["public_key_auth"] is True
    assert captured["preferred_auth"] == ["publickey"]
    assert captured["disable_trivial_auth"] is True
    assert captured["pkcs11_provider"] is None
    assert captured["x509_trusted_certs"] is None
    assert captured["x509_trusted_cert_paths"] == []
    assert captured["proxy_command"] is None


@pytest.mark.asyncio
async def test_proxy_jump_connections_are_expanded_in_nested_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    settings.ssh_config_path.write_text(
        """Host production
    Hostname target.example.test
    Port 2201
    ProxyJump jump-a,jump-b
Host jump-a
    Hostname jump-a.example.test
    ProxyJump nested
Host nested
    Hostname nested.example.test
    Port 2202
Host jump-b
    Hostname jump-b.example.test
    Port 2203
""",
        encoding="utf-8",
    )
    connections = [_FakeConnection() for _ in range(4)]
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def fake_connect(*args: Any, **kwargs: Any) -> _FakeConnection:
        calls.append((args, kwargs))
        return connections[len(calls) - 1]

    monkeypatch.setattr("vscode_gateway.ssh_connection.asyncssh.connect", fake_connect)
    service = _connection_service(settings, _FakeKeyService([generate_key("ssh-ed25519")]))

    result = await service.connect_for_session(
        session_id=SessionId("2d3d9e3d-4b80-4f0e-98c4-32bc8f0e1f5a"),
        alias="production",
    )

    assert [call[0][0] for call in calls] == ["nested", "jump-a", "jump-b", "production"]
    assert [call[0][1] for call in calls] == [2202, 22, 2203, 2201]
    assert calls[0][1]["tunnel"] is None
    assert calls[1][1]["tunnel"] is connections[0]
    assert calls[2][1]["tunnel"] is connections[1]
    assert calls[3][1]["tunnel"] is connections[2]
    assert calls[0][1]["options"].host == "nested.example.test"
    assert calls[3][1]["options"].host == "target.example.test"
    assert result.chain == tuple(connections)
    capturers = [call[1]["client_factory"]() for call in calls]
    assert len({id(capturer) for capturer in capturers}) == len(capturers)
    for _, kwargs in calls:
        assert kwargs["known_hosts"] == str(settings.ssh_known_hosts_path)
        assert kwargs["client_keys"]
        assert kwargs["agent_path"] is None
        assert kwargs["password_auth"] is False
        assert kwargs["kbdint_auth"] is False
        assert kwargs["host_based_auth"] is False
        assert kwargs["gss_auth"] is False
        assert kwargs["gss_kex"] is False
        assert kwargs["public_key_auth"] is True
        assert kwargs["preferred_auth"] == ["publickey"]
        assert kwargs["disable_trivial_auth"] is True
        assert kwargs["pkcs11_provider"] is None
        assert kwargs["x509_trusted_certs"] is None
        assert kwargs["proxy_command"] is None


def test_proxy_jump_parser_validates_none_ipv6_and_whitespace() -> None:
    hops = _parse_proxy_jump("alice@example.com@[2001:db8::1]:2200,bob@jump.example.test")
    assert [(hop.user, hop.host, hop.port) for hop in hops] == [
        ("alice@example.com", "2001:db8::1", 2200),
        ("bob", "jump.example.test", None),
    ]
    assert _parse_proxy_jump("none") == ()

    for malformed in (
        " jump.example.test",
        "jump.example.test,",
        "none,jump.example.test",
        "jump.example.test:0",
        "jump.example.test:not-a-port",
        "2001:db8::1",
        "[not-an-ipv6-address]",
    ):
        with pytest.raises(_ProxyJumpConfigError):
            _parse_proxy_jump(malformed)


@pytest.mark.asyncio
async def test_proxy_jump_cycle_and_depth_are_rejected_before_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    settings.ssh_config_path.write_text(
        """Host production
    ProxyJump jump-a
Host jump-a
    ProxyJump production
""",
        encoding="utf-8",
    )
    calls = 0

    async def fail_if_called(*args: Any, **kwargs: Any) -> _FakeConnection:
        nonlocal calls
        calls += 1
        raise AssertionError("cycle should be rejected during route resolution")

    monkeypatch.setattr("vscode_gateway.ssh_connection.asyncssh.connect", fail_if_called)
    service = _connection_service(settings, _FakeKeyService([generate_key("ssh-ed25519")]))

    with pytest.raises(GatewayError) as exc_info:
        await service.connect_for_session(
            session_id=SessionId("2d3d9e3d-4b80-4f0e-98c4-32bc8f0e1f5a"),
            alias="production",
        )
    assert exc_info.value.code == ErrorCode.SSH_CONFIG_INVALID
    assert calls == 0

    settings.ssh_config_path.write_text(
        "Host production\n    ProxyJump jump-a,jump-a\n",
        encoding="utf-8",
    )

    with pytest.raises(GatewayError) as exc_info:
        await service.connect_for_session(
            session_id=SessionId("2d3d9e3d-4b80-4f0e-98c4-32bc8f0e1f5a"),
            alias="production",
        )
    assert exc_info.value.code == ErrorCode.SSH_CONFIG_INVALID
    assert calls == 0

    lines = ["Host production", "    ProxyJump hop0"]
    for index in range(8):
        lines.extend((f"Host hop{index}", f"    ProxyJump hop{index + 1}"))
    lines.append("Host hop8")
    settings.ssh_config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(GatewayError) as exc_info:
        await service.connect_for_session(
            session_id=SessionId("2d3d9e3d-4b80-4f0e-98c4-32bc8f0e1f5a"),
            alias="production",
        )
    assert exc_info.value.code == ErrorCode.SSH_CONFIG_INVALID
    assert calls == 0


@pytest.mark.asyncio
async def test_proxy_jump_allows_eight_jump_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    lines = ["Host production", "    ProxyJump hop0"]
    for index in range(7):
        lines.extend((f"Host hop{index}", f"    ProxyJump hop{index + 1}"))
    lines.append("Host hop7")
    settings.ssh_config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    connections = [_FakeConnection() for _ in range(9)]
    calls = 0

    async def fake_connect(*args: Any, **kwargs: Any) -> _FakeConnection:
        nonlocal calls
        connection = connections[calls]
        calls += 1
        return connection

    monkeypatch.setattr("vscode_gateway.ssh_connection.asyncssh.connect", fake_connect)
    service = _connection_service(settings, _FakeKeyService([generate_key("ssh-ed25519")]))

    result = await service.connect_for_session(
        session_id=SessionId("2d3d9e3d-4b80-4f0e-98c4-32bc8f0e1f5a"),
        alias="production",
    )

    assert calls == 9
    assert result.chain == tuple(connections)


@pytest.mark.asyncio
async def test_proxy_jump_partial_chain_is_closed_on_connect_failure_and_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    settings.ssh_config_path.write_text(
        """Host production
    ProxyJump jump-a
    """,
        encoding="utf-8",
    )
    first = _FakeConnection()
    calls: int = 0

    async def fail_on_target(*args: Any, **kwargs: Any) -> _FakeConnection:
        nonlocal calls
        calls += 1
        if calls == 1:
            return first
        raise OSError("target unavailable")

    monkeypatch.setattr("vscode_gateway.ssh_connection.asyncssh.connect", fail_on_target)
    service = _connection_service(settings, _FakeKeyService([generate_key("ssh-ed25519")]))

    with pytest.raises(GatewayError) as exc_info:
        await service.connect_for_session(
            session_id=SessionId("2d3d9e3d-4b80-4f0e-98c4-32bc8f0e1f5a"),
            alias="production",
        )
    assert exc_info.value.code == ErrorCode.SSH_UNREACHABLE
    assert first.closed is True
    assert first.waited is True

    first = _FakeConnection()
    calls = 0
    pending = asyncio.Event()
    second_started = asyncio.Event()

    async def cancel_on_target(*args: Any, **kwargs: Any) -> _FakeConnection:
        nonlocal calls
        calls += 1
        if calls == 1:
            return first
        second_started.set()
        await pending.wait()
        raise AssertionError("target should be cancelled")

    monkeypatch.setattr("vscode_gateway.ssh_connection.asyncssh.connect", cancel_on_target)
    task = asyncio.create_task(
        service.connect_for_session(
            session_id=SessionId("2d3d9e3d-4b80-4f0e-98c4-32bc8f0e1f5a"),
            alias="production",
        )
    )
    await asyncio.wait_for(second_started.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert first.closed is True
    assert first.waited is True


@pytest.mark.asyncio
async def test_proxy_jump_uses_one_overall_connect_timeout_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    settings.ssh_connect_timeout = 1.0
    settings.ssh_config_path.write_text(
        """Host production
    ProxyJump jump-a
""",
        encoding="utf-8",
    )
    timeouts: list[float] = []
    connections = [_FakeConnection(), _FakeConnection()]

    async def delayed_connect(*args: Any, **kwargs: Any) -> _FakeConnection:
        timeouts.append(cast(float, kwargs["connect_timeout"]))
        await asyncio.sleep(0.01)
        if len(timeouts) == 2:
            raise OSError("target unavailable")
        return connections[0]

    monkeypatch.setattr("vscode_gateway.ssh_connection.asyncssh.connect", delayed_connect)
    service = _connection_service(settings, _FakeKeyService([generate_key("ssh-ed25519")]))

    with pytest.raises(GatewayError) as exc_info:
        await service.connect_for_session(
            session_id=SessionId("2d3d9e3d-4b80-4f0e-98c4-32bc8f0e1f5a"),
            alias="production",
        )

    assert exc_info.value.code == ErrorCode.SSH_UNREACHABLE
    assert len(timeouts) == 2
    assert 0 < timeouts[1] < timeouts[0] <= settings.ssh_connect_timeout


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
    assert challenge.role == "target"
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
async def test_jump_host_key_challenge_uses_jump_role_and_selected_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    settings.ssh_config_path.write_text(
        """Host production
    ProxyJump jump.example.test
""",
        encoding="utf-8",
    )
    presented = generate_key("ssh-ed25519")
    session_id = SessionId("2d3d9e3d-4b80-4f0e-98c4-32bc8f0e1f5a")

    async with migrated_database(tmp_path) as database:
        await add_session(database, session_id=session_id)
        trust_service = HostTrustService(settings, database)
        service = _connection_service(
            settings,
            _FakeKeyService([generate_key("ssh-ed25519")]),
            trust_service,
        )

        async def reject_jump(*args: Any, **kwargs: Any) -> None:
            capturer: _HostKeyCapturer = kwargs["client_factory"]()
            capturer.captured[("jump.example.test", 22)] = [
                CapturedHostKey(
                    host="jump.example.test",
                    addr="203.0.113.12",
                    port=22,
                    algorithm=presented.get_algorithm(),
                    fingerprint=presented.get_fingerprint("sha256"),
                    public_key_text=presented.export_public_key("openssh").decode().strip(),
                )
            ]
            raise asyncssh.HostKeyNotVerifiable("jump host key unknown")

        monkeypatch.setattr("vscode_gateway.ssh_connection.asyncssh.connect", reject_jump)
        with pytest.raises(GatewayError) as exc_info:
            await service.connect_for_session(session_id=session_id, alias="production")
        challenge = await trust_service.get_challenge(session_id)

    assert exc_info.value.code == ErrorCode.SSH_HOST_UNKNOWN
    assert challenge is not None
    assert challenge.role == "jump"
    assert challenge.alias == "production"
    assert challenge.host == "jump.example.test"


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

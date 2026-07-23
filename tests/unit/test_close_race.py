"""Unit tests for close/start race correctness.

These tests exercise the close()/_do_close refactor that re-reads the
session row by session id after cancelling and awaiting the start task.

* the start task persists remote identity after close()'s stale snapshot
  but before cancellation is observed; close re-reads and stops the
  remote, then deletes the row and releases capacity exactly once;
* the same race for tunnel identity;
* the start task completes ``_open_failure_cleanup`` (clearing
  persisted identity) before close re-reads; close becomes a no-op row
  delete with no double-stop;
* close against a fully ``ready`` session stops both the remote and the
  tunnel and releases capacity exactly once.
"""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, cast

import aiosqlite
import asyncssh
import pytest

from tests.support.session_harness import (
    FakeConnectionHandle as _FakeConnectionHandle,
)
from tests.support.session_harness import (
    FakeConnectionService as _FakeConnectionService,
)
from tests.support.session_harness import (
    FakeListener as _FakeListener,
)
from tests.support.session_harness import (
    install_happy_open_stubs as _install_happy_open_stubs,
)
from tests.support.session_harness import (
    make_catalog,
    make_session_service,
    make_settings,
)
from tests.support.session_harness import (
    wait_for_capacity_release as _wait_for_capacity_release,
)
from vscode_gateway import sessions as sessions_mod
from vscode_gateway.db import (
    get_session,
    insert_session,
    set_remote_identity,
    set_tunnel_identity,
)
from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.host_trust import HostTrustService
from vscode_gateway.models import (
    HostKeyRole,
    SessionId,
    SessionRecord,
    SessionStage,
    SessionState,
)
from vscode_gateway.runtime import RuntimeService
from vscode_gateway.sessions import SessionService
from vscode_gateway.settings import Settings
from vscode_gateway.ssh_config import SshCatalog
from vscode_gateway.ssh_connection import SshConnection, _HostKeyCapturer


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path, capacity=4)


@pytest.fixture
def catalog(settings: Settings) -> SshCatalog:
    return make_catalog(settings, ("host-a",))


@pytest.fixture
def runtime(settings: Settings) -> RuntimeService:
    return RuntimeService(settings)


class _OrderedConnectionHandle(_FakeConnectionHandle):
    def __init__(self, name: str, close_order: list[str], *, fail_wait: bool = False) -> None:
        super().__init__()
        self.name = name
        self.close_order = close_order
        self.fail_wait = fail_wait

    def close(self) -> None:
        self.close_order.append(self.name)
        super().close()

    async def wait_closed(self) -> None:
        self.waited = True
        if self.fail_wait:
            raise RuntimeError("synthetic close failure")


class _ConnectionBoundListener(_FakeListener):
    def __init__(self, connection: _FakeConnectionHandle) -> None:
        super().__init__()
        self.connection = connection

    async def wait_closed(self) -> None:
        if not self.connection.closed:
            raise RuntimeError("active forwards remain until the SSH connection closes")
        await super().wait_closed()


@pytest.fixture
def connection_service() -> _FakeConnectionService:
    return _FakeConnectionService()


@pytest.fixture
def service(
    settings: Settings,
    db: aiosqlite.Connection,
    catalog: SshCatalog,
    runtime: RuntimeService,
    connection_service: _FakeConnectionService,
) -> SessionService:
    return make_session_service(settings, db, catalog, runtime, connection_service)


async def test_shutdown_awaits_listener_and_connection_close(
    service: SessionService,
    connection_service: _FakeConnectionService,
) -> None:
    sid = uuid.uuid4()
    ssh_conn = await connection_service.connect_for_session(session_id=sid, alias="host-a")
    listener = _FakeListener()
    service._tunnels[sid] = sessions_mod._SessionTunnel(
        ssh_conn=ssh_conn,
        listener=cast(asyncssh.SSHListener, listener),
    )

    await service.shutdown()

    handle = cast(_FakeConnectionHandle, ssh_conn.conn)
    assert listener.closed is True
    assert listener.waited is True
    assert handle.closed is True
    assert handle.waited is True
    assert service._tunnels == {}


async def test_shutdown_closes_target_then_jumps_in_reverse_order(
    service: SessionService,
) -> None:
    sid = uuid.uuid4()
    close_order: list[str] = []
    jump_a = _OrderedConnectionHandle("jump-a", close_order)
    jump_b = _OrderedConnectionHandle("jump-b", close_order, fail_wait=True)
    target = _OrderedConnectionHandle("target", close_order)
    ssh_conn = SshConnection(
        conn=cast(asyncssh.SSHClientConnection, target),
        listener=None,
        local_port=0,
        remote_port=0,
        alias="host-a",
        capturer=_HostKeyCapturer(),
        connections=tuple(
            cast(asyncssh.SSHClientConnection, connection)
            for connection in (jump_a, jump_b, target)
        ),
    )
    service._tunnels[sid] = sessions_mod._SessionTunnel(ssh_conn=ssh_conn)

    await service.shutdown()

    assert close_order == ["target", "jump-b", "jump-a"]
    assert jump_a.waited is True
    assert jump_b.waited is True
    assert target.waited is True


async def test_close_retains_connection_handle_when_wait_closed_fails(
    service: SessionService,
    db: aiosqlite.Connection,
    connection_service: _FakeConnectionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_happy_open_stubs(monkeypatch)
    sid = uuid.uuid4()
    record = SessionRecord(id=sid, alias="host-a", state=SessionState.READY)
    record.remote_pid = 4242
    await insert_session(db, record)
    service._capacity_acquire(sid)
    ssh_conn = await connection_service.connect_for_session(session_id=sid, alias="host-a")
    service._tunnels[sid] = sessions_mod._SessionTunnel(ssh_conn=ssh_conn)

    async def _fail_close(self: SessionService, connection: asyncssh.SSHClientConnection) -> str:
        del self, connection
        return "SSH connection close error: timed out"

    monkeypatch.setattr(SessionService, "_close_connection", _fail_close)

    await service._do_close(record)

    remaining = await get_session(db, str(sid))
    assert remaining is not None
    assert remaining.state == SessionState.ERROR
    assert remaining.error_code == ErrorCode.STOP_FAILED.value
    assert service._tunnels[sid].ssh_conn is ssh_conn
    assert sid in service._capacity_owned


# ---------------------------------------------------------------------------
# Race 1: start task persists remote identity after close's stale snapshot
# ---------------------------------------------------------------------------


async def test_close_rereads_after_cancelling_start_task_and_stops_remote(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close re-reads and stops identity persisted during cancellation."""
    state = _install_happy_open_stubs(monkeypatch)
    released_ids: list[SessionId] = []
    original_capacity_release = service._capacity_release

    def _spy_capacity_release(session_id: SessionId) -> None:
        released_ids.append(session_id)
        original_capacity_release(session_id)

    monkeypatch.setattr(service, "_capacity_release", _spy_capacity_release)

    wrote_identity: asyncio.Event = asyncio.Event()

    async def _stub_do_open_write_on_cancel(
        self: SessionService, session_id: SessionId, alias: str
    ) -> Any:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            # Bypass open cleanup so close must reclaim the identity.
            await sessions_mod.set_remote_identity(
                db,
                str(session_id),
                4242,
                9876,
                "boot-abc",
                "psid-xyz",
                "/opt/openvscode/node",
            )
            wrote_identity.set()
            raise

    monkeypatch.setattr(SessionService, "_do_open", _stub_do_open_write_on_cancel)

    sid = uuid.uuid4()
    await insert_session(
        db,
        SessionRecord(
            id=sid, alias="host-a", state=SessionState.STARTING, stage=SessionStage.VALIDATE
        ),
    )
    service._capacity_acquire(sid)

    task = asyncio.create_task(service._run_open(sid, "host-a"))
    service._start_tasks[sid] = task
    await asyncio.sleep(0.05)

    pre = await get_session(db, str(sid))
    assert pre is not None
    assert pre.remote_pid is None

    close_task = asyncio.create_task(service.close("host-a"))
    await asyncio.wait_for(wrote_identity.wait(), timeout=2.0)
    released = await _wait_for_capacity_release(service, sid)
    assert released
    await close_task

    assert await get_session(db, str(sid)) is None
    assert service._capacity_owned == set()
    assert state.stop_calls == [sid]
    assert released_ids == [sid]
    assert service._start_tasks.get(sid) is None
    assert task.cancelled() or task.done()


# ---------------------------------------------------------------------------
# Race 2: start task persists tunnel identity after close's stale snapshot
# ---------------------------------------------------------------------------


async def test_close_rereads_after_cancelling_start_task_and_stops_tunnel(
    service: SessionService,
    db: aiosqlite.Connection,
    connection_service: _FakeConnectionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close re-reads and stops a tunnel persisted during cancellation."""
    _install_happy_open_stubs(monkeypatch)

    wrote_identity: asyncio.Event = asyncio.Event()
    listener: _FakeListener | None = None

    async def _stub_do_open_write_on_cancel(
        self: SessionService, session_id: SessionId, alias: str
    ) -> Any:
        nonlocal listener
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            ssh_conn = await connection_service.connect_for_session(
                session_id=session_id, alias=alias
            )
            listener = _FakeListener()
            self._tunnels[session_id] = sessions_mod._SessionTunnel(
                ssh_conn=ssh_conn,
                listener=cast(asyncssh.SSHListener, listener),
            )
            await sessions_mod.set_tunnel_identity(db, str(session_id), 54321)
            wrote_identity.set()
            raise

    monkeypatch.setattr(SessionService, "_do_open", _stub_do_open_write_on_cancel)

    sid = uuid.uuid4()
    await insert_session(
        db,
        SessionRecord(
            id=sid, alias="host-a", state=SessionState.STARTING, stage=SessionStage.VALIDATE
        ),
    )
    service._capacity_acquire(sid)

    task = asyncio.create_task(service._run_open(sid, "host-a"))
    service._start_tasks[sid] = task
    await asyncio.sleep(0.05)

    pre = await get_session(db, str(sid))
    assert pre is not None

    close_task = asyncio.create_task(service.close("host-a"))
    await asyncio.wait_for(wrote_identity.wait(), timeout=2.0)
    released = await _wait_for_capacity_release(service, sid)
    assert released
    await close_task

    assert await get_session(db, str(sid)) is None
    assert service._capacity_owned == set()
    assert listener is not None
    assert listener.closed is True
    assert service._tunnels.get(sid) is None

    assert task.cancelled() or task.done()


# ---------------------------------------------------------------------------
# Race 3: open cleanup completes before close re-reads
# ---------------------------------------------------------------------------


async def test_close_after_open_cleanup_is_noop_stop_no_double_release(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_happy_open_stubs(monkeypatch)

    cancel_trigger: asyncio.Event = asyncio.Event()

    original_set_remote = sessions_mod.set_remote_identity

    async def _intercept_set_remote(db_: aiosqlite.Connection, sid: str, *args: Any) -> None:
        await original_set_remote(db_, sid, *args)
        cancel_trigger.set()

    monkeypatch.setattr(sessions_mod, "set_remote_identity", _intercept_set_remote)

    sid = uuid.uuid4()
    await insert_session(
        db,
        SessionRecord(
            id=sid, alias="host-a", state=SessionState.STARTING, stage=SessionStage.VALIDATE
        ),
    )
    service._capacity_acquire(sid)

    task = asyncio.create_task(service._do_open(sid, "host-a"))
    await asyncio.wait_for(cancel_trigger.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Open cleanup stopped the remote and cleared persisted identity.
    settled = await get_session(db, str(sid))
    assert settled is not None
    assert settled.state == SessionState.ERROR
    assert settled.remote_pid is None
    assert state.stop_calls == [sid]
    assert sid in service._capacity_owned

    close_task = asyncio.create_task(service.close("host-a"))
    released = await _wait_for_capacity_release(service, sid)
    assert released
    await close_task

    assert await get_session(db, str(sid)) is None
    assert service._capacity_owned == set()
    assert state.stop_calls == [sid]


# ---------------------------------------------------------------------------
# Race 4: close against a ready session stops remote and tunnel
# ---------------------------------------------------------------------------


async def test_close_ready_session_stops_remote_and_tunnel(
    service: SessionService,
    db: aiosqlite.Connection,
    connection_service: _FakeConnectionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_happy_open_stubs(monkeypatch)

    sid = uuid.uuid4()
    record = SessionRecord(id=sid, alias="host-a", state=SessionState.READY, stage=None)
    await insert_session(db, record)
    await set_remote_identity(
        db,
        str(sid),
        4242,
        9876,
        "boot-abc",
        "psid-xyz",
        "/opt/openvscode/node",
    )
    await set_tunnel_identity(db, str(sid), 54321)
    service._capacity_acquire(sid)
    ssh_conn = await connection_service.connect_for_session(session_id=sid, alias="host-a")
    handle = cast(_FakeConnectionHandle, ssh_conn.conn)
    listener = _ConnectionBoundListener(handle)
    service._tunnels[sid] = sessions_mod._SessionTunnel(
        ssh_conn=ssh_conn,
        listener=cast(asyncssh.SSHListener, listener),
    )
    service._registry.add(sid, 54321)

    close_task = asyncio.create_task(service.close("host-a"))
    released = await _wait_for_capacity_release(service, sid)
    assert released
    await close_task

    assert await get_session(db, str(sid)) is None
    assert service._capacity_owned == set()
    assert listener.closed is True
    assert listener.waited is True
    assert handle.closed is True
    assert state.stop_calls == [sid]
    assert state.remove_calls == [sid]
    assert service._tunnels.get(sid) is None
    assert service._registry.lookup(sid) is None


async def test_close_host_key_failure_without_identities_does_not_reconnect(
    service: SessionService,
    settings: Settings,
    db: aiosqlite.Connection,
    connection_service: _FakeConnectionService,
) -> None:
    sid = uuid.uuid4()
    record = SessionRecord(
        id=sid,
        alias="host-a",
        state=SessionState.ERROR,
        stage=SessionStage.STOP,
        error_code=ErrorCode.SSH_HOST_UNKNOWN.value,
        error_message="Trust required",
    )
    await insert_session(db, record)
    service._capacity_acquire(sid)

    trust_service = HostTrustService(settings, db)
    await trust_service.record_challenge(
        session_id=sid,
        role="target",
        alias="host-a",
        host="host-a.example.test",
        port=22,
        algorithm="ssh-ed25519",
        fingerprint="SHA256:pending",
        public_key="ssh-ed25519 AAAApending",
    )
    known_hosts_before = settings.ssh_known_hosts_path.read_text(encoding="utf-8")

    await service.close("host-a")
    assert await _wait_for_capacity_release(service, sid)

    assert await get_session(db, str(sid)) is None
    assert await trust_service.get_challenge(sid) is None
    assert connection_service.connections == []
    assert settings.ssh_known_hosts_path.read_text(encoding="utf-8") == known_hosts_before


async def test_force_close_unreachable_session_releases_all_local_state(
    service: SessionService,
    settings: Settings,
    db: aiosqlite.Connection,
    connection_service: _FakeConnectionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = uuid.uuid4()
    record = SessionRecord(
        id=sid,
        alias="host-a",
        state=SessionState.ERROR,
        stage=SessionStage.STOP,
        remote_pid=4242,
        remote_port=9876,
        remote_boot_id="boot-abc",
        remote_process_start_id="psid-xyz",
        remote_executable="/opt/openvscode/node",
        error_code=ErrorCode.STOP_FAILED.value,
        error_message="Target unreachable",
    )
    await insert_session(db, record)
    service._capacity_acquire(sid)
    service._registry.add(sid, 54321)
    service._connected_counts[sid] = 2
    grace_timer = asyncio.create_task(asyncio.sleep(30))
    service._grace_timers[sid] = grace_timer

    trust_service = HostTrustService(settings, db)
    await trust_service.record_challenge(
        session_id=sid,
        role="target",
        alias="host-a",
        host="host-a.example.test",
        port=22,
        algorithm="ssh-ed25519",
        fingerprint="SHA256:pending",
        public_key="ssh-ed25519 AAAApending",
    )
    known_hosts_before = settings.ssh_known_hosts_path.read_text(encoding="utf-8")

    connect_attempts: list[SessionId] = []

    async def _fail_connect(
        *,
        session_id: SessionId,
        alias: str,
        role: HostKeyRole = "target",
    ) -> SshConnection:
        del alias, role
        connect_attempts.append(session_id)
        raise GatewayError(ErrorCode.SSH_UNREACHABLE, "Target unreachable", status_code=502)

    monkeypatch.setattr(connection_service, "connect_for_session", _fail_connect)

    workspace = (await service.get_workspaces_full())[0]
    assert workspace.can_force_close is True
    assert workspace.has_remote_identity is True

    warning_events: list[tuple[str, dict[str, object]]] = []

    class _TestLogger:
        def warning(self, event: str, **values: object) -> None:
            warning_events.append((event, values))

    monkeypatch.setattr(sessions_mod, "logger", _TestLogger())

    await service.close("host-a", force=True)
    await asyncio.sleep(0)

    assert connect_attempts == [sid]
    assert await get_session(db, str(sid)) is None
    assert await trust_service.get_challenge(sid) is None
    assert sid not in service._capacity_owned
    assert service._registry.lookup(sid) is None
    assert sid not in service._connected_counts
    assert sid not in service._grace_timers
    assert grace_timer.cancelled()
    assert settings.ssh_known_hosts_path.read_text(encoding="utf-8") == known_hosts_before
    assert warning_events == [
        (
            "session_force_closed",
            {
                "session_id": str(sid),
                "persisted_remote_identity": True,
                "remote_cleanup_confirmed": False,
                "cleanup_error_count": 1,
            },
        )
    ]

    await service.close("host-a", force=True)
    assert connect_attempts == [sid]

    service.on_client_connected(sid)
    service.on_client_disconnected(sid)
    assert sid not in service._connected_counts
    assert sid not in service._grace_timers

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
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import aiosqlite
import asyncssh
import pytest

from vscode_gateway import sessions as sessions_mod
from vscode_gateway.db import (
    get_session,
    insert_session,
    open_database,
    run_migrations,
    set_remote_identity,
    set_tunnel_identity,
)
from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.host_trust import HostTrustService
from vscode_gateway.models import (
    CatalogSnapshot,
    HostKeyRole,
    RuntimeCapabilities,
    RuntimeIdentity,
    SessionId,
    SessionRecord,
    SessionStage,
    SessionState,
)
from vscode_gateway.proxy import ProxyRegistry
from vscode_gateway.runtime import RuntimeService
from vscode_gateway.sessions import SessionService
from vscode_gateway.settings import Settings
from vscode_gateway.ssh_config import SshCatalog
from vscode_gateway.ssh_connection import (
    SshConnection,
    SshConnectionService,
    _HostKeyCapturer,
)


def _make_settings(tmp_path: Path, *, capacity: int = 10) -> Settings:
    return Settings(
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        ssh_dir=tmp_path / "ssh",
        ssh_config_path=tmp_path / "ssh" / "config",
        ssh_known_hosts_path=tmp_path / "ssh" / "known_hosts",
        ssh_keys_dir=tmp_path / "ssh" / "keys",
        password_hash_path=tmp_path / "state" / "password.hash",
        session_secret_path=tmp_path / "state" / "session.secret",
        session_capacity=capacity,
    )


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    conn = await open_database(tmp_path / "test.db")
    migrations_dir = Path(__file__).parent.parent.parent / "src" / "vscode_gateway" / "migrations"
    await run_migrations(conn, migrations_dir)
    yield conn
    await conn.close()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return _make_settings(tmp_path, capacity=4)


@pytest.fixture
def catalog(settings: Settings) -> SshCatalog:
    cat = SshCatalog(settings)
    cat.set_snapshot(
        CatalogSnapshot(
            revision="rev",
            aliases=("host-a",),
            loaded_at=datetime.now(UTC),
        )
    )
    return cat


@pytest.fixture
def runtime(settings: Settings) -> RuntimeService:
    return RuntimeService(settings)


class _FakeConnectionHandle:
    def __init__(self) -> None:
        self.closed = False
        self.waited = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


class _FakeListener:
    def __init__(self) -> None:
        self.closed = False
        self.waited = False
        self._closed = asyncio.Event()

    def close(self) -> None:
        self.closed = True
        self._closed.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()
        self.waited = True


class _FakeConnectionService(SshConnectionService):
    def __init__(self) -> None:
        self.connections: list[SshConnection] = []
        self.listeners: list[_FakeListener] = []

    async def connect_for_session(
        self,
        *,
        session_id: SessionId,
        alias: str,
        role: HostKeyRole = "target",
    ) -> SshConnection:
        del session_id, role
        handle = _FakeConnectionHandle()
        connection = SshConnection(
            conn=cast(asyncssh.SSHClientConnection, handle),
            listener=None,
            local_port=0,
            remote_port=0,
            alias=alias,
            capturer=_HostKeyCapturer(),
        )
        self.connections.append(connection)
        return connection

    async def forward_local_port(
        self,
        ssh_conn: SshConnection,
        remote_port: int,
    ) -> tuple[asyncssh.SSHListener, int]:
        listener = _FakeListener()
        self.listeners.append(listener)
        ssh_conn.listener = cast(asyncssh.SSHListener, listener)
        ssh_conn.local_port = 54321
        ssh_conn.remote_port = remote_port
        return cast(asyncssh.SSHListener, listener), 54321


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
    return SessionService(
        settings,
        db,
        catalog,
        runtime,
        ProxyRegistry(),
        connection_service,
        HostTrustService(settings, db),
    )


def _install_happy_open_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    remote: RuntimeIdentity | None = None,
    health_ok: bool = True,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "stop_calls": list[SessionId](),
        "remove_calls": list[SessionId](),
        "verify_calls": list[int](),
    }

    async def _capabilities(
        self: RuntimeService, connection: asyncssh.SSHClientConnection
    ) -> RuntimeCapabilities:
        del connection
        return RuntimeCapabilities(
            platform="linux", arch="x64", helper_version="v1", available=True
        )

    async def _ensure_installed(
        self: RuntimeService, connection: asyncssh.SSHClientConnection, platform: str
    ) -> None:
        del connection, platform
        return None

    async def _start_session(
        self: RuntimeService, connection: asyncssh.SSHClientConnection, session_id: SessionId
    ) -> RuntimeIdentity:
        del connection, session_id
        return remote or RuntimeIdentity(
            pid=4242,
            port=9876,
            boot_id="boot-abc",
            process_start_id="psid-xyz",
            executable="/opt/openvscode/node",
            session_dir="/tmp/ovs",
        )

    async def _stop_session(
        self: RuntimeService, connection: asyncssh.SSHClientConnection, session_id: SessionId
    ) -> bool:
        del connection
        state["stop_calls"].append(session_id)
        return True

    async def _remove_session(
        self: RuntimeService, connection: asyncssh.SSHClientConnection, session_id: SessionId
    ) -> bool:
        del connection
        state["remove_calls"].append(session_id)
        return True

    async def _inspect_session(
        self: RuntimeService,
        connection: asyncssh.SSHClientConnection,
        session_id: SessionId,
    ) -> dict[str, Any]:
        del connection, session_id
        return {"running": False}

    async def _verify(self: SessionService, session_id: SessionId, local_port: int) -> None:
        state["verify_calls"].append(local_port)
        if not health_ok:
            raise GatewayError(
                ErrorCode.EDITOR_UNHEALTHY,
                "Editor unhealthy (stub)",
                status_code=502,
            )

    monkeypatch.setattr(RuntimeService, "capabilities", _capabilities)
    monkeypatch.setattr(RuntimeService, "ensure_installed", _ensure_installed)
    monkeypatch.setattr(RuntimeService, "start_session", _start_session)
    monkeypatch.setattr(RuntimeService, "stop_session", _stop_session)
    monkeypatch.setattr(RuntimeService, "remove_session", _remove_session)
    monkeypatch.setattr(RuntimeService, "inspect_session", _inspect_session)
    monkeypatch.setattr(SessionService, "_verify_editor_health", _verify)
    return state


async def _wait_for_capacity_release(
    service: SessionService, sid: SessionId, timeout: float = 2.0
) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if sid not in service._capacity_owned:
            return True
        await asyncio.sleep(0.02)
    return sid not in service._capacity_owned


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
    assert state["stop_calls"] == [sid]
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
    state = _install_happy_open_stubs(monkeypatch)

    wrote_identity: asyncio.Event = asyncio.Event()

    async def _stub_do_open_write_on_cancel(
        self: SessionService, session_id: SessionId, alias: str
    ) -> Any:
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
            state["listener"] = listener
            await sessions_mod.set_tunnel_identity(db, str(session_id), 54321, 99999)
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
    assert pre.tunnel_pid is None

    close_task = asyncio.create_task(service.close("host-a"))
    await asyncio.wait_for(wrote_identity.wait(), timeout=2.0)
    released = await _wait_for_capacity_release(service, sid)
    assert released
    await close_task

    assert await get_session(db, str(sid)) is None
    assert service._capacity_owned == set()
    assert cast(_FakeListener, state["listener"]).closed is True
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
    assert state["stop_calls"] == [sid]
    assert sid in service._capacity_owned

    close_task = asyncio.create_task(service.close("host-a"))
    released = await _wait_for_capacity_release(service, sid)
    assert released
    await close_task

    assert await get_session(db, str(sid)) is None
    assert service._capacity_owned == set()
    assert state["stop_calls"] == [sid]


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
    await set_tunnel_identity(db, str(sid), 54321, 99999)
    service._capacity_acquire(sid)
    ssh_conn = await connection_service.connect_for_session(session_id=sid, alias="host-a")
    listener = _FakeListener()
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
    assert state["stop_calls"] == [sid]
    assert state["remove_calls"] == [sid]
    assert service._tunnels.get(sid) is None
    assert service._registry.lookup(sid) is None


# ---------------------------------------------------------------------------
# Race 5: capacity released exactly once in every close path
# ---------------------------------------------------------------------------


async def test_capacity_released_exactly_once_in_all_close_paths(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only close releases capacity in the cancel-then-close race."""
    state = _install_happy_open_stubs(monkeypatch)

    wrote_identity: asyncio.Event = asyncio.Event()

    async def _stub_do_open_write_on_cancel(
        self: SessionService, session_id: SessionId, alias: str
    ) -> Any:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
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
    assert service._capacity_owned == {sid}

    task = asyncio.create_task(service._run_open(sid, "host-a"))
    service._start_tasks[sid] = task
    await asyncio.sleep(0.05)

    close_task = asyncio.create_task(service.close("host-a"))
    await asyncio.wait_for(wrote_identity.wait(), timeout=2.0)
    released = await _wait_for_capacity_release(service, sid)
    assert released
    await close_task

    assert sid not in service._capacity_owned
    assert service._capacity_owned == set()
    assert state["stop_calls"] == [sid]

    assert task.cancelled() or task.done()


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

"""Unit tests for session-ID-keyed capacity accounting."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
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
    list_sessions,
    open_database,
    run_migrations,
)
from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.host_trust import HostTrustService
from vscode_gateway.models import (
    CatalogSnapshot,
    HostKeyRole,
    RecoveryReport,
    SessionId,
    SessionRecord,
    SessionStage,
    SessionState,
    SessionView,
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
    return _make_settings(tmp_path, capacity=2)


@pytest.fixture
def catalog(settings: Settings) -> SshCatalog:
    cat = SshCatalog(settings)
    cat.set_snapshot(
        CatalogSnapshot(
            revision="rev",
            aliases=("host-a", "host-b", "host-c"),
            loaded_at=datetime.now(UTC),
        )
    )
    return cat


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
def runtime(settings: Settings) -> RuntimeService:
    return RuntimeService(settings)


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


@pytest.fixture
def connection_service() -> _FakeConnectionService:
    return _FakeConnectionService()


def _make_record(
    alias: str,
    *,
    state: SessionState = SessionState.STARTING,
    stage: SessionStage | None = SessionStage.VALIDATE,
    sid: SessionId | None = None,
) -> SessionRecord:
    return SessionRecord(
        id=sid or uuid.uuid4(),
        alias=alias,
        state=state,
        stage=stage,
    )


def _running_inspection(*, pid: int = 4242) -> dict[str, Any]:
    return {
        "running": True,
        "identity_ok": True,
        "pid": pid,
        "port": 9876,
        "boot_id": "boot-abc",
        "process_start_id": "start-xyz",
        "executable": "/opt/openvscode/node",
        "session_dir": "/home/test/.vscode-gateway/sessions/test",
    }


def _ready_record(alias: str, sid: SessionId) -> SessionRecord:
    return SessionRecord(
        id=sid,
        alias=alias,
        state=SessionState.READY,
        remote_pid=4242,
        remote_port=9876,
        remote_boot_id="boot-abc",
        remote_process_start_id="start-xyz",
        remote_executable="/opt/openvscode/node",
        local_port=12345,
        tunnel_pid=0,
    )


def _install_reattach_stubs(monkeypatch: pytest.MonkeyPatch, *, pid: int = 4242) -> None:
    async def _inspect(
        self: RuntimeService,
        connection: asyncssh.SSHClientConnection,
        session_id: SessionId,
    ) -> dict[str, Any]:
        del self, connection, session_id
        return _running_inspection(pid=pid)

    async def _verify(self: SessionService, session_id: SessionId, local_port: int) -> None:
        del self, session_id, local_port

    monkeypatch.setattr(RuntimeService, "inspect_session", _inspect)
    monkeypatch.setattr(SessionService, "_verify_editor_health", _verify)


# ---------------------------------------------------------------------------
# Ledger primitives
# ---------------------------------------------------------------------------


def test_capacity_acquire_adds_session_id(service: SessionService) -> None:
    sid = uuid.uuid4()
    service._capacity_acquire(sid)
    assert sid in service._capacity_owned
    assert len(service._capacity_owned) == 1


def test_capacity_acquire_duplicate_is_idempotent(service: SessionService) -> None:
    sid = uuid.uuid4()
    service._capacity_acquire(sid)
    # Second acquire of the same id is logged but treated idempotently.
    service._capacity_acquire(sid)
    assert len(service._capacity_owned) == 1


def test_capacity_acquire_at_total_raises(service: SessionService, settings: Settings) -> None:
    total = settings.session_capacity
    for _ in range(total):
        service._capacity_acquire(uuid.uuid4())
    with pytest.raises(RuntimeError, match="capacity reached"):
        service._capacity_acquire(uuid.uuid4())
    # The failing acquire must not have added an entry.
    assert len(service._capacity_owned) == total


def test_capacity_release_without_acquire_is_noop(service: SessionService) -> None:
    sid = uuid.uuid4()
    service._capacity_release(sid)
    assert service._capacity_owned == set()


def test_capacity_release_is_idempotent(service: SessionService) -> None:
    sid = uuid.uuid4()
    service._capacity_acquire(sid)
    service._capacity_release(sid)
    # Releasing a second time must not under-count (was already removed).
    service._capacity_release(sid)
    assert service._capacity_owned == set()


# ---------------------------------------------------------------------------
# open() rollback paths
# ---------------------------------------------------------------------------


async def test_open_acquires_capacity_after_insert(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _noop_do_open(self: SessionService, session_id: SessionId, alias: str) -> SessionView:
        rec = await get_session(db, str(session_id))
        assert rec is not None
        return SessionView(
            id=rec.id,
            alias=rec.alias,
            state=rec.state,
        )

    monkeypatch.setattr(SessionService, "_do_open", _noop_do_open)

    view = await service.open("host-a")
    assert view.id in service._capacity_owned
    assert len(service._capacity_owned) == 1


async def test_open_insert_failure_rolls_back_capacity(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    async def _boom(_db: aiosqlite.Connection, _record: SessionRecord) -> None:
        raise RuntimeError("insert failed")

    monkeypatch.setattr("vscode_gateway.sessions.insert_session", _boom)

    with pytest.raises(RuntimeError, match="insert failed"):
        await service.open("host-a")

    # Capacity must not have been reserved when the row could not be
    # persisted. A future open must see an empty ledger.
    assert service._capacity_owned == set()


async def test_open_capacity_reached_blocks_new_session(
    service: SessionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fill capacity manually with two unrelated session ids (matching the
    # fixture capacity of 2).
    service._capacity_acquire(uuid.uuid4())
    service._capacity_acquire(uuid.uuid4())

    async def _noop_do_open(self: SessionService, session_id: SessionId, alias: str) -> SessionView:
        return SessionView(id=session_id, alias=alias, state=SessionState.STARTING)

    monkeypatch.setattr(SessionService, "_do_open", _noop_do_open)

    with pytest.raises(GatewayError) as exc_info:
        await service.open("host-a")
    assert exc_info.value.code == ErrorCode.CAPACITY_REACHED
    # The rejected open must not have changed the ledger.
    assert len(service._capacity_owned) == 2


# ---------------------------------------------------------------------------
# recover_all() capacity reconstruction
# ---------------------------------------------------------------------------


@pytest.fixture
def inspect_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, dict[str, Any]]]:
    """Per-alias inspect_session return values; default: remote not running."""
    responses: dict[str, dict[str, Any]] = {}

    async def _inspect(
        self: RuntimeService, connection: asyncssh.SSHClientConnection, session_id: SessionId
    ) -> dict[str, Any]:
        del connection, session_id
        return responses.get("default", {"running": False, "identity_ok": False, "port": 0})

    async def _remove(
        self: RuntimeService,
        connection: asyncssh.SSHClientConnection,
        session_id: SessionId,
    ) -> bool:
        del connection, session_id
        return True

    monkeypatch.setattr(RuntimeService, "inspect_session", _inspect)
    monkeypatch.setattr(RuntimeService, "remove_session", _remove)
    yield responses


async def test_recover_all_rebuilds_ledger_from_persisted_rows(
    service: SessionService,
    db: aiosqlite.Connection,
    inspect_stub: dict[str, dict[str, Any]],
) -> None:
    sid_a = uuid.uuid4()
    sid_b = uuid.uuid4()
    await insert_session(db, _make_record("host-a", sid=sid_a, state=SessionState.READY))
    await insert_session(db, _make_record("host-b", sid=sid_b, state=SessionState.STARTING))

    # READY becomes an error row. STARTING is deleted only after inspection
    # proves absence and remote metadata removal is confirmed.
    report = await service.recover_all()

    assert isinstance(report, RecoveryReport)
    assert report.cleaned == 1
    assert service._capacity_owned == {sid_a}


async def test_recover_all_releases_capacity_for_cleaned_rows(
    service: SessionService,
    db: aiosqlite.Connection,
    inspect_stub: dict[str, dict[str, Any]],
) -> None:
    sid_a = uuid.uuid4()
    await insert_session(db, _make_record("host-a", sid=sid_a, state=SessionState.ERROR))

    report = await service.recover_all()

    assert report.cleaned == 1
    assert service._capacity_owned == set()
    remaining = await list_sessions(db)
    assert remaining == []


async def test_recover_all_starting_running_no_port_retains_evidence(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Forward declaration to satisfy pyright about inspect_stub signature.
    async def _inspect(
        self: RuntimeService, connection: asyncssh.SSHClientConnection, session_id: SessionId
    ) -> dict[str, Any]:
        del connection, session_id
        return {"running": True, "identity_ok": True, "port": 0}

    monkeypatch.setattr(RuntimeService, "inspect_session", _inspect)

    sid = uuid.uuid4()
    await insert_session(db, _make_record("host-a", sid=sid, state=SessionState.STARTING))

    report = await service.recover_all()

    assert report.cleaned == 0
    assert report.failed == 1
    assert sid in service._capacity_owned
    row = await get_session(db, str(sid))
    assert row is not None
    assert row.state == SessionState.ERROR


async def test_max_sessions_enforced_after_recovery(
    settings: Settings,
    db: aiosqlite.Connection,
    catalog: SshCatalog,
    runtime: RuntimeService,
    connection_service: _FakeConnectionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Capacity equals the number of pre-existing rows so the next open must
    # be rejected.
    service = SessionService(
        settings,
        db,
        catalog,
        runtime,
        ProxyRegistry(),
        connection_service,
        HostTrustService(settings, db),
    )

    async def _inspect(
        self: RuntimeService, connection: asyncssh.SSHClientConnection, session_id: SessionId
    ) -> dict[str, Any]:
        del connection, session_id
        return {"running": True, "identity_ok": False, "port": 0}

    monkeypatch.setattr(RuntimeService, "inspect_session", _inspect)

    await insert_session(db, _make_record("host-a", state=SessionState.READY))
    await insert_session(db, _make_record("host-b", state=SessionState.STARTING))

    await service.recover_all()
    assert len(service._capacity_owned) == settings.session_capacity

    async def _noop_do_open(self: SessionService, session_id: SessionId, alias: str) -> SessionView:
        return SessionView(id=session_id, alias=alias, state=SessionState.STARTING)

    monkeypatch.setattr(SessionService, "_do_open", _noop_do_open)

    with pytest.raises(GatewayError) as exc_info:
        await service.open("host-c")
    assert exc_info.value.code == ErrorCode.CAPACITY_REACHED
    assert len(service._capacity_owned) == settings.session_capacity


# ---------------------------------------------------------------------------
# _do_close releases capacity only after the row is deleted
# ---------------------------------------------------------------------------


async def test_do_close_releases_capacity_on_success(
    service: SessionService,
    db: aiosqlite.Connection,
    connection_service: _FakeConnectionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = uuid.uuid4()
    record = _make_record("host-a", sid=sid, state=SessionState.READY)
    record.remote_pid = 1234
    await insert_session(db, record)
    service._capacity_acquire(sid)

    # Avoid touching real SSH/runtime helpers during the close path.
    async def _no_stop(
        self: RuntimeService, connection: asyncssh.SSHClientConnection, session_id: SessionId
    ) -> bool:
        del connection, session_id
        return True

    async def _no_remove(
        self: RuntimeService, connection: asyncssh.SSHClientConnection, session_id: SessionId
    ) -> bool:
        del connection, session_id
        return True

    monkeypatch.setattr(RuntimeService, "stop_session", _no_stop)
    monkeypatch.setattr(RuntimeService, "remove_session", _no_remove)

    await service._do_close(record)

    assert sid not in service._capacity_owned
    remaining = await list_sessions(db)
    assert remaining == []
    handle = cast(_FakeConnectionHandle, connection_service.connections[-1].conn)
    assert handle.closed is True
    assert handle.waited is True


async def test_recovery_inspection_failure_retains_error_row_and_capacity(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = uuid.uuid4()
    record = _make_record("host-a", sid=sid, state=SessionState.ERROR)
    record.error_code = ErrorCode.SSH_HOST_UNKNOWN.value
    record.error_message = "Trust required"
    record.remote_pid = 1234
    await insert_session(db, record)

    async def _inspect_failure(
        self: RuntimeService,
        connection: asyncssh.SSHClientConnection,
        session_id: SessionId,
    ) -> dict[str, Any]:
        del self, connection, session_id
        raise GatewayError(ErrorCode.SSH_UNREACHABLE, "temporary outage", status_code=502)

    monkeypatch.setattr(RuntimeService, "inspect_session", _inspect_failure)

    report = await service.recover_all()

    assert report.cleaned == 0
    assert report.failed == 1
    assert sid in service._capacity_owned
    remaining = await get_session(db, str(sid))
    assert remaining is not None
    assert remaining.error_code == ErrorCode.SSH_HOST_UNKNOWN.value
    assert remaining.remote_pid == 1234


async def test_recover_all_reattaches_same_session_and_process(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_reattach_stubs(monkeypatch)
    sid = uuid.uuid4()
    await insert_session(db, _ready_record("host-a", sid))

    report = await service.recover_all()

    assert report.recovered == 1
    assert report.failed == 0
    recovered = await get_session(db, str(sid))
    assert recovered is not None
    assert recovered.id == sid
    assert recovered.remote_pid == 4242
    assert recovered.state == SessionState.READY
    assert service._registry.lookup(sid) == 54321
    assert sid in service._tunnels
    await service.shutdown()


async def test_tunnel_exit_reconnects_with_same_session_id(
    service: SessionService,
    db: aiosqlite.Connection,
    connection_service: _FakeConnectionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_reattach_stubs(monkeypatch)
    sid = uuid.uuid4()
    await insert_session(db, _ready_record("host-a", sid))
    service._capacity_acquire(sid)

    old_connection = await connection_service.connect_for_session(session_id=sid, alias="host-a")
    old_listener = _FakeListener()
    old_tunnel = sessions_mod._SessionTunnel(
        ssh_conn=old_connection,
        listener=cast(asyncssh.SSHListener, old_listener),
    )
    service._tunnels[sid] = old_tunnel
    service._registry.add(sid, 12345)

    await service.on_tunnel_exit(sid, 0, expected_tunnel=old_tunnel)

    recovered = await get_session(db, str(sid))
    assert recovered is not None
    assert recovered.id == sid
    assert recovered.state == SessionState.READY
    assert recovered.remote_pid == 4242
    assert recovered.local_port == 54321
    assert recovered.error_code is None
    assert service._registry.lookup(sid) == 54321
    assert service._tunnels[sid] is not old_tunnel
    old_handle = cast(_FakeConnectionHandle, old_connection.conn)
    assert old_listener.closed is True
    assert old_handle.closed is True
    assert len(connection_service.connections) == 2
    await service.shutdown()


async def test_tunnel_exit_retries_after_temporary_connect_failure(
    service: SessionService,
    db: aiosqlite.Connection,
    connection_service: _FakeConnectionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_reattach_stubs(monkeypatch)
    monkeypatch.setattr(sessions_mod, "_RECOVERY_RETRY_INITIAL_DELAY", 0.0)
    sid = uuid.uuid4()
    await insert_session(db, _ready_record("host-a", sid))
    service._capacity_acquire(sid)

    old_connection = await connection_service.connect_for_session(session_id=sid, alias="host-a")
    old_tunnel = sessions_mod._SessionTunnel(
        ssh_conn=old_connection,
        listener=cast(asyncssh.SSHListener, _FakeListener()),
    )
    service._tunnels[sid] = old_tunnel

    original_connect = connection_service.connect_for_session
    attempts = 0

    async def _flaky_connect(
        *,
        session_id: SessionId,
        alias: str,
        role: HostKeyRole = "target",
    ) -> SshConnection:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise GatewayError(ErrorCode.SSH_UNREACHABLE, "temporary outage", status_code=502)
        return await original_connect(session_id=session_id, alias=alias, role=role)

    monkeypatch.setattr(connection_service, "connect_for_session", _flaky_connect)

    await service.on_tunnel_exit(sid, 0, expected_tunnel=old_tunnel)

    recovered = await get_session(db, str(sid))
    assert attempts == 2
    assert recovered is not None
    assert recovered.id == sid
    assert recovered.state == SessionState.READY
    assert service._registry.lookup(sid) == 54321
    await service.shutdown()


async def test_stale_tunnel_watcher_cannot_replace_current_tunnel(
    service: SessionService,
    db: aiosqlite.Connection,
    connection_service: _FakeConnectionService,
) -> None:
    sid = uuid.uuid4()
    await insert_session(db, _ready_record("host-a", sid))
    old_connection = await connection_service.connect_for_session(session_id=sid, alias="host-a")
    replacement_connection = await connection_service.connect_for_session(
        session_id=sid, alias="host-a"
    )
    old_tunnel = sessions_mod._SessionTunnel(ssh_conn=old_connection)
    replacement = sessions_mod._SessionTunnel(ssh_conn=replacement_connection)
    service._tunnels[sid] = replacement
    service._registry.add(sid, 54321)

    await service.on_tunnel_exit(sid, 0, expected_tunnel=old_tunnel)

    assert service._tunnels[sid] is replacement
    assert service._registry.lookup(sid) == 54321
    assert cast(_FakeConnectionHandle, replacement_connection.conn).closed is False
    await service._close_ssh_connection(old_connection)
    await service.shutdown()


async def test_tunnel_exit_rejects_changed_remote_process_identity(
    service: SessionService,
    db: aiosqlite.Connection,
    connection_service: _FakeConnectionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_reattach_stubs(monkeypatch, pid=9999)
    sid = uuid.uuid4()
    await insert_session(db, _ready_record("host-a", sid))
    service._capacity_acquire(sid)
    old_connection = await connection_service.connect_for_session(session_id=sid, alias="host-a")
    old_tunnel = sessions_mod._SessionTunnel(
        ssh_conn=old_connection,
        listener=cast(asyncssh.SSHListener, _FakeListener()),
    )
    service._tunnels[sid] = old_tunnel

    await service.on_tunnel_exit(sid, 0, expected_tunnel=old_tunnel)

    failed = await get_session(db, str(sid))
    assert failed is not None
    assert failed.state == SessionState.ERROR
    assert failed.error_code == ErrorCode.TUNNEL_LOST.value
    assert failed.remote_pid == 4242
    assert service._registry.lookup(sid) is None
    assert service._tunnels.get(sid) is None


async def test_retry_reattaches_alive_process_without_changing_session_id(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_reattach_stubs(monkeypatch)
    sid = uuid.uuid4()
    record = _ready_record("host-a", sid)
    record.state = SessionState.ERROR
    record.stage = SessionStage.RECOVER
    record.error_code = ErrorCode.TUNNEL_LOST.value
    record.error_message = "network failed"
    await insert_session(db, record)
    service._capacity_acquire(sid)

    async def _unexpected_start(
        self: RuntimeService,
        connection: asyncssh.SSHClientConnection,
        session_id: SessionId,
    ) -> Any:
        del self, connection, session_id
        raise AssertionError("retry must not start a second remote process")

    monkeypatch.setattr(RuntimeService, "start_session", _unexpected_start)

    retried = await service.retry("host-a")

    assert retried.id == sid
    assert retried.state == SessionState.READY
    recovered = await get_session(db, str(sid))
    assert recovered is not None
    assert recovered.id == sid
    assert recovered.remote_pid == 4242
    await service.shutdown()


async def test_retry_does_not_stop_a_changed_remote_process(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_reattach_stubs(monkeypatch, pid=9999)
    sid = uuid.uuid4()
    record = _ready_record("host-a", sid)
    record.state = SessionState.ERROR
    record.error_code = ErrorCode.TUNNEL_LOST.value
    await insert_session(db, record)
    service._capacity_acquire(sid)
    stop_calls: list[SessionId] = []

    async def _stop(
        self: RuntimeService,
        connection: asyncssh.SSHClientConnection,
        session_id: SessionId,
    ) -> bool:
        del self, connection
        stop_calls.append(session_id)
        return True

    monkeypatch.setattr(RuntimeService, "stop_session", _stop)

    with pytest.raises(GatewayError) as exc_info:
        await service.retry("host-a")

    assert exc_info.value.code == ErrorCode.RECOVERY_FAILED
    assert stop_calls == []
    retained = await get_session(db, str(sid))
    assert retained is not None
    assert retained.id == sid
    assert retained.remote_pid == 4242
    assert retained.state == SessionState.ERROR


async def test_do_close_retains_capacity_on_cleanup_failure(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = uuid.uuid4()
    record = _make_record(
        "host-a",
        sid=sid,
        state=SessionState.READY,
    )
    record.remote_pid = 1234  # makes _do_close attempt remote stoppage
    await insert_session(db, record)
    service._capacity_acquire(sid)

    async def _fail_stop(
        self: RuntimeService, connection: asyncssh.SSHClientConnection, session_id: SessionId
    ) -> bool:
        del connection, session_id
        raise GatewayError(ErrorCode.STOP_FAILED, "remote stop failed", status_code=500)

    monkeypatch.setattr(RuntimeService, "stop_session", _fail_stop)

    await service._do_close(record)

    # Row is marked error (kept) and capacity must remain owned.
    assert sid in service._capacity_owned
    sessions = await list_sessions(db)
    assert any(s.id == sid and s.state == SessionState.ERROR for s in sessions)

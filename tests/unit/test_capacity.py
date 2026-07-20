"""Unit tests for session-ID-keyed capacity accounting (HI-01 correction)."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from vscode_gateway.db import (
    get_session,
    insert_session,
    list_sessions,
    open_database,
    run_migrations,
)
from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.models import (
    CatalogSnapshot,
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
from vscode_gateway.ssh import SshCatalog


def _make_settings(tmp_path: Path, *, capacity: int = 10) -> Settings:
    return Settings(
        state_dir=tmp_path / "state",
        runtime_dir=tmp_path / "runtime",
        ssh_config_path=tmp_path / "ssh_config",
        ssh_keys_dir=tmp_path / "keys",
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


@pytest.fixture
def runtime(settings: Settings) -> RuntimeService:
    return RuntimeService(settings)


@pytest.fixture
def service(
    settings: Settings,
    db: aiosqlite.Connection,
    catalog: SshCatalog,
    runtime: RuntimeService,
) -> SessionService:
    return SessionService(settings, db, catalog, runtime, ProxyRegistry())


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

    async def _inspect(self: RuntimeService, alias: str, session_id: SessionId) -> dict[str, Any]:
        return responses.get(alias, {"running": False, "identity_ok": False, "port": 0})

    monkeypatch.setattr(RuntimeService, "inspect_session", _inspect)
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

    # Both rows remain (READY→error, STARTING→error). Capacity ledger must
    # contain every persisted session id.
    report = await service.recover_all()

    assert isinstance(report, RecoveryReport)
    assert service._capacity_owned == {sid_a, sid_b}
    assert len(service._capacity_owned) == 2


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


async def test_recover_all_starting_running_no_port_releases(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Forward declaration to satisfy pyright about inspect_stub signature.
    async def _inspect(self: RuntimeService, alias: str, session_id: SessionId) -> dict[str, Any]:
        return {"running": True, "identity_ok": True, "port": 0}

    monkeypatch.setattr(RuntimeService, "inspect_session", _inspect)

    sid = uuid.uuid4()
    await insert_session(db, _make_record("host-a", sid=sid, state=SessionState.STARTING))

    report = await service.recover_all()

    assert report.cleaned == 1
    assert sid not in service._capacity_owned


async def test_max_sessions_enforced_after_recovery(
    settings: Settings,
    db: aiosqlite.Connection,
    catalog: SshCatalog,
    runtime: RuntimeService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Capacity equals the number of pre-existing rows so the next open must
    # be rejected.
    service = SessionService(settings, db, catalog, runtime, ProxyRegistry())

    async def _inspect(self: RuntimeService, alias: str, session_id: SessionId) -> dict[str, Any]:
        return {"running": False, "identity_ok": False, "port": 0}

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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sid = uuid.uuid4()
    record = _make_record("host-a", sid=sid, state=SessionState.READY)
    await insert_session(db, record)
    service._capacity_acquire(sid)

    # Avoid touching real SSH/runtime helpers during the close path.
    async def _no_stop(self: RuntimeService, alias: str, session_id: SessionId) -> bool:
        return True

    async def _no_remove(self: RuntimeService, alias: str, session_id: SessionId) -> None:
        return None

    monkeypatch.setattr(RuntimeService, "stop_session", _no_stop)
    monkeypatch.setattr(RuntimeService, "remove_session", _no_remove)

    await service._do_close(record)

    assert sid not in service._capacity_owned
    remaining = await list_sessions(db)
    assert remaining == []


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

    async def _fail_stop(self: RuntimeService, alias: str, session_id: SessionId) -> bool:
        raise GatewayError(ErrorCode.STOP_FAILED, "remote stop failed", status_code=500)

    monkeypatch.setattr(RuntimeService, "stop_session", _fail_stop)

    await service._do_close(record)

    # Row is marked error (kept) and capacity must remain owned.
    assert sid in service._capacity_owned
    sessions = await list_sessions(db)
    assert any(s.id == sid and s.state == SessionState.ERROR for s in sessions)

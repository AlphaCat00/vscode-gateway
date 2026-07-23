"""Cancellation-safe and exception-safe ``_do_open`` tests.

These tests exercise the operation-local resource ledger, the reverse-order
cleanup on failure, the sanitized ``internal_error`` row written by the
generic exception path, the CancelledError cleanup-and-reraise contract,
and the shielding of the final ``mark_ready`` transaction.
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
from vscode_gateway import sessions as sessions_mod
from vscode_gateway.db import (
    get_session,
    insert_session,
)
from vscode_gateway.db import (
    mark_ready as db_mark_ready,
)
from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.models import (
    RuntimeIdentity,
    SessionId,
    SessionRecord,
    SessionStage,
    SessionState,
)
from vscode_gateway.runtime import RuntimeService
from vscode_gateway.sessions import SessionService
from vscode_gateway.settings import Settings
from vscode_gateway.ssh_config import SshCatalog


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path, capacity=4)


@pytest.fixture
def catalog(settings: Settings) -> SshCatalog:
    return make_catalog(settings, ("host-a",))


@pytest.fixture
def runtime(settings: Settings) -> RuntimeService:
    return RuntimeService(settings)


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


# ---------------------------------------------------------------------------
# Generic exception path
# ---------------------------------------------------------------------------


async def test_runtime_error_marks_internal_error_row(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-GatewayError raised inside ``_do_open`` is converted into an
    ``error`` row with ``internal_error`` code, fully cleaned resources, and
    capacity that remains owned (row persists)."""
    state = _install_happy_open_stubs(monkeypatch)

    async def _boom(
        self: RuntimeService,
        connection: asyncssh.SSHClientConnection,
        session_id: SessionId,
        alias: str,
    ) -> RuntimeIdentity:
        del connection, session_id, alias
        raise RuntimeError("synthetic bug in remote helper")

    monkeypatch.setattr(RuntimeService, "start_session", _boom)

    sid = uuid.uuid4()
    record = SessionRecord(
        id=sid, alias="host-a", state=SessionState.STARTING, stage=SessionStage.VALIDATE
    )
    await insert_session(db, record)
    service._capacity_acquire(sid)

    with pytest.raises(RuntimeError, match="synthetic bug"):
        await service._do_open(sid, "host-a")

    row = await get_session(db, str(sid))
    assert row is not None
    assert row.state == SessionState.ERROR
    assert row.error_code == ErrorCode.INTERNAL_ERROR.value
    assert row.stage == SessionStage.STOP.value

    assert sid in service._capacity_owned
    assert len(service._capacity_owned) == 1

    assert service._tunnels.get(sid) is None
    assert state.stop_calls == []
    assert service._registry.lookup(sid) is None


async def test_runtime_error_releases_capacity_after_close(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the error path keeps the row, ``_do_close`` reclaims resources
    and releases the capacity reservation exactly once."""
    _install_happy_open_stubs(monkeypatch)

    async def _boom(
        self: RuntimeService,
        connection: asyncssh.SSHClientConnection,
        session_id: SessionId,
        alias: str,
    ) -> RuntimeIdentity:
        del connection, session_id, alias
        raise RuntimeError("synthetic")

    async def _stop(
        self: RuntimeService, connection: asyncssh.SSHClientConnection, session_id: SessionId
    ) -> bool:
        del connection, session_id
        return True

    async def _remove(
        self: RuntimeService, connection: asyncssh.SSHClientConnection, session_id: SessionId
    ) -> bool:
        del connection, session_id
        return True

    monkeypatch.setattr(RuntimeService, "start_session", _boom)
    monkeypatch.setattr(RuntimeService, "stop_session", _stop)
    monkeypatch.setattr(RuntimeService, "remove_session", _remove)

    sid = uuid.uuid4()
    record = SessionRecord(
        id=sid, alias="host-a", state=SessionState.STARTING, stage=SessionStage.VALIDATE
    )
    await insert_session(db, record)
    service._capacity_acquire(sid)

    with pytest.raises(RuntimeError):
        await service._do_open(sid, "host-a")

    assert sid in service._capacity_owned
    assert len(service._capacity_owned) == 1

    await service.close("host-a")
    for _ in range(20):
        if sid not in service._capacity_owned:
            break
        await asyncio.sleep(0.02)
    assert sid not in service._capacity_owned
    assert await get_session(db, str(sid)) is None
    assert service._capacity_owned == set()


# ---------------------------------------------------------------------------
# Cancellation paths
# ---------------------------------------------------------------------------


async def test_cancel_after_remote_triggers_cleanup_and_reraises(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CancelledError after the remote is created (before mark_ready) triggers
    best-effort cleanup of the remote, marks an ``error`` row, and re-raises.

    The row persists when cleanup cannot prove safety, so capacity remains
    owned until close or retry reclaims it.
    """
    state = _install_happy_open_stubs(monkeypatch)
    cancel_trigger: asyncio.Event = asyncio.Event()

    original_set_remote = sessions_mod.set_remote_identity

    async def _intercept_set_remote(db_: aiosqlite.Connection, sid: str, *args: Any) -> None:
        await original_set_remote(db_, sid, *args)
        cancel_trigger.set()

    monkeypatch.setattr(sessions_mod, "set_remote_identity", _intercept_set_remote)

    sid = uuid.uuid4()
    record = SessionRecord(
        id=sid, alias="host-a", state=SessionState.STARTING, stage=SessionStage.VALIDATE
    )
    await insert_session(db, record)
    service._capacity_acquire(sid)

    task = asyncio.create_task(service._do_open(sid, "host-a"))
    await asyncio.wait_for(cancel_trigger.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = await get_session(db, str(sid))
    assert row is not None
    assert row.state == SessionState.ERROR
    assert row.error_code == ErrorCode.INTERNAL_ERROR.value
    assert state.stop_calls == [sid]
    assert service._tunnels.get(sid) is None
    assert service._registry.lookup(sid) is None
    assert sid in service._capacity_owned


async def test_capacity_released_exactly_once_after_cancel_then_close(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Across the cancel-then-close failure path, capacity is released exactly
    once (in ``_do_close``), never in the open cleanup, never twice."""
    _install_happy_open_stubs(monkeypatch)
    cancel_trigger: asyncio.Event = asyncio.Event()

    async def _stop(
        self: RuntimeService, connection: asyncssh.SSHClientConnection, session_id: SessionId
    ) -> bool:
        del connection, session_id
        return True

    async def _remove(
        self: RuntimeService, connection: asyncssh.SSHClientConnection, session_id: SessionId
    ) -> bool:
        del connection, session_id
        return True

    monkeypatch.setattr(RuntimeService, "stop_session", _stop)
    monkeypatch.setattr(RuntimeService, "remove_session", _remove)

    async def _intercept_set_remote(db_: aiosqlite.Connection, sid: str, *args: Any) -> None:
        cancel_trigger.set()

    monkeypatch.setattr(sessions_mod, "set_remote_identity", _intercept_set_remote)

    sid = uuid.uuid4()
    record = SessionRecord(
        id=sid, alias="host-a", state=SessionState.STARTING, stage=SessionStage.VALIDATE
    )
    await insert_session(db, record)
    service._capacity_acquire(sid)

    task = asyncio.create_task(service._do_open(sid, "host-a"))
    await asyncio.wait_for(cancel_trigger.wait(), timeout=2.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sid in service._capacity_owned

    await service.close("host-a")
    for _ in range(20):
        if sid not in service._capacity_owned:
            break
        await asyncio.sleep(0.02)
    assert sid not in service._capacity_owned
    assert service._capacity_owned == set()


# ---------------------------------------------------------------------------
# mark_ready shielding
# ---------------------------------------------------------------------------


async def test_mark_ready_transaction_is_shielded_from_cancellation(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation arriving while ``mark_ready`` is in flight must not abort
    the commit; the shielded transaction completes and the row reaches
    ``ready``. Live resources and capacity are preserved for the now-ready
    session so close()/retry() remains the sole owner of teardown.
    """
    _install_happy_open_stubs(monkeypatch)

    in_mark_ready: asyncio.Event = asyncio.Event()
    proceed: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def _blocking_mark_ready(db_: aiosqlite.Connection, sid: str) -> None:
        in_mark_ready.set()
        # The fake commits only after the test releases `proceed`. This
        # is wrapped as an independent Task via asyncio.ensure_future by
        # the production code, so cancelling the open task does not
        # cancel this inner task and the await proceeds.
        await proceed
        await db_mark_ready(db_, sid)

    monkeypatch.setattr(sessions_mod, "mark_ready", _blocking_mark_ready)

    sid = uuid.uuid4()
    record = SessionRecord(
        id=sid, alias="host-a", state=SessionState.STARTING, stage=SessionStage.VALIDATE
    )
    await insert_session(db, record)
    service._capacity_acquire(sid)

    task = asyncio.create_task(service._do_open(sid, "host-a"))
    await asyncio.wait_for(in_mark_ready.wait(), timeout=2.0)

    pre_row = await get_session(db, str(sid))
    assert pre_row is not None
    assert pre_row.state == SessionState.STARTING

    task.cancel()
    await asyncio.sleep(0.05)
    still_starting = await get_session(db, str(sid))
    assert still_starting is not None
    assert still_starting.state == SessionState.STARTING

    proceed.set_result(None)
    row: SessionRecord | None = None
    for _ in range(50):
        row = await get_session(db, str(sid))
        if row is not None and row.state == SessionState.READY:
            break
        await asyncio.sleep(0.02)
    assert row is not None
    assert row.state == SessionState.READY

    with pytest.raises(asyncio.CancelledError):
        await task

    tunnel = service._tunnels.get(sid)
    assert tunnel is not None
    assert tunnel.listener is not None
    assert service._registry.lookup(sid) == 54321
    assert sid in service._capacity_owned
    assert not cast(_FakeListener, tunnel.listener).closed


async def test_gateway_error_path_does_not_double_release_capacity(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GatewayError from a normal ``_do_open`` stage keeps the row in
    ``error`` and capacity owned; there is no release in the open path.
    Closing afterwards releases exactly once."""
    _install_happy_open_stubs(monkeypatch, health_ok=False)

    async def _stop(
        self: RuntimeService, connection: asyncssh.SSHClientConnection, session_id: SessionId
    ) -> bool:
        del connection, session_id
        return True

    async def _remove(
        self: RuntimeService, connection: asyncssh.SSHClientConnection, session_id: SessionId
    ) -> bool:
        del connection, session_id
        return True

    monkeypatch.setattr(RuntimeService, "stop_session", _stop)
    monkeypatch.setattr(RuntimeService, "remove_session", _remove)

    sid = uuid.uuid4()
    record = SessionRecord(
        id=sid, alias="host-a", state=SessionState.STARTING, stage=SessionStage.VALIDATE
    )
    await insert_session(db, record)
    service._capacity_acquire(sid)

    with pytest.raises(GatewayError) as exc_info:
        await service._do_open(sid, "host-a")
    assert exc_info.value.code == ErrorCode.EDITOR_UNHEALTHY

    row = await get_session(db, str(sid))
    assert row is not None
    assert row.state == SessionState.ERROR
    assert row.error_code == ErrorCode.EDITOR_UNHEALTHY.value
    assert sid in service._capacity_owned

    # Tunnel was acquired and must have been torn down in cleanup reverse order.
    assert service._tunnels.get(sid) is None
    assert service._registry.lookup(sid) is None

    await service.close("host-a")
    for _ in range(20):
        if sid not in service._capacity_owned:
            break
        await asyncio.sleep(0.02)
    assert sid not in service._capacity_owned
    assert service._capacity_owned == set()


# ---------------------------------------------------------------------------
# _run_open defensive wrapper
# ---------------------------------------------------------------------------


async def test_run_open_converts_unhandled_bug_into_error_row(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a programming bug raises before ``_do_open`` finishes cleanup, the
    task wrapper still converts the residual ``starting`` row into a sanitized
    ``internal_error`` row."""

    async def _broken_do_open(self: SessionService, session_id: SessionId, alias: str) -> Any:
        raise RuntimeError("bug inside _do_open before any cleanup ran")

    monkeypatch.setattr(SessionService, "_do_open", _broken_do_open)

    sid = uuid.uuid4()
    record = SessionRecord(
        id=sid, alias="host-a", state=SessionState.STARTING, stage=SessionStage.VALIDATE
    )
    await insert_session(db, record)
    service._capacity_acquire(sid)

    await service._run_open(sid, "host-a")

    row = await get_session(db, str(sid))
    assert row is not None
    assert row.state == SessionState.ERROR
    assert row.error_code == ErrorCode.INTERNAL_ERROR.value
    assert row.stage == SessionStage.STOP.value
    # The defensive wrapper must not have leaked the task entry.
    assert sid not in service._start_tasks

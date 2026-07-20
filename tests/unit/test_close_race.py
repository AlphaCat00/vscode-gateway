"""Unit tests for HI-03: close/start race correctness.

These tests exercise the close()/_do_close refactor that re-reads the
session row by session id after cancelling and awaiting the start task
(Plan §14.2 steps 3-6). They cover the four required race outcomes:

* the start task persists remote identity after close()'s stale snapshot
  but before cancellation is observed; close re-reads and stops the
  remote, then deletes the row and releases capacity exactly once;
* the same race for tunnel identity;
* the start task completes HI-02's ``_open_failure_cleanup`` (clearing
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
from typing import Any

import aiosqlite
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
from vscode_gateway.models import (
    CatalogSnapshot,
    RuntimeCapabilities,
    RuntimeIdentity,
    SessionId,
    SessionRecord,
    SessionStage,
    SessionState,
    TunnelIdentity,
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


@pytest.fixture
def service(
    settings: Settings,
    db: aiosqlite.Connection,
    catalog: SshCatalog,
    runtime: RuntimeService,
) -> SessionService:
    return SessionService(settings, db, catalog, runtime, ProxyRegistry())


class _FakeTunnelProc:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated: bool = False
        self.killed: bool = False
        self._waiters: list[asyncio.Future[int]] = []

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        for w in self._waiters:
            if not w.done():
                w.set_result(self.returncode)

    def kill(self) -> None:
        self.killed = True
        self.terminated = True
        self.returncode = -9
        for w in self._waiters:
            if not w.done():
                w.set_result(self.returncode)

    async def wait(self) -> int:
        if self.returncode is not None:
            return self.returncode
        fut: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        return await fut


def _install_happy_open_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tunnel_proc: _FakeTunnelProc | None = None,
    remote: RuntimeIdentity | None = None,
    health_ok: bool = True,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "tunnel_proc": tunnel_proc or _FakeTunnelProc(),
        "stop_calls": list[SessionId](),
        "remove_calls": list[SessionId](),
        "verify_calls": list[int](),
    }

    async def _capabilities(self: RuntimeService, alias: str) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            platform="linux", arch="x64", helper_version="v1", available=True
        )

    async def _ensure_installed(self: RuntimeService, alias: str, platform: str) -> None:
        return None

    async def _start_session(
        self: RuntimeService, alias: str, session_id: SessionId
    ) -> RuntimeIdentity:
        return remote or RuntimeIdentity(
            pid=4242,
            port=9876,
            boot_id="boot-abc",
            process_start_id="psid-xyz",
            executable="/opt/openvscode/node",
            session_dir="/tmp/ovs",
        )

    async def _stop_session(self: RuntimeService, alias: str, session_id: SessionId) -> bool:
        state["stop_calls"].append(session_id)
        return True

    async def _remove_session(self: RuntimeService, alias: str, session_id: SessionId) -> None:
        state["remove_calls"].append(session_id)
        return None

    async def _start_local_forward(
        settings: Settings, alias: str, remote_port: int
    ) -> tuple[TunnelIdentity, _FakeTunnelProc]:
        identity = TunnelIdentity(local_port=54321, pid=99999)
        return identity, state["tunnel_proc"]

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
    monkeypatch.setattr(sessions_mod, "start_local_forward", _start_local_forward)
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


# ---------------------------------------------------------------------------
# Race 1: start task persists remote identity after close's stale snapshot
# ---------------------------------------------------------------------------


async def test_close_rereads_after_cancelling_start_task_and_stops_remote(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Race HI-03: the start task writes remote identity AFTER close()'s
    stale snapshot is captured but BEFORE cancellation is observed (here
    by writing it inside the CancelledError handler). Close awaits the
    start task, then ``_do_close`` re-reads the row, sees the persisted
    remote_pid, and stops the remote. Row is deleted and capacity released
    exactly once.
    """
    state = _install_happy_open_stubs(monkeypatch)

    wrote_identity: asyncio.Event = asyncio.Event()

    async def _stub_do_open_write_on_cancel(
        self: SessionService, session_id: SessionId, alias: str
    ) -> Any:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            # Simulate: identity was persisted after close's stale read
            # but before cancellation is observed. Bypass HI-02 cleanup
            # (no stop_session here) so close must defensively reclaim.
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
    # Let the start task enter its long sleep (no identity written yet).
    await asyncio.sleep(0.05)

    # The row currently has no remote identity — close()'s stale snapshot
    # would observe this in the buggy original path.
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Race HI-03 (tunnel variant): the start task writes tunnel identity
    after close() captured its stale snapshot. Close awaits the start
    task, ``_do_close`` re-reads, pops the in-process tunnel handle, and
    terminates it. Row is deleted and capacity released exactly once.
    """
    state = _install_happy_open_stubs(monkeypatch)
    tunnel_proc = state["tunnel_proc"]

    wrote_identity: asyncio.Event = asyncio.Event()

    async def _stub_do_open_write_on_cancel(
        self: SessionService, session_id: SessionId, alias: str
    ) -> Any:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self._tunnel_processes[session_id] = tunnel_proc
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
    assert tunnel_proc.terminated is True
    assert service._tunnel_processes.get(sid) is None

    assert task.cancelled() or task.done()


# ---------------------------------------------------------------------------
# Race 3: start task completes HI-02 _open_failure_cleanup before close re-reads
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

    # HI-02 path: stopped the remote once and cleared persisted identity.
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _install_happy_open_stubs(monkeypatch)
    tunnel_proc = state["tunnel_proc"]

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
    service._tunnel_processes[sid] = tunnel_proc
    service._registry.add(sid, 54321)

    close_task = asyncio.create_task(service.close("host-a"))
    released = await _wait_for_capacity_release(service, sid)
    assert released
    await close_task

    assert await get_session(db, str(sid)) is None
    assert service._capacity_owned == set()
    assert tunnel_proc.terminated is True
    assert state["stop_calls"] == [sid]
    assert state["remove_calls"] == [sid]
    assert service._tunnel_processes.get(sid) is None
    assert service._registry.lookup(sid) is None


# ---------------------------------------------------------------------------
# Race 5: capacity released exactly once in every close path
# ---------------------------------------------------------------------------


async def test_capacity_released_exactly_once_in_all_close_paths(
    service: SessionService,
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capacity is released exactly once across the cancel-then-close race
    path. The start task does not release; HI-02 cleanup does not release
    (it only stops resources and clears identity); only ``_do_close``
    releases after the row is deleted (Plan §14.6). stub_tracks two
    stop_session calls would indicate a double-stop bug.
    """
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

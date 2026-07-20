from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite
import structlog

from vscode_gateway.db import (
    delete_session,
    get_session,
    get_session_by_alias,
    insert_session,
    list_sessions,
    mark_error,
    mark_ready,
    mark_stopping,
    set_disconnect_deadline,
    set_last_connected,
    set_remote_identity,
    set_tunnel_identity,
    update_connected_clients,
    update_session_stage,
)
from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.models import (
    CloseReason,
    RecoveryReport,
    SessionId,
    SessionRecord,
    SessionStage,
    SessionState,
    SessionView,
    WorkspaceView,
)
from vscode_gateway.proxy import ProxyRegistry
from vscode_gateway.runtime import RuntimeService
from vscode_gateway.settings import Settings
from vscode_gateway.ssh import SshCatalog, start_local_forward


class SessionService:
    def __init__(
        self,
        settings: Settings,
        db: aiosqlite.Connection,
        catalog: SshCatalog,
        runtime: RuntimeService,
        proxy_registry: ProxyRegistry,
    ) -> None:
        self._settings = settings
        self._db = db
        self._catalog = catalog
        self._runtime = runtime
        self._registry = proxy_registry

        self._alias_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Capacity ownership ledger keyed by session ID. Per Plan §14.6 the
        # ledger is the source of truth for who owns a slot; an anonymous
        # integer cannot be rolled back safely on failure or rebuilt from
        # persisted rows during recovery.
        self._capacity_total: int = settings.session_capacity
        self._capacity_owned: set[SessionId] = set()

        self._tunnel_processes: dict[SessionId, asyncio.subprocess.Process] = {}
        self._grace_timers: dict[SessionId, asyncio.Task[None]] = {}
        self._start_tasks: dict[SessionId, asyncio.Task[None]] = {}
        self._connected_counts: dict[SessionId, int] = defaultdict(int)
        self._spawn_fn: Callable[[Awaitable[Any]], asyncio.Task[Any]] | None = None

    def bind_background(self, spawn_fn: Callable[[Awaitable[Any]], asyncio.Task[Any]]) -> None:
        self._spawn_fn = spawn_fn

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task[Any] | None:
        if self._spawn_fn is not None:
            return self._spawn_fn(coro)
        # Default: use the running loop and attach a logging done-callback.
        task = asyncio.ensure_future(coro)

        def _done(t: asyncio.Task[Any]) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                structlog.get_logger().error("background_task_failed", error=str(exc))

        task.add_done_callback(_done)
        return task

    # --- Capacity ownership ledger (Plan §14.6) ---
    def _capacity_acquire(self, session_id: SessionId) -> None:
        if session_id in self._capacity_owned:
            structlog.get_logger().warning("capacity_acquire_duplicate", session_id=str(session_id))
            return
        if len(self._capacity_owned) >= self._capacity_total:
            raise RuntimeError("capacity reached")
        self._capacity_owned.add(session_id)

    def _capacity_release(self, session_id: SessionId) -> None:
        self._capacity_owned.discard(session_id)

    def _get_lock(self, alias: str) -> asyncio.Lock:
        return self._alias_locks[alias]

    async def open(self, alias: str) -> SessionView:
        async with self._get_lock(alias):
            catalog = self._catalog.snapshot
            if catalog is None or not self._catalog.is_valid_alias(alias):
                raise GatewayError(
                    ErrorCode.ALIAS_NOT_FOUND, f"Alias '{alias}' not found", status_code=404
                )

            existing = await get_session_by_alias(self._db, alias)
            if existing:
                if existing.state in (SessionState.STARTING, SessionState.READY):
                    return self._to_view(existing)
                if existing.state == SessionState.STOPPING:
                    raise GatewayError(ErrorCode.CONFLICT, "Session is stopping", status_code=409)
                if existing.state == SessionState.ERROR:
                    raise GatewayError(
                        ErrorCode.CONFLICT,
                        "Session is in error; retry or close it first",
                        status_code=409,
                    )

            if len(self._capacity_owned) >= self._capacity_total:
                raise GatewayError(
                    ErrorCode.CAPACITY_REACHED,
                    "Session capacity reached; close an existing session first",
                    status_code=429,
                )

            session_id = uuid.uuid4()
            session = SessionRecord(
                id=session_id,
                alias=alias,
                state=SessionState.STARTING,
                stage=SessionStage.VALIDATE,
            )
            await insert_session(self._db, session)

            # Reserve capacity for this specific session ID only after the
            # row is durable. If acquisition fails (capacity was taken
            # while we awaited insert), roll back the row.
            try:
                self._capacity_acquire(session_id)
            except RuntimeError as exc:
                with suppress(Exception):
                    await delete_session(self._db, str(session_id))
                raise GatewayError(
                    ErrorCode.CAPACITY_REACHED,
                    "Session capacity reached; close an existing session first",
                    status_code=429,
                ) from exc

            # Spawn the slow _do_open work in the background so the HTTP
            # route can return 202 immediately. _do_open is responsible for
            # updating the session record (including marking it error) as it
            # progresses.
            task = self._spawn(self._run_open(session_id, alias))
            if task is not None:
                self._start_tasks[session_id] = task
            else:
                # No background runner available — run inline (used in unit
                # tests that don't bind a spawner).
                try:
                    return await self._do_open(session_id, alias)
                except Exception:
                    self._start_tasks.pop(session_id, None)
                    raise
            return self._to_view(session)

    async def _run_open(self, session_id: SessionId, alias: str) -> None:
        try:
            await self._do_open(session_id, alias)
        except Exception as exc:
            logger = structlog.get_logger()
            logger.error("open_task_failed", session_id=str(session_id), error=str(exc))
        finally:
            self._start_tasks.pop(session_id, None)

    async def _do_open(self, session_id: SessionId, alias: str) -> SessionView:
        try:
            await update_session_stage(self._db, str(session_id), SessionStage.VALIDATE)
            capabilities = await self._runtime.capabilities(alias)

            await update_session_stage(self._db, str(session_id), SessionStage.INSTALL)
            await self._runtime.ensure_installed(alias, capabilities.platform)

            await update_session_stage(self._db, str(session_id), SessionStage.START_REMOTE)
            remote = await self._runtime.start_session(alias, session_id)
            await set_remote_identity(
                self._db,
                str(session_id),
                remote.pid,
                remote.port,
                remote.boot_id,
                remote.process_start_id,
                remote.executable,
            )

            await update_session_stage(self._db, str(session_id), SessionStage.START_TUNNEL)
            tunnel_identity, tunnel_proc = await start_local_forward(
                self._settings, alias, remote.port
            )
            await set_tunnel_identity(
                self._db,
                str(session_id),
                tunnel_identity.local_port,
                tunnel_identity.pid,
            )
            self._tunnel_processes[session_id] = tunnel_proc
            self._registry.add(session_id, tunnel_identity.local_port)

            await update_session_stage(self._db, str(session_id), SessionStage.VERIFY)
            await self._verify_editor_health(session_id, tunnel_identity.local_port)

            await mark_ready(self._db, str(session_id))
            self._start_tasks.pop(session_id, None)

            record = await get_session(self._db, str(session_id))
            if record is None:
                raise GatewayError(ErrorCode.INTERNAL_ERROR, "Session disappeared after ready")
            return self._to_view(record)

        except GatewayError as exc:
            if exc.code != ErrorCode.CAPACITY_REACHED and exc.code != ErrorCode.ALIAS_NOT_FOUND:
                await self._best_effort_cleanup(
                    session_id,
                    str(exc.code)
                    in (
                        ErrorCode.REMOTE_START_FAILED,
                        ErrorCode.TUNNEL_START_FAILED,
                        ErrorCode.STARTUP_TIMEOUT,
                    ),
                )
                existing = await get_session(self._db, str(session_id))
                if existing is not None:
                    await mark_error(
                        self._db,
                        str(session_id),
                        exc.code.value,
                        exc.safe_message,
                        stage=SessionStage.STOP,
                    )
            self._start_tasks.pop(session_id, None)
            raise

    async def close(
        self,
        alias: str,
        reason: CloseReason = CloseReason.USER_REQUESTED,
    ) -> None:
        async with self._get_lock(alias):
            session = await get_session_by_alias(self._db, alias)
            if session is None:
                return

            await mark_stopping(self._db, str(session.id), reason.value)
            self._cancel_start(session.id)
            self._cancel_grace(session.id)
            self._registry.remove(session.id)
            close_session = session

        # Run the slow cleanup work in the background; route returns 204.
        self._spawn(self._do_close(close_session))

    async def _do_close(self, session: SessionRecord) -> None:
        session_id = session.id
        errors: list[str] = []

        tunnel = self._tunnel_processes.pop(session_id, None)
        if tunnel is not None:
            try:
                tunnel.terminate()
                try:
                    await asyncio.wait_for(tunnel.wait(), timeout=10.0)
                except TimeoutError:
                    tunnel.kill()
                    await tunnel.wait()
            except ProcessLookupError:
                pass
            except Exception as exc:
                errors.append(f"Tunnel termination error: {exc}")

        if session.remote_pid and session.state != SessionState.STARTING:
            try:
                await self._runtime.stop_session(session.alias, session_id)
            except GatewayError as exc:
                errors.append(exc.safe_message)
            except Exception as exc:
                errors.append(str(exc))

        if errors:
            await mark_error(
                self._db,
                str(session_id),
                ErrorCode.STOP_FAILED.value,
                "; ".join(errors),
                stage=SessionStage.STOP,
            )
            return

        with suppress(Exception):
            await self._runtime.remove_session(session.alias, session_id)

        await delete_session(self._db, str(session_id))
        # Release this session's capacity reservation only after the row is
        # gone and resources are confirmed absent (Plan §14.2 step 9).
        self._capacity_release(session_id)

        self._connected_counts.pop(session_id, None)

    async def retry(self, alias: str) -> SessionView:
        # Per plan §14.3: retry must never call public ``open()`` while
        # holding the same non-reentrant alias lock. We synchronously
        # establish cleanup safety inside the lock, release it, then call
        # the public ``open()`` from outside.
        async with self._get_lock(alias):
            session = await get_session_by_alias(self._db, alias)
            if session is None:
                raise GatewayError(
                    ErrorCode.ALIAS_NOT_FOUND,
                    f"No session to retry for alias '{alias}'",
                    status_code=404,
                )
            if session.state != SessionState.ERROR:
                raise GatewayError(
                    ErrorCode.CONFLICT,
                    "Only errored sessions can be retried",
                    status_code=409,
                )

            # Cancel and *await* any residual start task so its cancellation
            # cleanup contract completes before we touch resources. Never
            # spawn a competing background close that could overlap with a
            # new open.
            start_task = self._start_tasks.pop(session.id, None)
            if start_task is not None:
                start_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await start_task

            self._cancel_grace(session.id)
            self._registry.remove(session.id)

            # Run resource cleanup synchronously. ``_do_close`` is
            # responsible for stopping owned tunnel/remote, deleting the
            # row, and releasing capacity exactly once. If cleanup cannot
            # prove absence it leaves the error row in place and re-raises.
            try:
                await self._do_close(session)
            except Exception as exc:
                raise GatewayError(
                    ErrorCode.STOP_FAILED,
                    f"Retry aborted: cleanup failed: {exc}",
                    status_code=500,
                ) from exc

        # Old run safely closed; lock released. Open the fresh run through
        # the public entry point so we never re-enter the alias lock.
        return await self.open(alias)

    async def recover_all(self) -> RecoveryReport:
        sessions = await list_sessions(self._db)

        # Rebuild the capacity ownership ledger from persisted rows before
        # accepting mutations so recovery matches Plan §14.6: every
        # resource-bearing row (all durable rows here) counts against
        # capacity. Deletes during recovery remove the corresponding id.
        self._capacity_owned = {record.id for record in sessions}

        recovered = 0
        failed = 0
        cleaned = 0

        for record in sessions:
            async with self._get_lock(record.alias):
                try:
                    if record.state == SessionState.STARTING:
                        insp = await self._runtime.inspect_session(record.alias, record.id)
                        if insp.get("running") and insp.get("identity_ok", False):
                            port = insp.get("port", 0)
                            if port:
                                tunnel_identity, tunnel_proc = await start_local_forward(
                                    self._settings, record.alias, port
                                )
                                await set_tunnel_identity(
                                    self._db,
                                    str(record.id),
                                    tunnel_identity.local_port,
                                    tunnel_identity.pid,
                                )
                                self._tunnel_processes[record.id] = tunnel_proc
                                self._registry.add(record.id, tunnel_identity.local_port)
                                await mark_ready(self._db, str(record.id))
                                recovered += 1
                            else:
                                await delete_session(self._db, str(record.id))
                                cleaned += 1
                                self._capacity_release(record.id)
                        else:
                            await mark_error(
                                self._db,
                                str(record.id),
                                ErrorCode.RECOVERY_FAILED.value,
                                "Remote process not found or identity mismatch",
                            )
                            failed += 1

                    elif record.state == SessionState.READY:
                        insp = await self._runtime.inspect_session(record.alias, record.id)
                        if insp.get("running") and insp.get("identity_ok", False):
                            port = insp.get("port", 0)
                            if port:
                                tunnel_identity, tunnel_proc = await start_local_forward(
                                    self._settings, record.alias, port
                                )
                                await set_tunnel_identity(
                                    self._db,
                                    str(record.id),
                                    tunnel_identity.local_port,
                                    tunnel_identity.pid,
                                )
                                self._tunnel_processes[record.id] = tunnel_proc
                                self._registry.add(record.id, tunnel_identity.local_port)
                            recovered += 1
                        else:
                            await mark_error(
                                self._db,
                                str(record.id),
                                ErrorCode.RECOVERY_FAILED.value,
                                "Remote process absent after restart",
                            )
                            failed += 1

                        if record.disconnect_deadline_at:
                            deadline = record.disconnect_deadline_at
                            if deadline > datetime.now(UTC):
                                t = asyncio.ensure_future(self._grace_watcher(record.id, deadline))
                                self._grace_timers[record.id] = t
                            else:
                                self._spawn(
                                    self.close(record.alias, CloseReason.DISCONNECT_GRACE_EXPIRED)
                                )

                    elif record.state == SessionState.STOPPING:
                        self._spawn(self._do_close(record))

                    elif record.state == SessionState.ERROR:
                        insp = await self._runtime.inspect_session(record.alias, record.id)
                        if not insp.get("running"):
                            await delete_session(self._db, str(record.id))
                            cleaned += 1
                            self._capacity_release(record.id)

                except Exception:
                    await mark_error(
                        self._db,
                        str(record.id),
                        ErrorCode.RECOVERY_FAILED.value,
                        "Recovery attempt failed",
                    )
                    failed += 1

        return RecoveryReport(
            recovered=recovered,
            failed=failed,
            cleaned=cleaned,
            total=len(sessions),
        )

    async def reconcile_catalog(self, task_group: asyncio.TaskGroup) -> None:
        catalog = self._catalog.snapshot
        if catalog is None or catalog.error:
            return

        sessions = await list_sessions(self._db)
        for record in sessions:
            if record.alias not in catalog.aliases:
                task_group.create_task(self.close(record.alias, CloseReason.ALIAS_REMOVED))

    def get_workspaces(self) -> list[WorkspaceView]:
        return []  # populated by routes calling get_workspaces_full

    async def get_workspaces_full(self) -> list[WorkspaceView]:
        catalog = self._catalog.snapshot
        catalog_aliases: set[str] = set(catalog.aliases) if catalog else set()
        sessions = await list_sessions(self._db)
        session_by_alias: dict[str, SessionRecord] = {s.alias: s for s in sessions}

        result: list[WorkspaceView] = []

        for alias in sorted(set(list(catalog_aliases) + list(session_by_alias.keys()))):
            session = session_by_alias.get(alias)

            if session is None:
                result.append(
                    WorkspaceView(
                        alias=alias,
                        state="closed",
                        can_open=True,
                    )
                )
            else:
                editor_url = None
                if session.state == SessionState.READY:
                    editor_url = f"/editor/{session.id}/"

                state_map = {
                    SessionState.STARTING: "starting",
                    SessionState.READY: "ready",
                    SessionState.STOPPING: "stopping",
                    SessionState.ERROR: "error",
                }

                result.append(
                    WorkspaceView(
                        alias=alias,
                        state=state_map.get(session.state, "error"),  # type: ignore[arg-type]
                        session_id=session.id,
                        editor_url=editor_url,
                        connected_clients=self._connected_counts.get(session.id, 0),
                        disconnect_deadline=session.disconnect_deadline_at,
                        stage=session.stage.value if session.stage else None,
                        error_code=session.error_code,
                        error_message=session.error_message,
                        can_open=session.state == SessionState.ERROR and session.error_code is None,
                        can_close=session.state
                        in (
                            SessionState.STARTING,
                            SessionState.READY,
                            SessionState.STOPPING,
                            SessionState.ERROR,
                        ),
                        can_retry=session.state == SessionState.ERROR,
                        catalog_missing=alias not in catalog_aliases,
                    )
                )

        def _order_key(ws: WorkspaceView) -> tuple[int, str]:
            order = {"ready": 0, "starting": 1, "stopping": 2, "error": 3, "closed": 4}
            return (order.get(ws.state, 9), ws.alias)

        result.sort(key=_order_key)
        return result

    async def get_sessions(self) -> list[SessionView]:
        sessions = await list_sessions(self._db)
        return [self._to_view(s) for s in sessions]

    async def get_session_view(self, alias: str) -> SessionView | None:
        session = await get_session_by_alias(self._db, alias)
        if session is None:
            return None
        return self._to_view(session)

    def _to_view(self, record: SessionRecord) -> SessionView:
        editor_url = None
        if record.state == SessionState.READY:
            editor_url = f"/editor/{record.id}/"
        return SessionView(
            id=record.id,
            alias=record.alias,
            state=record.state,
            stage=record.stage,
            connected_clients=self._connected_counts.get(record.id, 0),
            disconnect_deadline=record.disconnect_deadline_at,
            editor_url=editor_url,
            error_code=record.error_code,
            error_message=record.error_message,
        )

    async def _verify_editor_health(self, session_id: SessionId, local_port: int) -> None:
        import httpx

        url = f"http://127.0.0.1:{local_port}/editor/{session_id}"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=10.0)
                if response.status_code >= 400:
                    raise GatewayError(
                        ErrorCode.EDITOR_UNHEALTHY,
                        f"Editor returned {response.status_code}",
                        status_code=502,
                    )
        except httpx.RequestError as exc:
            raise GatewayError(
                ErrorCode.EDITOR_UNHEALTHY,
                f"Cannot reach editor: {exc}",
                status_code=502,
            ) from exc

    async def _best_effort_cleanup(
        self, session_id: SessionId, has_resources: bool = False
    ) -> None:
        self._registry.remove(session_id)
        tunnel = self._tunnel_processes.pop(session_id, None)
        if tunnel is not None:
            try:
                tunnel.terminate()
                try:
                    await asyncio.wait_for(tunnel.wait(), timeout=5.0)
                except TimeoutError:
                    tunnel.kill()
                    await tunnel.wait()
            except (ProcessLookupError, TimeoutError):
                pass

    def _cancel_start(self, session_id: SessionId) -> None:
        task = self._start_tasks.pop(session_id, None)
        if task is not None:
            task.cancel()

    def _cancel_grace(self, session_id: SessionId) -> None:
        task = self._grace_timers.pop(session_id, None)
        if task is not None:
            task.cancel()

    async def _grace_watcher(self, session_id: SessionId, deadline: datetime) -> None:
        now = datetime.now(UTC)
        delay = (deadline - now).total_seconds()
        if delay > 0:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

        record = await get_session(self._db, str(session_id))
        if record is None:
            return
        if record.state != SessionState.READY:
            return
        if self._connected_counts.get(session_id, 0) > 0:
            return

        await self.close(record.alias, CloseReason.DISCONNECT_GRACE_EXPIRED)
        self._grace_timers.pop(session_id, None)

    # --- Presence / proxy callbacks ---
    def on_client_connected(self, session_id: SessionId) -> None:
        self._cancel_grace(session_id)
        self._connected_counts[session_id] = self._connected_counts.get(session_id, 0) + 1
        self._spawn(set_last_connected(self._db, str(session_id)))

    def on_client_disconnected(self, session_id: SessionId) -> None:
        count = self._connected_counts.get(session_id, 0)
        if count > 0:
            count -= 1
            self._connected_counts[session_id] = count

        if count == 0:
            now = datetime.now(UTC)
            deadline = now + timedelta(seconds=self._settings.disconnect_grace_period)
            t = self._spawn(self._grace_watcher(session_id, deadline))
            if t is not None:
                self._grace_timers[session_id] = t
            self._spawn(set_disconnect_deadline(self._db, str(session_id), now, deadline))
            self._spawn(update_connected_clients(self._db, str(session_id), count))

    async def on_tunnel_exit(self, session_id: SessionId, return_code: int) -> None:
        record = await get_session(self._db, str(session_id))
        if record is None:
            return
        if record.state == SessionState.STOPPING:
            return

        self._registry.remove(session_id)
        await mark_error(
            self._db,
            str(session_id),
            ErrorCode.TUNNEL_LOST.value,
            f"SSH tunnel exited with code {return_code}",
        )
        with suppress(Exception):
            await self._runtime.stop_session(record.alias, session_id)

"""Manage remote editor session lifecycle and resources."""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import aiosqlite
import asyncssh
import structlog

from vscode_gateway.db import (
    clear_remote_identity,
    clear_tunnel_identity,
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
from vscode_gateway.host_trust import HostTrustService
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
from vscode_gateway.ssh_config import SshCatalog
from vscode_gateway.ssh_connection import SshConnection, SshConnectionService

logger = structlog.get_logger()
_RESOURCE_CLOSE_TIMEOUT = 5.0
_PRE_REMOTE_FAILURE_CODES = frozenset(
    {
        ErrorCode.SSH_NO_UPLOADED_KEYS.value,
        ErrorCode.SSH_NO_UPLOADED_KEY_ACCEPTED.value,
        ErrorCode.SSH_HOST_UNKNOWN.value,
        ErrorCode.SSH_HOST_CHANGED.value,
    }
)


@dataclass
class _OpenLedger:
    """Resources acquired during one open operation."""

    session_id: SessionId
    alias: str
    ssh_conn: SshConnection | None = None
    remote_started: bool = False
    remote_identity_persisted: bool = False
    forward_started: bool = False
    tunnel_identity_persisted: bool = False
    registry_added: bool = False
    ready_task: asyncio.Task[None] | None = None


@dataclass
class _SessionTunnel:
    ssh_conn: SshConnection
    listener: asyncssh.SSHListener | None = None
    watcher_task: asyncio.Task[None] | None = None


class SessionService:
    def __init__(
        self,
        settings: Settings,
        db: aiosqlite.Connection,
        catalog: SshCatalog,
        runtime: RuntimeService,
        proxy_registry: ProxyRegistry,
        connection_service: SshConnectionService,
        host_trust_service: HostTrustService,
    ) -> None:
        self._settings = settings
        self._db = db
        self._catalog = catalog
        self._runtime = runtime
        self._registry = proxy_registry
        self._connection_service = connection_service
        self._host_trust_service = host_trust_service

        self._alias_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._capacity_total: int = settings.session_capacity
        self._capacity_owned: set[SessionId] = set()

        self._tunnels: dict[SessionId, _SessionTunnel] = {}
        self._grace_timers: dict[SessionId, asyncio.Task[None]] = {}
        self._start_tasks: dict[SessionId, asyncio.Task[None]] = {}
        self._connected_counts: dict[SessionId, int] = defaultdict(int)
        self._spawn_fn: Callable[[Awaitable[Any]], asyncio.Task[Any]] | None = None

    def bind_background(self, spawn_fn: Callable[[Awaitable[Any]], asyncio.Task[Any]]) -> None:
        self._spawn_fn = spawn_fn

    async def shutdown(self) -> None:
        """Close all local forwarding resources without stopping remotes.

        Durable session rows remain intact so startup recovery can inspect
        and reattach to the managed remote processes on the next start.
        """
        for task in list(self._grace_timers.values()):
            task.cancel()
        self._grace_timers.clear()

        for session_id, tunnel in list(self._tunnels.items()):
            self._registry.remove(session_id)
            await self._cancel_forward_watcher(tunnel)
            if tunnel.listener is not None:
                error = await self._close_listener(tunnel.listener)
                if error is not None:
                    logger.warning(
                        "shutdown_listener_close_failed",
                        session_id=str(session_id),
                        error=error,
                    )
            error = await self._close_ssh_connection(tunnel.ssh_conn)
            if error is not None:
                logger.warning(
                    "shutdown_connection_close_failed",
                    session_id=str(session_id),
                    error=error,
                )
        self._tunnels.clear()

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task[Any] | None:
        if self._spawn_fn is not None:
            return self._spawn_fn(coro)
        task = asyncio.ensure_future(coro)

        def _done(t: asyncio.Task[Any]) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                structlog.get_logger().error("background_task_failed", error=str(exc))

        task.add_done_callback(_done)
        return task

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

            task = self._spawn(self._run_open(session_id, alias))
            if task is not None:
                self._start_tasks[session_id] = task
            else:
                try:
                    return await self._do_open(session_id, alias)
                except Exception:
                    self._start_tasks.pop(session_id, None)
                    raise
            return self._to_view(session)

    async def _run_open(self, session_id: SessionId, alias: str) -> None:
        try:
            await self._do_open(session_id, alias)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            structlog.get_logger().error(
                "open_task_unhandled",
                session_id=str(session_id),
                error=str(exc),
            )
            with suppress(Exception):
                existing = await get_session(self._db, str(session_id))
                if existing is not None and existing.state == SessionState.STARTING:
                    await mark_error(
                        self._db,
                        str(session_id),
                        ErrorCode.INTERNAL_ERROR.value,
                        "Internal error during open",
                        stage=SessionStage.STOP,
                    )
        finally:
            self._start_tasks.pop(session_id, None)

    async def _do_open(self, session_id: SessionId, alias: str) -> SessionView:
        ledger = _OpenLedger(session_id=session_id, alias=alias)
        try:
            await update_session_stage(self._db, str(session_id), SessionStage.VALIDATE)
            ssh_conn = await self._connection_service.connect_for_session(
                session_id=session_id,
                alias=alias,
            )
            ledger.ssh_conn = ssh_conn

            await update_session_stage(self._db, str(session_id), SessionStage.INSTALL)
            capabilities = await self._runtime.capabilities(ssh_conn.conn)
            await self._runtime.ensure_installed(ssh_conn.conn, capabilities.platform)

            await update_session_stage(self._db, str(session_id), SessionStage.START_REMOTE)
            remote = await self._runtime.start_session(ssh_conn.conn, session_id)
            ledger.remote_started = True
            await set_remote_identity(
                self._db,
                str(session_id),
                remote.pid,
                remote.port,
                remote.boot_id,
                remote.process_start_id,
                remote.executable,
            )
            ledger.remote_identity_persisted = True

            await update_session_stage(self._db, str(session_id), SessionStage.START_TUNNEL)
            listener, local_port = await self._connection_service.forward_local_port(
                ssh_conn, remote.port
            )
            ledger.forward_started = True
            await asyncio.shield(
                set_tunnel_identity(
                    self._db,
                    str(session_id),
                    local_port,
                    0,  # no subprocess PID
                )
            )
            ledger.tunnel_identity_persisted = True
            self._tunnels[session_id] = _SessionTunnel(
                ssh_conn=ssh_conn,
                listener=listener,
            )
            self._registry.add(session_id, local_port)
            ledger.registry_added = True
            self._spawn_forward_watcher(session_id)

            await update_session_stage(self._db, str(session_id), SessionStage.VERIFY)
            await self._verify_editor_health(session_id, local_port)

            # Keep the durable state aligned with live resources on cancellation.
            ready_task = asyncio.ensure_future(mark_ready(self._db, str(session_id)))
            ledger.ready_task = ready_task
            await asyncio.shield(ready_task)
            return self._to_view(await self._finalize_open_read(session_id, ready_task))

        except asyncio.CancelledError:
            await self._open_failure_cleanup(ledger, ErrorCode.INTERNAL_ERROR, "Open cancelled")
            raise
        except GatewayError as exc:
            if exc.code not in (ErrorCode.CAPACITY_REACHED, ErrorCode.ALIAS_NOT_FOUND):
                await self._open_failure_cleanup(ledger, exc.code, exc.safe_message)
            raise
        except Exception as exc:
            structlog.get_logger().error(
                "open_internal_error",
                session_id=str(session_id),
                error=str(exc),
            )
            await self._open_failure_cleanup(
                ledger, ErrorCode.INTERNAL_ERROR, "Internal error during open"
            )
            raise

    def _spawn_forward_watcher(self, session_id: SessionId) -> None:
        tunnel = self._tunnels.get(session_id)
        if tunnel is None or tunnel.listener is None:
            return
        if tunnel.watcher_task is not None:
            return

        async def _watch() -> None:
            try:
                await tunnel.listener.wait_closed()  # type: ignore[union-attr]
            except asyncio.CancelledError:
                return
            await self.on_tunnel_exit(session_id, 0)

        task = self._spawn(_watch())
        if task is not None:
            tunnel.watcher_task = cast(asyncio.Task[None], task)

    async def _cancel_forward_watcher(self, tunnel: _SessionTunnel) -> None:
        task = tunnel.watcher_task
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
        tunnel.watcher_task = None

    async def _close_listener(self, listener: asyncssh.SSHListener) -> str | None:
        try:
            listener.close()
            await asyncio.wait_for(listener.wait_closed(), timeout=_RESOURCE_CLOSE_TIMEOUT)
        except Exception as exc:
            return f"Forward listener close error: {exc}"
        return None

    async def _close_connection(self, conn: asyncssh.SSHClientConnection) -> str | None:
        try:
            conn.close()
            await asyncio.wait_for(conn.wait_closed(), timeout=_RESOURCE_CLOSE_TIMEOUT)
        except Exception as exc:
            return f"SSH connection close error: {exc}"
        return None

    async def _close_ssh_connection(self, ssh_conn: SshConnection) -> str | None:
        """Close the target and every jump connection in reverse order."""
        errors: list[str] = []
        for conn in reversed(ssh_conn.chain):
            error = await self._close_connection(conn)
            if error is not None:
                errors.append(error)
        return "; ".join(errors) if errors else None

    async def _finalize_open_read(
        self,
        session_id: SessionId,
        ready_task: asyncio.Task[None],
    ) -> SessionRecord:
        with suppress(asyncio.CancelledError, Exception):
            if not ready_task.done():
                await ready_task
        record = await get_session(self._db, str(session_id))
        if record is None:
            raise GatewayError(ErrorCode.INTERNAL_ERROR, "Session disappeared after ready")
        return record

    async def _open_failure_cleanup(
        self,
        ledger: _OpenLedger,
        code: ErrorCode,
        message: str,
    ) -> None:
        ready_task = ledger.ready_task
        if ready_task is not None:
            with suppress(asyncio.CancelledError, Exception):
                if not ready_task.done():
                    await ready_task
            existing = await get_session(self._db, str(ledger.session_id))
            if existing is not None and existing.state == SessionState.READY:
                return

        if ledger.registry_added:
            with suppress(Exception):
                self._registry.remove(ledger.session_id)
            ledger.registry_added = False

        tunnel = self._tunnels.pop(ledger.session_id, None)
        if tunnel is not None:
            await self._cancel_forward_watcher(tunnel)
            listener_error = None
            if tunnel.listener is not None:
                listener_error = await self._close_listener(tunnel.listener)
            if listener_error is None:
                ledger.forward_started = False
                with suppress(Exception):
                    await asyncio.shield(clear_tunnel_identity(self._db, str(ledger.session_id)))
                ledger.tunnel_identity_persisted = False
            else:
                logger.warning(
                    "open_cleanup_listener_close_failed",
                    session_id=str(ledger.session_id),
                    error=listener_error,
                )

        if ledger.remote_started and ledger.ssh_conn is not None:
            remote_stopped_ok = True
            try:
                await self._runtime.stop_session(ledger.ssh_conn.conn, ledger.session_id)
            except Exception:
                remote_stopped_ok = False
            ledger.remote_started = False
            if remote_stopped_ok and ledger.remote_identity_persisted:
                with suppress(Exception):
                    await asyncio.shield(clear_remote_identity(self._db, str(ledger.session_id)))
                ledger.remote_identity_persisted = False
                ledger.remote_started = False

        if ledger.ssh_conn is not None:
            close_error = await self._close_ssh_connection(ledger.ssh_conn)
            if close_error is not None:
                logger.warning(
                    "open_cleanup_connection_close_failed",
                    session_id=str(ledger.session_id),
                    error=close_error,
                )
                self._tunnels[ledger.session_id] = _SessionTunnel(
                    ssh_conn=ledger.ssh_conn,
                    listener=tunnel.listener if tunnel is not None else None,
                )
            ledger.ssh_conn = None

        with suppress(Exception):
            existing = await get_session(self._db, str(ledger.session_id))
            if existing is not None and existing.state != SessionState.READY:
                await asyncio.shield(
                    mark_error(
                        self._db,
                        str(ledger.session_id),
                        code.value,
                        message,
                        stage=SessionStage.STOP,
                    )
                )

    async def close(
        self,
        alias: str,
        reason: CloseReason = CloseReason.USER_REQUESTED,
        *,
        force: bool = False,
    ) -> None:
        async with self._get_lock(alias):
            session = await get_session_by_alias(self._db, alias)
            if session is None:
                return

            session_id = session.id
            await mark_stopping(self._db, str(session_id), reason.value)
            self._cancel_grace(session_id)
            self._registry.remove(session_id)

            # Settle an in-flight open before inspecting its resources.
            start_task = self._start_tasks.pop(session_id, None)
            if start_task is not None:
                start_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await start_task

            close_session = session

            if force:
                await self._do_close(close_session, force=True)
                return

        self._spawn(self._do_close(close_session))

    async def _do_close(self, session: SessionRecord, *, force: bool = False) -> None:
        session_id = session.id

        fresh = await get_session(self._db, str(session_id))
        if fresh is None:
            self._cancel_grace(session_id)
            self._capacity_release(session_id)
            self._registry.remove(session_id)
            self._connected_counts.pop(session_id, None)
            return

        errors: list[str] = []
        remote_absent = False
        connection_close_failed = False
        had_remote_identity = self._has_persisted_remote_identity(fresh)

        tunnel = self._tunnels.pop(session_id, None)
        ssh_conn: SshConnection | None = tunnel.ssh_conn if tunnel is not None else None
        if tunnel is not None:
            await self._cancel_forward_watcher(tunnel)
            if tunnel.listener is not None:
                listener_error = await self._close_listener(tunnel.listener)
                if listener_error is not None:
                    errors.append(listener_error)
                else:
                    with suppress(Exception):
                        await clear_tunnel_identity(self._db, str(session_id))

        try:
            if self._can_close_without_remote_inspection(fresh):
                remote_absent = True
            else:
                ssh_conn = await self._conn_for_remote(
                    session_id=session_id,
                    alias=fresh.alias,
                    ssh_conn=ssh_conn,
                )
                if fresh.remote_pid is not None:
                    await self._runtime.stop_session(ssh_conn.conn, session_id)
                    remote_absent = True
                else:
                    inspection = await self._runtime.inspect_session(ssh_conn.conn, session_id)
                    if inspection["running"] is True:
                        await self._runtime.stop_session(ssh_conn.conn, session_id)
                    remote_absent = True

                await self._runtime.remove_session(ssh_conn.conn, session_id)
        except GatewayError as exc:
            errors.append(exc.safe_message)
        except Exception as exc:
            errors.append(str(exc))
        finally:
            if ssh_conn is not None:
                close_error = await self._close_ssh_connection(ssh_conn)
                if close_error is not None:
                    errors.append(close_error)
                    connection_close_failed = True

        if connection_close_failed and ssh_conn is not None and not force:
            self._tunnels[session_id] = _SessionTunnel(
                ssh_conn=ssh_conn,
                listener=None,
            )

        if remote_absent:
            try:
                await clear_remote_identity(self._db, str(session_id))
            except Exception as exc:
                errors.append(f"Failed to clear remote identity: {exc}")

        if not force and (errors or not remote_absent):
            message = "; ".join(errors) if errors else "Remote process absence was not confirmed"
            await mark_error(
                self._db,
                str(session_id),
                ErrorCode.STOP_FAILED.value,
                message,
                stage=SessionStage.STOP,
            )
            return

        await delete_session(self._db, str(session_id))
        self._cancel_grace(session_id)
        self._capacity_release(session_id)
        self._registry.remove(session_id)
        self._connected_counts.pop(session_id, None)

        if force:
            logger.warning(
                "session_force_closed",
                session_id=str(session_id),
                persisted_remote_identity=had_remote_identity,
                remote_cleanup_confirmed=remote_absent,
                cleanup_error_count=len(errors),
            )

    @staticmethod
    def _has_persisted_remote_identity(session: SessionRecord) -> bool:
        return any(
            value is not None
            for value in (
                session.remote_pid,
                session.remote_port,
                session.remote_boot_id,
                session.remote_process_start_id,
                session.remote_executable,
            )
        )

    @classmethod
    def _can_close_without_remote_inspection(cls, session: SessionRecord) -> bool:
        has_runtime_identity = cls._has_persisted_remote_identity(session) or any(
            value is not None for value in (session.local_port, session.tunnel_pid)
        )
        return session.error_code in _PRE_REMOTE_FAILURE_CODES and not has_runtime_identity

    async def _conn_for_remote(
        self,
        *,
        session_id: SessionId,
        alias: str,
        ssh_conn: SshConnection | None,
    ) -> SshConnection:
        """Reuse the session connection or open a new owned connection."""
        if ssh_conn is not None:
            return ssh_conn
        return await self._connection_service.connect_for_session(
            session_id=session_id, alias=alias
        )

    async def retry(self, alias: str) -> SessionView:
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

            start_task = self._start_tasks.pop(session.id, None)
            if start_task is not None:
                start_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await start_task

            self._cancel_grace(session.id)
            self._registry.remove(session.id)

            try:
                await self._do_close(session)
            except Exception as exc:
                raise GatewayError(
                    ErrorCode.STOP_FAILED,
                    f"Retry aborted: cleanup failed: {exc}",
                    status_code=500,
                ) from exc
            if await get_session(self._db, str(session.id)) is not None:
                raise GatewayError(
                    ErrorCode.STOP_FAILED,
                    "Retry aborted: cleanup could not confirm remote process absence",
                    status_code=409,
                )

        return await self.open(alias)

    async def recover_all(self) -> RecoveryReport:
        sessions = await list_sessions(self._db)

        self._capacity_owned = {record.id for record in sessions}

        recovered = 0
        failed = 0
        cleaned = 0

        for record in sessions:
            async with self._get_lock(record.alias):
                ssh_conn: SshConnection | None = None
                listener: asyncssh.SSHListener | None = None
                keep_resources = False
                try:
                    if record.state in (SessionState.STARTING, SessionState.READY):
                        ssh_conn = await self._connection_service.connect_for_session(
                            session_id=record.id, alias=record.alias
                        )
                        insp = await self._runtime.inspect_session(ssh_conn.conn, record.id)
                        if insp["running"] is False:
                            if record.state == SessionState.STARTING:
                                await self._runtime.remove_session(ssh_conn.conn, record.id)
                                close_error = await self._close_ssh_connection(ssh_conn)
                                if close_error is not None:
                                    raise GatewayError(
                                        ErrorCode.RECOVERY_FAILED,
                                        close_error,
                                        status_code=500,
                                    )
                                ssh_conn = None
                                await delete_session(self._db, str(record.id))
                                cleaned += 1
                                self._capacity_release(record.id)
                            else:
                                await clear_remote_identity(self._db, str(record.id))
                                await mark_error(
                                    self._db,
                                    str(record.id),
                                    ErrorCode.RECOVERY_FAILED.value,
                                    "Remote process absent after restart",
                                )
                                failed += 1
                            continue

                        port = insp.get("port")
                        invalid_identity = not insp.get("identity_ok", False)
                        invalid_port = not isinstance(port, int) or port <= 0
                        if invalid_identity or invalid_port:
                            await mark_error(
                                self._db,
                                str(record.id),
                                ErrorCode.RECOVERY_FAILED.value,
                                "Remote process identity mismatch or invalid port",
                            )
                            failed += 1
                            continue

                        remote_port = cast(int, port)
                        listener, local_port = await self._connection_service.forward_local_port(
                            ssh_conn, remote_port
                        )
                        await set_tunnel_identity(
                            self._db,
                            str(record.id),
                            local_port,
                            0,
                        )
                        if record.state == SessionState.STARTING:
                            await mark_ready(self._db, str(record.id))

                        self._tunnels[record.id] = _SessionTunnel(
                            ssh_conn=ssh_conn,
                            listener=listener,
                        )
                        self._registry.add(record.id, local_port)
                        self._spawn_forward_watcher(record.id)
                        keep_resources = True
                        recovered += 1

                        if record.disconnect_deadline_at:
                            deadline = record.disconnect_deadline_at
                            if deadline > datetime.now(UTC):
                                task = self._spawn(self._grace_watcher(record.id, deadline))
                                if task is not None:
                                    self._grace_timers[record.id] = cast(asyncio.Task[None], task)
                            else:
                                self._spawn(
                                    self.close(
                                        record.alias,
                                        CloseReason.DISCONNECT_GRACE_EXPIRED,
                                    )
                                )

                    elif record.state == SessionState.STOPPING:
                        await self._do_close(record)
                        if await get_session(self._db, str(record.id)) is None:
                            cleaned += 1
                        else:
                            failed += 1

                    elif record.state == SessionState.ERROR:
                        ssh_conn = await self._connection_service.connect_for_session(
                            session_id=record.id, alias=record.alias
                        )
                        insp = await self._runtime.inspect_session(ssh_conn.conn, record.id)
                        if insp["running"] is False:
                            await self._runtime.remove_session(ssh_conn.conn, record.id)
                            close_error = await self._close_ssh_connection(ssh_conn)
                            if close_error is not None:
                                raise GatewayError(
                                    ErrorCode.RECOVERY_FAILED,
                                    close_error,
                                    status_code=500,
                                )
                            ssh_conn = None
                            await delete_session(self._db, str(record.id))
                            cleaned += 1
                            self._capacity_release(record.id)
                        else:
                            failed += 1

                except Exception as exc:
                    if record.state != SessionState.ERROR:
                        await mark_error(
                            self._db,
                            str(record.id),
                            ErrorCode.RECOVERY_FAILED.value,
                            "Recovery attempt failed",
                        )
                    logger.warning(
                        "session_recovery_failed",
                        session_id=str(record.id),
                        alias=record.alias,
                        error=str(exc),
                    )
                    failed += 1
                finally:
                    if not keep_resources:
                        if listener is not None:
                            await self._close_listener(listener)
                            with suppress(Exception):
                                await clear_tunnel_identity(self._db, str(record.id))
                        if ssh_conn is not None:
                            close_error = await self._close_ssh_connection(ssh_conn)
                            if close_error is not None:
                                self._tunnels[record.id] = _SessionTunnel(
                                    ssh_conn=ssh_conn,
                                    listener=None,
                                )

        remaining_after = await list_sessions(self._db)
        error_sessions_remaining = sum(1 for r in remaining_after if r.state == SessionState.ERROR)
        orphaned_resources_remaining = sum(
            1 for r in remaining_after if r.state == SessionState.ERROR and r.remote_pid is not None
        )

        return RecoveryReport(
            recovered=recovered,
            failed=failed,
            cleaned=cleaned,
            total=len(sessions),
            error_sessions_remaining=error_sessions_remaining,
            orphaned_resources_remaining=orphaned_resources_remaining,
        )

    async def reconcile_catalog(self, task_group: asyncio.TaskGroup) -> None:
        catalog = self._catalog.snapshot
        if catalog is None or catalog.error:
            return

        sessions = await list_sessions(self._db)
        for record in sessions:
            if record.alias not in catalog.aliases:
                task_group.create_task(self.close(record.alias, CloseReason.ALIAS_REMOVED))

    async def get_workspaces_full(self) -> list[WorkspaceView]:
        catalog = self._catalog.snapshot
        catalog_aliases: set[str] = set(catalog.aliases) if catalog else set()
        sessions = await list_sessions(self._db)
        session_by_alias: dict[str, SessionRecord] = {s.alias: s for s in sessions}

        challenges: dict[SessionId, Any] = {}
        for ch in await self._host_trust_service.list_challenges():
            challenges[ch.session_id] = ch

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
                        can_force_close=session.state == SessionState.ERROR
                        and session.error_code == ErrorCode.STOP_FAILED.value,
                        has_remote_identity=self._has_persisted_remote_identity(session),
                        catalog_missing=alias not in catalog_aliases,
                        ssh_host_key=challenges.get(session.id),
                    )
                )

        def _order_key(ws: WorkspaceView) -> tuple[int, str]:
            order = {"ready": 0, "starting": 1, "stopping": 2, "error": 3, "closed": 4}
            return (order.get(ws.state, 9), ws.alias)

        result.sort(key=_order_key)
        return result

    async def get_sessions(self) -> list[SessionView]:
        sessions = await list_sessions(self._db)
        challenges: dict[SessionId, Any] = {}
        for ch in await self._host_trust_service.list_challenges():
            challenges[ch.session_id] = ch
        return [self._to_view(s, challenges.get(s.id)) for s in sessions]

    async def get_session_view(self, alias: str) -> SessionView | None:
        session = await get_session_by_alias(self._db, alias)
        if session is None:
            return None
        challenge: Any = await self._host_trust_service.get_challenge(session.id)
        return self._to_view(session, challenge)

    def _to_view(
        self,
        record: SessionRecord,
        ssh_host_key: Any | None = None,
    ) -> SessionView:
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
            ssh_host_key=ssh_host_key,
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
        if session_id not in self._capacity_owned:
            return
        self._cancel_grace(session_id)
        self._connected_counts[session_id] = self._connected_counts.get(session_id, 0) + 1
        self._spawn(set_last_connected(self._db, str(session_id)))

    def on_client_disconnected(self, session_id: SessionId) -> None:
        if session_id not in self._capacity_owned:
            self._cancel_grace(session_id)
            self._connected_counts.pop(session_id, None)
            return
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
        tunnel = self._tunnels.pop(session_id, None)
        ssh_conn = tunnel.ssh_conn if tunnel is not None else None
        if ssh_conn is not None:
            with suppress(Exception):
                await self._runtime.stop_session(ssh_conn.conn, session_id)
            close_error = await self._close_ssh_connection(ssh_conn)
            if close_error is not None:
                logger.warning(
                    "tunnel_exit_connection_close_failed",
                    session_id=str(session_id),
                    error=close_error,
                )

"""Shared fixtures and fakes for session lifecycle unit tests."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import aiosqlite
import asyncssh
import pytest

from vscode_gateway.db import open_database, run_migrations
from vscode_gateway.errors import ErrorCode, GatewayError
from vscode_gateway.host_trust import HostTrustService
from vscode_gateway.models import (
    CatalogSnapshot,
    HostKeyRole,
    RuntimeCapabilities,
    RuntimeIdentity,
    SessionId,
)
from vscode_gateway.proxy import ProxyRegistry
from vscode_gateway.runtime import RuntimeService
from vscode_gateway.sessions import SessionService
from vscode_gateway.settings import Settings
from vscode_gateway.ssh_config import SshCatalog
from vscode_gateway.ssh_connection import SshConnection, SshConnectionService, _HostKeyCapturer


def make_settings(tmp_path: Path, *, capacity: int = 10) -> Settings:
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


async def open_migrated_database(path: Path) -> aiosqlite.Connection:
    conn = await open_database(path)
    migrations_dir = Path(__file__).parent.parent.parent / "src" / "vscode_gateway" / "migrations"
    await run_migrations(conn, migrations_dir)
    return conn


def make_catalog(settings: Settings, aliases: tuple[str, ...]) -> SshCatalog:
    catalog = SshCatalog(settings)
    catalog.set_snapshot(
        CatalogSnapshot(
            revision="rev",
            aliases=aliases,
            loaded_at=datetime.now(UTC),
        )
    )
    return catalog


class FakeConnectionHandle:
    def __init__(self) -> None:
        self.closed = False
        self.waited = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited = True


class FakeListener:
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


class FakeConnectionService(SshConnectionService):
    def __init__(self) -> None:
        self.connections: list[SshConnection] = []
        self.listeners: list[FakeListener] = []

    async def connect_for_session(
        self,
        *,
        session_id: SessionId,
        alias: str,
        role: HostKeyRole = "target",
    ) -> SshConnection:
        del session_id, role
        handle = FakeConnectionHandle()
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
        listener = FakeListener()
        self.listeners.append(listener)
        ssh_conn.listener = cast(asyncssh.SSHListener, listener)
        ssh_conn.local_port = 54321
        ssh_conn.remote_port = remote_port
        return cast(asyncssh.SSHListener, listener), 54321


def make_session_service(
    settings: Settings,
    db: aiosqlite.Connection,
    catalog: SshCatalog,
    runtime: RuntimeService,
    connection_service: SshConnectionService,
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


@dataclass(slots=True)
class RuntimeStubState:
    stop_calls: list[SessionId] = field(default_factory=lambda: list[SessionId]())
    remove_calls: list[SessionId] = field(default_factory=lambda: list[SessionId]())
    verify_calls: list[int] = field(default_factory=lambda: list[int]())


def install_happy_open_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    remote: RuntimeIdentity | None = None,
    health_ok: bool = True,
) -> RuntimeStubState:
    """Install fakes for every ``_do_open`` dependency except ``mark_ready``."""
    state = RuntimeStubState()

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
        self: RuntimeService,
        connection: asyncssh.SSHClientConnection,
        session_id: SessionId,
        alias: str,
    ) -> RuntimeIdentity:
        del connection, session_id, alias
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
        state.stop_calls.append(session_id)
        return True

    async def _remove_session(
        self: RuntimeService, connection: asyncssh.SSHClientConnection, session_id: SessionId
    ) -> bool:
        del connection
        state.remove_calls.append(session_id)
        return True

    async def _inspect_session(
        self: RuntimeService,
        connection: asyncssh.SSHClientConnection,
        session_id: SessionId,
    ) -> dict[str, Any]:
        del connection, session_id
        return {"running": False}

    async def _verify(self: SessionService, session_id: SessionId, local_port: int) -> None:
        state.verify_calls.append(local_port)
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


async def wait_for_capacity_release(
    service: SessionService, session_id: SessionId, timeout: float = 2.0
) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if session_id not in service._capacity_owned:
            return True
        await asyncio.sleep(0.02)
    return session_id not in service._capacity_owned

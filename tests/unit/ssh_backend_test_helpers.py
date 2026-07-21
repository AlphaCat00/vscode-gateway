"""Small fixtures shared by the focused SSH-backend tests."""

# pyright: reportUnknownMemberType=false

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import aiosqlite
import asyncssh

from vscode_gateway.db import insert_session, open_database, run_migrations
from vscode_gateway.models import SessionRecord, SessionState
from vscode_gateway.settings import Settings


def generate_key(algorithm: str) -> asyncssh.SSHKey:
    """Call AsyncSSH's generator through a typed test-only wrapper."""
    generator = cast(Callable[[str], asyncssh.SSHKey], asyncssh.generate_private_key)
    return generator(algorithm)


def make_settings(
    tmp_path: Path,
    *,
    upload_limit: int = 131_072,
    canonical_origin: str = "http://testserver",
) -> Settings:
    """Build settings whose entire SSH state is inside ``tmp_path``."""
    state_dir = tmp_path / "state"
    ssh_dir = state_dir / "ssh"
    return Settings(
        canonical_origin=canonical_origin,
        state_dir=state_dir,
        runtime_dir=tmp_path / "runtime",
        ssh_dir=ssh_dir,
        ssh_config_path=ssh_dir / "config",
        ssh_known_hosts_path=ssh_dir / "known_hosts",
        ssh_keys_dir=ssh_dir / "keys",
        ssh_key_upload_max_bytes=upload_limit,
        password_hash_path=state_dir / "password.hash",
        session_secret_path=state_dir / "session.secret",
    )


@asynccontextmanager
async def migrated_database(tmp_path: Path) -> AsyncGenerator[aiosqlite.Connection]:
    """Yield a real temporary SQLite connection with all app migrations."""
    database = await open_database(tmp_path / "gateway.db")
    migrations = Path(__file__).resolve().parents[2] / "src" / "vscode_gateway" / "migrations"
    await run_migrations(database, migrations)
    try:
        yield database
    finally:
        await database.close()


async def add_session(
    database: aiosqlite.Connection,
    *,
    alias: str = "production",
    state: SessionState = SessionState.ERROR,
    session_id: UUID | None = None,
) -> UUID:
    """Insert a minimal session row for FK-backed SSH records."""
    session_id = session_id or uuid4()
    await insert_session(database, SessionRecord(id=session_id, alias=alias, state=state))
    return session_id

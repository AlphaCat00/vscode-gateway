"""Tests for the database module."""

import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from vscode_gateway.db import (
    delete_session,
    get_session,
    get_session_by_alias,
    insert_session,
    list_sessions,
    mark_error,
    mark_ready,
    mark_stopping,
    open_database,
    run_migrations,
    update_session_stage,
)
from vscode_gateway.models import SessionRecord, SessionStage, SessionState


@pytest.fixture
async def db() -> AsyncIterator[aiosqlite.Connection]:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        conn = await open_database(db_path)
        migrations_dir = (
            Path(__file__).parent.parent.parent / "src" / "vscode_gateway" / "migrations"
        )
        await run_migrations(conn, migrations_dir)
        yield conn
        await conn.close()


async def test_insert_and_get(db: aiosqlite.Connection) -> None:
    import uuid

    sid = uuid.uuid4()
    record = SessionRecord(
        id=sid,
        alias="test-host",
        state=SessionState.STARTING,
        stage=SessionStage.VALIDATE,
    )
    await insert_session(db, record)

    fetched = await get_session(db, str(sid))
    assert fetched is not None
    assert fetched.id == sid
    assert fetched.alias == "test-host"
    assert fetched.state == SessionState.STARTING


async def test_get_by_alias(db: aiosqlite.Connection) -> None:
    import uuid

    sid = uuid.uuid4()
    record = SessionRecord(
        id=sid,
        alias="myalias",
        state=SessionState.STARTING,
    )
    await insert_session(db, record)

    fetched = await get_session_by_alias(db, "myalias")
    assert fetched is not None
    assert fetched.alias == "myalias"

    missing = await get_session_by_alias(db, "nonexistent")
    assert missing is None


async def test_mark_ready(db: aiosqlite.Connection) -> None:
    import uuid

    sid = uuid.uuid4()
    record = SessionRecord(
        id=sid,
        alias="rhost",
        state=SessionState.STARTING,
    )
    await insert_session(db, record)

    await mark_ready(db, str(sid))
    fetched = await get_session(db, str(sid))
    assert fetched is not None
    assert fetched.state == SessionState.READY
    assert fetched.stage is None


async def test_mark_error(db: aiosqlite.Connection) -> None:
    import uuid

    sid = uuid.uuid4()
    record = SessionRecord(
        id=sid,
        alias="badhost",
        state=SessionState.STARTING,
    )
    await insert_session(db, record)

    await mark_error(db, str(sid), "ssh_unreachable", "Connection refused")
    fetched = await get_session(db, str(sid))
    assert fetched is not None
    assert fetched.state == SessionState.ERROR
    assert fetched.error_code == "ssh_unreachable"
    assert fetched.error_message == "Connection refused"


async def test_mark_stopping(db: aiosqlite.Connection) -> None:
    import uuid

    sid = uuid.uuid4()
    record = SessionRecord(
        id=sid,
        alias="stopme",
        state=SessionState.READY,
    )
    await insert_session(db, record)

    await mark_stopping(db, str(sid), "user_requested")
    fetched = await get_session(db, str(sid))
    assert fetched is not None
    assert fetched.state == SessionState.STOPPING
    assert fetched.close_reason == "user_requested"


async def test_delete_session(db: aiosqlite.Connection) -> None:
    import uuid

    sid = uuid.uuid4()
    record = SessionRecord(
        id=sid,
        alias="delme",
        state=SessionState.READY,
    )
    await insert_session(db, record)

    await delete_session(db, str(sid))
    fetched = await get_session(db, str(sid))
    assert fetched is None


async def test_list_sessions(db: aiosqlite.Connection) -> None:
    import uuid

    sid1 = uuid.uuid4()
    sid2 = uuid.uuid4()
    await insert_session(db, SessionRecord(id=sid1, alias="a", state=SessionState.STARTING))
    await insert_session(db, SessionRecord(id=sid2, alias="b", state=SessionState.READY))

    sessions = await list_sessions(db)
    assert len(sessions) == 2
    aliases = {s.alias for s in sessions}
    assert aliases == {"a", "b"}


async def test_update_stage(db: aiosqlite.Connection) -> None:
    import uuid

    sid = uuid.uuid4()
    record = SessionRecord(
        id=sid,
        alias="srv",
        state=SessionState.STARTING,
        stage=SessionStage.VALIDATE,
    )
    await insert_session(db, record)

    await update_session_stage(db, str(sid), SessionStage.START_REMOTE)
    fetched = await get_session(db, str(sid))
    assert fetched is not None
    assert fetched.stage == SessionStage.START_REMOTE

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from vscode_gateway.models import SessionRecord, SessionStage, SessionState


def _row_to_session(row: aiosqlite.Row) -> SessionRecord:
    from uuid import UUID

    return SessionRecord(
        id=UUID(row["id"]),
        alias=row["alias"],
        state=SessionState(row["state"]),
        stage=SessionStage(row["stage"]) if row["stage"] else None,
        remote_pid=row["remote_pid"],
        remote_port=row["remote_port"],
        remote_boot_id=row["remote_boot_id"],
        remote_process_start_id=row["remote_process_start_id"],
        remote_executable=row["remote_executable"],
        local_port=row["local_port"],
        tunnel_pid=row["tunnel_pid"],
        connected_clients=row["connected_clients"],
        last_connected_at=_parse_datetime(row["last_connected_at"]),
        last_disconnected_at=_parse_datetime(row["last_disconnected_at"]),
        disconnect_deadline_at=_parse_datetime(row["disconnect_deadline_at"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
        close_reason=row["close_reason"],
        created_at=_parse_datetime(row["created_at"]) or datetime.now(UTC),
        updated_at=_parse_datetime(row["updated_at"]) or datetime.now(UTC),
    )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


_MIGRATION_FILE_RE = re.compile(r"^(\d{3})_.*\.sql$")


async def open_database(path: Path) -> aiosqlite.Connection:
    db = await aiosqlite.connect(str(path))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode = WAL")
    await db.execute("PRAGMA synchronous = NORMAL")
    await db.execute("PRAGMA foreign_keys = ON")
    await db.execute("PRAGMA busy_timeout = 5000")
    return db


async def run_migrations(db: aiosqlite.Connection, migrations_dir: Path) -> None:
    migrations = sorted(
        [f for f in migrations_dir.iterdir() if _MIGRATION_FILE_RE.match(f.name)],
        key=lambda f: f.name,
    )

    async with db.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
        current_version: int = row[0] if row else 0

    for migration_file in migrations:
        match = _MIGRATION_FILE_RE.match(migration_file.name)
        if not match:
            continue
        version = int(match.group(1))
        if version <= current_version:
            continue

        sql = migration_file.read_text(encoding="utf-8")
        await db.executescript(f"BEGIN EXCLUSIVE;\n{sql}\nCOMMIT;")
        await db.execute(f"PRAGMA user_version = {version}")
        current_version = version


async def insert_session(db: aiosqlite.Connection, record: SessionRecord) -> None:
    await db.execute(
        """INSERT INTO sessions (
            id, alias, state, stage, remote_pid, remote_port, remote_boot_id,
            remote_process_start_id, remote_executable, local_port, tunnel_pid,
            connected_clients, last_connected_at, last_disconnected_at,
            disconnect_deadline_at, error_code, error_message, close_reason,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(record.id),
            record.alias,
            record.state.value,
            record.stage.value if record.stage else None,
            record.remote_pid,
            record.remote_port,
            record.remote_boot_id,
            record.remote_process_start_id,
            record.remote_executable,
            record.local_port,
            record.tunnel_pid,
            record.connected_clients,
            record.last_connected_at.isoformat() if record.last_connected_at else None,
            record.last_disconnected_at.isoformat() if record.last_disconnected_at else None,
            record.disconnect_deadline_at.isoformat() if record.disconnect_deadline_at else None,
            record.error_code,
            record.error_message,
            record.close_reason,
            record.created_at.isoformat(),
            record.updated_at.isoformat(),
        ),
    )
    await db.commit()


async def get_session(db: aiosqlite.Connection, session_id: str) -> SessionRecord | None:
    async with db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)) as cursor:
        row = await cursor.fetchone()
        return _row_to_session(row) if row else None


async def get_session_by_alias(db: aiosqlite.Connection, alias: str) -> SessionRecord | None:
    async with db.execute("SELECT * FROM sessions WHERE alias = ?", (alias,)) as cursor:
        row = await cursor.fetchone()
        return _row_to_session(row) if row else None


async def list_sessions(db: aiosqlite.Connection) -> list[SessionRecord]:
    async with db.execute("SELECT * FROM sessions ORDER BY created_at") as cursor:
        rows = await cursor.fetchall()
        return [_row_to_session(r) for r in rows]


async def update_session_stage(
    db: aiosqlite.Connection, session_id: str, stage: SessionStage
) -> None:
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "UPDATE sessions SET stage = ?, updated_at = ? WHERE id = ?",
        (stage.value, now, session_id),
    )
    await db.commit()


async def set_remote_identity(
    db: aiosqlite.Connection,
    session_id: str,
    remote_pid: int,
    remote_port: int,
    remote_boot_id: str,
    remote_process_start_id: str,
    remote_executable: str,
) -> None:
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """UPDATE sessions SET
            remote_pid = ?, remote_port = ?, remote_boot_id = ?,
            remote_process_start_id = ?, remote_executable = ?,
            updated_at = ?
        WHERE id = ?""",
        (
            remote_pid,
            remote_port,
            remote_boot_id,
            remote_process_start_id,
            remote_executable,
            now,
            session_id,
        ),
    )
    await db.commit()


async def set_tunnel_identity(
    db: aiosqlite.Connection, session_id: str, local_port: int, tunnel_pid: int
) -> None:
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "UPDATE sessions SET local_port = ?, tunnel_pid = ?, updated_at = ? WHERE id = ?",
        (local_port, tunnel_pid, now, session_id),
    )
    await db.commit()


async def clear_remote_identity(db: aiosqlite.Connection, session_id: str) -> None:
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """UPDATE sessions SET
            remote_pid = NULL, remote_port = NULL, remote_boot_id = NULL,
            remote_process_start_id = NULL, remote_executable = NULL,
            updated_at = ?
        WHERE id = ?""",
        (now, session_id),
    )
    await db.commit()


async def clear_tunnel_identity(db: aiosqlite.Connection, session_id: str) -> None:
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "UPDATE sessions SET local_port = NULL, tunnel_pid = NULL, updated_at = ? WHERE id = ?",
        (now, session_id),
    )
    await db.commit()


async def mark_ready(db: aiosqlite.Connection, session_id: str) -> None:
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """UPDATE sessions SET
            state = ?, stage = NULL, updated_at = ?
        WHERE id = ? AND state = 'starting'""",
        (SessionState.READY.value, now, session_id),
    )
    await db.commit()


async def mark_stopping(db: aiosqlite.Connection, session_id: str, reason: str) -> None:
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """UPDATE sessions SET
            state = ?, close_reason = ?, stage = ?, updated_at = ?
        WHERE id = ?""",
        (SessionState.STOPPING.value, reason, SessionStage.STOP.value, now, session_id),
    )
    await db.commit()


async def mark_error(
    db: aiosqlite.Connection,
    session_id: str,
    error_code: str,
    error_message: str = "",
    stage: SessionStage | None = None,
) -> None:
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """UPDATE sessions SET
            state = ?, error_code = ?, error_message = ?, stage = ?, updated_at = ?
        WHERE id = ?""",
        (
            SessionState.ERROR.value,
            error_code,
            error_message,
            stage.value if stage else None,
            now,
            session_id,
        ),
    )
    await db.commit()


async def delete_session(db: aiosqlite.Connection, session_id: str) -> None:
    await db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    await db.commit()


async def update_connected_clients(db: aiosqlite.Connection, session_id: str, count: int) -> None:
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "UPDATE sessions SET connected_clients = ?, updated_at = ? WHERE id = ?",
        (count, now, session_id),
    )
    await db.commit()


async def set_disconnect_deadline(
    db: aiosqlite.Connection,
    session_id: str,
    last_disconnected_at: datetime,
    deadline: datetime,
) -> None:
    await db.execute(
        """UPDATE sessions SET
            last_disconnected_at = ?, disconnect_deadline_at = ?, updated_at = ?
        WHERE id = ?""",
        (
            last_disconnected_at.isoformat(),
            deadline.isoformat(),
            datetime.now(UTC).isoformat(),
            session_id,
        ),
    )
    await db.commit()


async def set_last_connected(db: aiosqlite.Connection, session_id: str) -> None:
    now = datetime.now(UTC)
    await db.execute(
        """UPDATE sessions SET
            last_connected_at = ?, disconnect_deadline_at = NULL,
            last_disconnected_at = NULL, updated_at = ?
        WHERE id = ?""",
        (now.isoformat(), now.isoformat(), session_id),
    )
    await db.commit()

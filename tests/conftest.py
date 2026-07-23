from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from tests.support.session_harness import open_migrated_database


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    connection = await open_migrated_database(tmp_path / "test.db")
    try:
        yield connection
    finally:
        await connection.close()

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

from agent.storage.sqlite import init_db, resolve_sqlite_path


@asynccontextmanager
async def open_connection(
    sqlite_path: str | Path | None,
) -> AsyncIterator[aiosqlite.Connection]:
    db_path = resolve_sqlite_path(sqlite_path)
    await init_db(sqlite_path=db_path)

    async with aiosqlite.connect(db_path) as connection:
        await connection.execute("PRAGMA foreign_keys = ON;")
        connection.row_factory = aiosqlite.Row
        yield connection


async def ensure_run(
    connection: aiosqlite.Connection,
    run_id: str,
    started_at: str | None = None,
) -> None:
    started = started_at or datetime.now(UTC).isoformat()
    await connection.execute(
        """
        INSERT OR IGNORE INTO runs (run_id, mode, status, started_at, metadata_json)
        VALUES (?, 'manual', 'running', ?, '{}');
        """,
        (run_id, started),
    )


def dumps_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def loads_json(payload: str | None, default: Any) -> Any:
    if payload is None:
        return default
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return default

"""Copy-on-write recording versions backed by SQLite."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS recording_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    step_ids_json TEXT NOT NULL DEFAULT '[]',
    steps_snapshot_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (run_id, name)
);
CREATE INDEX IF NOT EXISTS idx_recording_versions_run ON recording_versions (run_id);
"""


class RecordingVersions:
    """Manage named step-subset versions for a run."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def _ensure_table(self, conn: aiosqlite.Connection) -> None:
        await conn.executescript(_CREATE_TABLE)
        await conn.commit()

    async def save_version(
        self,
        run_id: str,
        name: str,
        step_ids: list[str],
        all_steps: list[dict[str, Any]],
    ) -> None:
        """Save a named version as a copy-on-write snapshot of selected steps."""
        steps_snapshot = [s for s in all_steps if s.get("stepId") in set(step_ids)]
        async with aiosqlite.connect(self._db_path) as conn:
            await self._ensure_table(conn)
            await conn.execute(
                """
                INSERT INTO recording_versions (run_id, name, step_ids_json, steps_snapshot_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id, name) DO UPDATE SET
                    step_ids_json = excluded.step_ids_json,
                    steps_snapshot_json = excluded.steps_snapshot_json,
                    created_at = CURRENT_TIMESTAMP
                """,
                (run_id, name, json.dumps(step_ids), json.dumps(steps_snapshot)),
            )
            await conn.commit()
        logger.info("version_saved run_id=%s name=%s step_count=%d", run_id, name, len(steps_snapshot))

    async def load_version(
        self,
        run_id: str,
        name: str,
    ) -> list[dict[str, Any]] | None:
        """Load a version's steps snapshot. Returns None if not found."""
        async with aiosqlite.connect(self._db_path) as conn:
            await self._ensure_table(conn)
            cursor = await conn.execute(
                "SELECT steps_snapshot_json FROM recording_versions WHERE run_id = ? AND name = ?",
                (run_id, name),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return json.loads(row[0])

    async def list_versions(self, run_id: str) -> list[dict[str, Any]]:
        """List all versions for a run with name and step count."""
        async with aiosqlite.connect(self._db_path) as conn:
            await self._ensure_table(conn)
            cursor = await conn.execute(
                """
                SELECT name, step_ids_json, created_at
                FROM recording_versions
                WHERE run_id = ?
                ORDER BY created_at ASC
                """,
                (run_id,),
            )
            rows = await cursor.fetchall()
            versions = []
            for name, step_ids_json, created_at in rows:
                step_ids = json.loads(step_ids_json)
                versions.append({
                    "name": name,
                    "stepCount": len(step_ids),
                    "createdAt": created_at,
                })
            return versions

    async def delete_version(self, run_id: str, name: str) -> None:
        async with aiosqlite.connect(self._db_path) as conn:
            await self._ensure_table(conn)
            await conn.execute(
                "DELETE FROM recording_versions WHERE run_id = ? AND name = ?",
                (run_id, name),
            )
            await conn.commit()

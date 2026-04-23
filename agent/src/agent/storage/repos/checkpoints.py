from __future__ import annotations

from pathlib import Path

from agent.execution.checkpoint import Checkpoint
from agent.storage.repos._common import dumps_json, ensure_run, loads_json, open_connection


class CheckpointRepository:
    def __init__(self, sqlite_path: str | Path | None = None) -> None:
        self._sqlite_path = sqlite_path

    async def save(self, run_id: str, checkpoint: Checkpoint) -> None:
        payload = checkpoint.model_dump(mode="json", by_alias=False)
        async with open_connection(self._sqlite_path) as connection:
            await ensure_run(connection, run_id=run_id)
            await connection.execute(
                """
                INSERT INTO checkpoints (
                    run_id,
                    current_step_id,
                    event_offset,
                    browser_session_id,
                    tab_id,
                    frame_path_json,
                    storage_state_ref,
                    paused_recovery_state_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    run_id,
                    payload["current_step_id"],
                    payload["event_offset"],
                    payload["browser_session_id"],
                    payload["tab_id"],
                    dumps_json(payload.get("frame_path", [])),
                    payload.get("storage_state_ref"),
                    dumps_json(payload["paused_recovery_state"])
                    if payload.get("paused_recovery_state") is not None
                    else None,
                ),
            )
            await connection.commit()

    async def load_latest(self, run_id: str) -> Checkpoint | None:
        async with open_connection(self._sqlite_path) as connection:
            cursor = await connection.execute(
                """
                SELECT
                    current_step_id,
                    event_offset,
                    browser_session_id,
                    tab_id,
                    frame_path_json,
                    storage_state_ref,
                    paused_recovery_state_json
                FROM checkpoints
                WHERE run_id = ?
                ORDER BY checkpoint_id DESC
                LIMIT 1;
                """,
                (run_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_checkpoint(row)

    async def load_for_run(
        self,
        run_id: str,
        current_step_id: str | None = None,
        limit: int = 200,
    ) -> list[Checkpoint]:
        query = (
            "SELECT current_step_id, event_offset, browser_session_id, tab_id, "
            "frame_path_json, storage_state_ref, paused_recovery_state_json "
            "FROM checkpoints WHERE run_id = ?"
        )
        params: list[str | int] = [run_id]
        if current_step_id is not None:
            query += " AND current_step_id = ?"
            params.append(current_step_id)
        query += " ORDER BY checkpoint_id DESC LIMIT ?"
        params.append(limit)

        async with open_connection(self._sqlite_path) as connection:
            cursor = await connection.execute(query, tuple(params))
            rows = await cursor.fetchall()
        return [_row_to_checkpoint(row) for row in rows]


def _row_to_checkpoint(row: object) -> Checkpoint:
    row_data = dict(row)
    return Checkpoint.model_validate(
        {
            "current_step_id": row_data["current_step_id"],
            "event_offset": row_data["event_offset"],
            "browser_session_id": row_data["browser_session_id"],
            "tab_id": row_data["tab_id"],
            "frame_path": loads_json(row_data["frame_path_json"], []),
            "storage_state_ref": row_data["storage_state_ref"],
            "paused_recovery_state": loads_json(
                row_data["paused_recovery_state_json"], None
            ),
        }
    )

from __future__ import annotations

from pathlib import Path

from agent.cache.models import CacheRecord, ContextFingerprint
from agent.storage.repos._common import dumps_json, ensure_run, loads_json, open_connection


class CacheRepository:
    def __init__(self, sqlite_path: str | Path | None = None) -> None:
        self._sqlite_path = sqlite_path

    async def save(self, record: CacheRecord) -> None:
        payload = record.model_dump(mode="json", by_alias=False)
        fingerprint = payload["fingerprint"]
        async with open_connection(self._sqlite_path) as connection:
            await ensure_run(
                connection,
                run_id=payload["run_id"],
                started_at=payload["created_at"],
            )
            await connection.execute(
                """
                INSERT INTO cache_records (
                    run_id,
                    step_id,
                    route_template,
                    dom_hash,
                    frame_hash,
                    modal_state,
                    decision,
                    decision_reasons_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    payload["run_id"],
                    payload["step_id"],
                    fingerprint["route_template"],
                    fingerprint["dom_hash"],
                    fingerprint["frame_hash"],
                    fingerprint["modal_state"],
                    payload["decision"],
                    dumps_json(payload.get("decision_reasons", [])),
                    payload["created_at"],
                ),
            )
            await connection.commit()

    async def load_latest(self, run_id: str, step_id: str) -> CacheRecord | None:
        async with open_connection(self._sqlite_path) as connection:
            cursor = await connection.execute(
                """
                SELECT
                    run_id,
                    step_id,
                    route_template,
                    dom_hash,
                    frame_hash,
                    modal_state,
                    decision,
                    decision_reasons_json,
                    created_at
                FROM cache_records
                WHERE run_id = ? AND step_id = ?
                ORDER BY cache_record_id DESC
                LIMIT 1;
                """,
                (run_id, step_id),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_cache_record(row)

    async def load_for_run(
        self,
        run_id: str,
        step_id: str | None = None,
        limit: int = 500,
    ) -> list[CacheRecord]:
        query = (
            "SELECT run_id, step_id, route_template, dom_hash, frame_hash, modal_state, "
            "decision, decision_reasons_json, created_at FROM cache_records WHERE run_id = ?"
        )
        params: list[str | int] = [run_id]
        if step_id is not None:
            query += " AND step_id = ?"
            params.append(step_id)
        query += " ORDER BY cache_record_id DESC LIMIT ?"
        params.append(limit)

        async with open_connection(self._sqlite_path) as connection:
            cursor = await connection.execute(query, tuple(params))
            rows = await cursor.fetchall()
        return [_row_to_cache_record(row) for row in rows]


def _row_to_cache_record(row: object) -> CacheRecord:
    row_data = dict(row)
    fingerprint = ContextFingerprint.model_validate(
        {
            "route_template": row_data["route_template"],
            "dom_hash": row_data["dom_hash"],
            "frame_hash": row_data["frame_hash"],
            "modal_state": row_data["modal_state"],
        }
    )
    return CacheRecord.model_validate(
        {
            "run_id": row_data["run_id"],
            "step_id": row_data["step_id"],
            "fingerprint": fingerprint.model_dump(mode="python", by_alias=False),
            "decision": row_data["decision"],
            "decision_reasons": loads_json(row_data["decision_reasons_json"], []),
            "created_at": row_data["created_at"],
        }
    )

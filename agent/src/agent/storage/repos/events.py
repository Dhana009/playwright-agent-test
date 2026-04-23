from __future__ import annotations

from pathlib import Path

from agent.execution.events import Event
from agent.storage.repos._common import dumps_json, ensure_run, loads_json, open_connection


class EventRepository:
    def __init__(self, sqlite_path: str | Path | None = None) -> None:
        self._sqlite_path = sqlite_path

    async def save(self, event: Event) -> None:
        payload = event.model_dump(mode="json")
        async with open_connection(self._sqlite_path) as connection:
            await ensure_run(connection, run_id=payload["run_id"], started_at=payload["ts"])
            await connection.execute(
                """
                INSERT INTO events (
                    event_id,
                    run_id,
                    step_id,
                    actor,
                    type,
                    ts,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    payload["event_id"],
                    payload["run_id"],
                    payload.get("step_id"),
                    payload["actor"],
                    payload["type"],
                    payload["ts"],
                    dumps_json(payload.get("payload", {})),
                ),
            )
            await connection.commit()

    async def load(self, event_id: str) -> Event | None:
        async with open_connection(self._sqlite_path) as connection:
            cursor = await connection.execute(
                """
                SELECT event_id, run_id, step_id, actor, type, ts, payload_json
                FROM events
                WHERE event_id = ?;
                """,
                (event_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_event(row)

    async def load_for_run(
        self,
        run_id: str,
        step_id: str | None = None,
        limit: int = 500,
    ) -> list[Event]:
        query = (
            "SELECT event_id, run_id, step_id, actor, type, ts, payload_json "
            "FROM events WHERE run_id = ?"
        )
        params: list[str | int] = [run_id]
        if step_id is not None:
            query += " AND step_id = ?"
            params.append(step_id)
        query += " ORDER BY ts ASC, event_id ASC LIMIT ?"
        params.append(limit)

        async with open_connection(self._sqlite_path) as connection:
            cursor = await connection.execute(query, tuple(params))
            rows = await cursor.fetchall()
        return [_row_to_event(row) for row in rows]


def _row_to_event(row: object) -> Event:
    row_data = dict(row)
    return Event.model_validate(
        {
            "event_id": row_data["event_id"],
            "run_id": row_data["run_id"],
            "step_id": row_data["step_id"],
            "actor": row_data["actor"],
            "type": row_data["type"],
            "ts": row_data["ts"],
            "payload": loads_json(row_data["payload_json"], {}),
        }
    )

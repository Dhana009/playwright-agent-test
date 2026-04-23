from __future__ import annotations

from pathlib import Path

from agent.stepgraph.models import StepGraph
from agent.storage.repos._common import dumps_json, ensure_run, loads_json, open_connection


class StepGraphRepository:
    def __init__(self, sqlite_path: str | Path | None = None) -> None:
        self._sqlite_path = sqlite_path

    async def save(self, graph: StepGraph) -> None:
        payload = graph.model_dump(mode="json", by_alias=True)
        async with open_connection(self._sqlite_path) as connection:
            await ensure_run(connection, run_id=payload["runId"])
            await connection.execute(
                """
                INSERT INTO step_graph (run_id, version, graph_json, created_at, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(run_id) DO UPDATE SET
                    version = excluded.version,
                    graph_json = excluded.graph_json,
                    updated_at = CURRENT_TIMESTAMP;
                """,
                (
                    payload["runId"],
                    payload.get("version", "1.0"),
                    dumps_json(payload),
                ),
            )
            await connection.commit()

    async def load(self, run_id: str) -> StepGraph | None:
        async with open_connection(self._sqlite_path) as connection:
            cursor = await connection.execute(
                "SELECT graph_json FROM step_graph WHERE run_id = ?;",
                (run_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        data = loads_json(dict(row)["graph_json"], None)
        if data is None:
            return None
        return StepGraph.model_validate(data)


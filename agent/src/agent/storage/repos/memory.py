from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from agent.memory.models import CompiledMemoryEntry, LearnedRepair, RawEvidence
from agent.storage.repos._common import dumps_json, ensure_run, loads_json, open_connection


class MemoryRepository:
    def __init__(self, sqlite_path: str | Path | None = None) -> None:
        self._sqlite_path = sqlite_path

    async def save_raw_evidence(self, evidence: RawEvidence) -> None:
        payload = evidence.model_dump(mode="json", by_alias=False)
        async with open_connection(self._sqlite_path) as connection:
            await ensure_run(
                connection,
                run_id=payload["run_id"],
                started_at=payload["captured_at"],
            )
            await connection.execute(
                """
                INSERT INTO raw_evidence (
                    evidence_id,
                    run_id,
                    step_id,
                    actor,
                    evidence_type,
                    artifact_ref,
                    captured_at,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    payload["evidence_id"],
                    payload["run_id"],
                    payload.get("step_id"),
                    payload["actor"],
                    payload["evidence_type"],
                    payload["artifact_ref"],
                    payload["captured_at"],
                    dumps_json(payload.get("metadata", {})),
                ),
            )
            await connection.commit()

    async def load_raw_evidence(self, evidence_id: str) -> RawEvidence | None:
        async with open_connection(self._sqlite_path) as connection:
            cursor = await connection.execute(
                """
                SELECT
                    evidence_id,
                    run_id,
                    step_id,
                    actor,
                    evidence_type,
                    artifact_ref,
                    captured_at,
                    metadata_json
                FROM raw_evidence
                WHERE evidence_id = ?;
                """,
                (evidence_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_raw_evidence(row)

    async def load_raw_evidence_for_run(
        self,
        run_id: str,
        step_id: str | None = None,
        limit: int = 500,
    ) -> list[RawEvidence]:
        query = (
            "SELECT evidence_id, run_id, step_id, actor, evidence_type, artifact_ref, "
            "captured_at, metadata_json FROM raw_evidence WHERE run_id = ?"
        )
        params: list[str | int] = [run_id]
        if step_id is not None:
            query += " AND step_id = ?"
            params.append(step_id)
        query += " ORDER BY captured_at DESC, evidence_id DESC LIMIT ?"
        params.append(limit)

        async with open_connection(self._sqlite_path) as connection:
            cursor = await connection.execute(query, tuple(params))
            rows = await cursor.fetchall()
        return [_row_to_raw_evidence(row) for row in rows]

    async def save_compiled_entry(
        self,
        entry: CompiledMemoryEntry,
    ) -> CompiledMemoryEntry:
        payload = entry.model_dump(mode="json", by_alias=False)
        async with open_connection(self._sqlite_path) as connection:
            cursor = await connection.execute(
                "SELECT version FROM compiled_memory WHERE entry_id = ?;",
                (payload["entry_id"],),
            )
            existing = await cursor.fetchone()

            if existing is not None and payload["version"] <= int(existing["version"]):
                payload["version"] = int(existing["version"]) + 1

            await connection.execute(
                """
                INSERT INTO compiled_memory (
                    entry_id,
                    entry_type,
                    key,
                    value_json,
                    version,
                    raw_evidence_ids_json,
                    confidence_score,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(entry_id) DO UPDATE SET
                    entry_type = excluded.entry_type,
                    key = excluded.key,
                    value_json = excluded.value_json,
                    version = excluded.version,
                    raw_evidence_ids_json = excluded.raw_evidence_ids_json,
                    confidence_score = excluded.confidence_score,
                    updated_at = excluded.updated_at;
                """,
                (
                    payload["entry_id"],
                    payload["entry_type"],
                    payload["key"],
                    dumps_json(payload.get("value", {})),
                    payload["version"],
                    dumps_json(payload.get("raw_evidence_ids", [])),
                    payload.get("confidence_score"),
                    payload["updated_at"],
                ),
            )
            await connection.commit()

        return entry.model_copy(update={"version": payload["version"]})

    async def load_compiled_entry(self, entry_id: str) -> CompiledMemoryEntry | None:
        async with open_connection(self._sqlite_path) as connection:
            cursor = await connection.execute(
                """
                SELECT
                    entry_id,
                    entry_type,
                    key,
                    value_json,
                    version,
                    raw_evidence_ids_json,
                    confidence_score,
                    updated_at
                FROM compiled_memory
                WHERE entry_id = ?;
                """,
                (entry_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_compiled_entry(row)

    async def load_compiled_entries(
        self,
        entry_type: str | None = None,
        key: str | None = None,
        limit: int = 500,
    ) -> list[CompiledMemoryEntry]:
        query = (
            "SELECT entry_id, entry_type, key, value_json, version, raw_evidence_ids_json, "
            "confidence_score, updated_at FROM compiled_memory WHERE 1 = 1"
        )
        params: list[str | int] = []
        if entry_type is not None:
            query += " AND entry_type = ?"
            params.append(entry_type)
        if key is not None:
            query += " AND key = ?"
            params.append(key)
        query += " ORDER BY updated_at DESC, entry_id DESC LIMIT ?"
        params.append(limit)

        async with open_connection(self._sqlite_path) as connection:
            cursor = await connection.execute(query, tuple(params))
            rows = await cursor.fetchall()
        return [_row_to_compiled_entry(row) for row in rows]

    async def save_learned_repair(self, repair: LearnedRepair) -> None:
        payload = repair.model_dump(mode="json", by_alias=False)
        timestamp = datetime.now(UTC).isoformat()
        async with open_connection(self._sqlite_path) as connection:
            await connection.execute(
                """
                INSERT INTO learned_repairs (
                    repair_id,
                    scope_key,
                    state,
                    domain,
                    normalized_route_template,
                    frame_context_json,
                    target_semantic_key,
                    app_version,
                    source_run_id,
                    source_step_id,
                    actor,
                    confidence_score,
                    validation_success_count,
                    validation_failure_count,
                    last_validated_at,
                    expires_at,
                    rollback_ref,
                    metadata_json,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repair_id) DO UPDATE SET
                    scope_key = excluded.scope_key,
                    state = excluded.state,
                    domain = excluded.domain,
                    normalized_route_template = excluded.normalized_route_template,
                    frame_context_json = excluded.frame_context_json,
                    target_semantic_key = excluded.target_semantic_key,
                    app_version = excluded.app_version,
                    source_run_id = excluded.source_run_id,
                    source_step_id = excluded.source_step_id,
                    actor = excluded.actor,
                    confidence_score = excluded.confidence_score,
                    validation_success_count = excluded.validation_success_count,
                    validation_failure_count = excluded.validation_failure_count,
                    last_validated_at = excluded.last_validated_at,
                    expires_at = excluded.expires_at,
                    rollback_ref = excluded.rollback_ref,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at;
                """,
                (
                    payload["repair_id"],
                    payload["scope_key"],
                    payload["state"],
                    payload["domain"],
                    payload["normalized_route_template"],
                    dumps_json(payload.get("frame_context", [])),
                    payload.get("target_semantic_key"),
                    payload.get("app_version"),
                    payload["source_run_id"],
                    payload["source_step_id"],
                    payload["actor"],
                    payload["confidence_score"],
                    payload.get("validation_success_count", 0),
                    payload.get("validation_failure_count", 0),
                    payload.get("last_validated_at"),
                    payload.get("expires_at"),
                    payload.get("rollback_ref"),
                    dumps_json(payload.get("metadata", {})),
                    timestamp,
                    timestamp,
                ),
            )
            await connection.commit()

    async def load_learned_repair(self, repair_id: str) -> LearnedRepair | None:
        async with open_connection(self._sqlite_path) as connection:
            cursor = await connection.execute(
                """
                SELECT
                    repair_id,
                    scope_key,
                    state,
                    domain,
                    normalized_route_template,
                    frame_context_json,
                    target_semantic_key,
                    app_version,
                    source_run_id,
                    source_step_id,
                    actor,
                    confidence_score,
                    validation_success_count,
                    validation_failure_count,
                    last_validated_at,
                    expires_at,
                    rollback_ref,
                    metadata_json
                FROM learned_repairs
                WHERE repair_id = ?;
                """,
                (repair_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_learned_repair(row)

    async def load_learned_repairs(
        self,
        source_run_id: str | None = None,
        source_step_id: str | None = None,
        scope_key: str | None = None,
        state: str | None = None,
        limit: int = 500,
    ) -> list[LearnedRepair]:
        query = (
            "SELECT repair_id, scope_key, state, domain, normalized_route_template, "
            "frame_context_json, target_semantic_key, app_version, source_run_id, "
            "source_step_id, actor, confidence_score, validation_success_count, "
            "validation_failure_count, last_validated_at, expires_at, rollback_ref, "
            "metadata_json FROM learned_repairs WHERE 1 = 1"
        )
        params: list[str | int] = []
        if source_run_id is not None:
            query += " AND source_run_id = ?"
            params.append(source_run_id)
        if source_step_id is not None:
            query += " AND source_step_id = ?"
            params.append(source_step_id)
        if scope_key is not None:
            query += " AND scope_key = ?"
            params.append(scope_key)
        if state is not None:
            query += " AND state = ?"
            params.append(state)
        query += " ORDER BY updated_at DESC, repair_id DESC LIMIT ?"
        params.append(limit)

        async with open_connection(self._sqlite_path) as connection:
            cursor = await connection.execute(query, tuple(params))
            rows = await cursor.fetchall()
        return [_row_to_learned_repair(row) for row in rows]


def _row_to_raw_evidence(row: object) -> RawEvidence:
    row_data = dict(row)
    return RawEvidence.model_validate(
        {
            "evidence_id": row_data["evidence_id"],
            "run_id": row_data["run_id"],
            "step_id": row_data["step_id"],
            "actor": row_data["actor"],
            "evidence_type": row_data["evidence_type"],
            "artifact_ref": row_data["artifact_ref"],
            "captured_at": row_data["captured_at"],
            "metadata": loads_json(row_data["metadata_json"], {}),
        }
    )


def _row_to_compiled_entry(row: object) -> CompiledMemoryEntry:
    row_data = dict(row)
    return CompiledMemoryEntry.model_validate(
        {
            "entry_id": row_data["entry_id"],
            "entry_type": row_data["entry_type"],
            "key": row_data["key"],
            "value": loads_json(row_data["value_json"], {}),
            "version": row_data["version"],
            "raw_evidence_ids": loads_json(row_data["raw_evidence_ids_json"], []),
            "confidence_score": row_data["confidence_score"],
            "updated_at": row_data["updated_at"],
        }
    )


def _row_to_learned_repair(row: object) -> LearnedRepair:
    row_data = dict(row)
    return LearnedRepair.model_validate(
        {
            "repair_id": row_data["repair_id"],
            "scope_key": row_data["scope_key"],
            "state": row_data["state"],
            "domain": row_data["domain"],
            "normalized_route_template": row_data["normalized_route_template"],
            "frame_context": loads_json(row_data["frame_context_json"], []),
            "target_semantic_key": row_data["target_semantic_key"],
            "app_version": row_data["app_version"],
            "source_run_id": row_data["source_run_id"],
            "source_step_id": row_data["source_step_id"],
            "actor": row_data["actor"],
            "confidence_score": row_data["confidence_score"],
            "validation_success_count": row_data["validation_success_count"],
            "validation_failure_count": row_data["validation_failure_count"],
            "last_validated_at": row_data["last_validated_at"],
            "expires_at": row_data["expires_at"],
            "rollback_ref": row_data["rollback_ref"],
            "metadata": loads_json(row_data["metadata_json"], {}),
        }
    )

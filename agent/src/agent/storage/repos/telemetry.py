from __future__ import annotations

from pathlib import Path

import aiosqlite

from agent.storage.repos._common import dumps_json, ensure_run, loads_json, open_connection
from agent.telemetry.models import LLMCall, RunMode, RunSummary
from agent.telemetry.summary import apply_llm_call_to_summary, build_initial_run_summary


class TelemetryRepository:
    def __init__(self, sqlite_path: str | Path | None = None) -> None:
        self._sqlite_path = sqlite_path

    async def save_llm_call(self, call: LLMCall) -> None:
        await self.record_llm_call(call)

    async def record_llm_call(self, call: LLMCall) -> RunSummary:
        payload = call.model_dump(mode="json", by_alias=False)
        async with open_connection(self._sqlite_path) as connection:
            await ensure_run(
                connection,
                run_id=payload["run_id"],
                started_at=payload["created_at"],
            )

            await _insert_llm_call(connection, payload)

            summary = await _load_or_initialize_summary(
                connection=connection,
                run_id=payload["run_id"],
                fallback_started_at=payload["created_at"],
            )
            summary = apply_llm_call_to_summary(summary, call)
            await _save_summary(connection, summary)
            await connection.commit()
            return summary

    async def save_run_summary(self, summary: RunSummary) -> None:
        async with open_connection(self._sqlite_path) as connection:
            await ensure_run(
                connection,
                run_id=summary.run_id,
                started_at=summary.started_at.isoformat(),
            )
            await _save_summary(connection, summary)
            await connection.commit()

    async def load_run_summary(self, run_id: str) -> RunSummary | None:
        async with open_connection(self._sqlite_path) as connection:
            return await _load_summary(connection, run_id)

    async def load_llm_call(self, call_id: str) -> LLMCall | None:
        async with open_connection(self._sqlite_path) as connection:
            cursor = await connection.execute(
                """
                SELECT
                    call_id,
                    run_id,
                    step_id,
                    provider,
                    model,
                    call_purpose,
                    context_tier,
                    escalation_path_json,
                    input_tokens,
                    output_tokens,
                    preflight_input_tokens,
                    preflight_output_tokens,
                    cache_read,
                    cache_write,
                    prompt_cache_hit,
                    est_cost,
                    actual_cost,
                    latency_ms,
                    no_progress_retry,
                    created_at
                FROM llm_calls
                WHERE call_id = ?;
                """,
                (call_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_llm_call(row)

    async def load_llm_calls_for_run(
        self,
        run_id: str,
        step_id: str | None = None,
        limit: int = 500,
    ) -> list[LLMCall]:
        query = (
            "SELECT call_id, run_id, step_id, provider, model, call_purpose, context_tier, "
            "escalation_path_json, input_tokens, output_tokens, preflight_input_tokens, "
            "preflight_output_tokens, cache_read, cache_write, prompt_cache_hit, est_cost, "
            "actual_cost, latency_ms, no_progress_retry, created_at FROM llm_calls WHERE run_id = ?"
        )
        params: list[str | int] = [run_id]
        if step_id is not None:
            query += " AND step_id = ?"
            params.append(step_id)
        query += " ORDER BY created_at DESC, call_id DESC LIMIT ?"
        params.append(limit)

        async with open_connection(self._sqlite_path) as connection:
            cursor = await connection.execute(query, tuple(params))
            rows = await cursor.fetchall()
        return [_row_to_llm_call(row) for row in rows]


def _row_to_llm_call(row: object) -> LLMCall:
    row_data = dict(row)
    return LLMCall.model_validate(
        {
            "call_id": row_data["call_id"],
            "run_id": row_data["run_id"],
            "step_id": row_data["step_id"],
            "provider": row_data["provider"],
            "model": row_data["model"],
            "call_purpose": row_data["call_purpose"],
            "context_tier": row_data["context_tier"],
            "escalation_path": loads_json(row_data["escalation_path_json"], []),
            "input_tokens": row_data["input_tokens"],
            "output_tokens": row_data["output_tokens"],
            "preflight_input_tokens": row_data["preflight_input_tokens"],
            "preflight_output_tokens": row_data["preflight_output_tokens"],
            "cache_read": row_data["cache_read"],
            "cache_write": row_data["cache_write"],
            "prompt_cache_hit": _decode_optional_bool(row_data["prompt_cache_hit"]),
            "est_cost": row_data["est_cost"],
            "actual_cost": row_data["actual_cost"],
            "latency_ms": row_data["latency_ms"],
            "no_progress_retry": bool(row_data["no_progress_retry"]),
            "created_at": row_data["created_at"],
        }
    )


async def _insert_llm_call(
    connection: aiosqlite.Connection,
    payload: dict[str, object],
) -> None:
    await connection.execute(
        """
        INSERT INTO llm_calls (
            call_id,
            run_id,
            step_id,
            provider,
            model,
            call_purpose,
            context_tier,
            escalation_path_json,
            input_tokens,
            output_tokens,
            preflight_input_tokens,
            preflight_output_tokens,
            cache_read,
            cache_write,
            prompt_cache_hit,
            est_cost,
            actual_cost,
            latency_ms,
            no_progress_retry,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            payload["call_id"],
            payload["run_id"],
            payload.get("step_id"),
            payload["provider"],
            payload["model"],
            payload["call_purpose"],
            payload["context_tier"],
            dumps_json(payload.get("escalation_path", [])),
            payload.get("input_tokens", 0),
            payload.get("output_tokens", 0),
            payload.get("preflight_input_tokens", 0),
            payload.get("preflight_output_tokens", 0),
            payload.get("cache_read", 0),
            payload.get("cache_write", 0),
            _encode_optional_bool(payload.get("prompt_cache_hit")),
            payload.get("est_cost", 0.0),
            payload.get("actual_cost", 0.0),
            payload.get("latency_ms", 0),
            int(bool(payload.get("no_progress_retry", False))),
            payload["created_at"],
        ),
    )


async def _load_or_initialize_summary(
    *,
    connection: aiosqlite.Connection,
    run_id: str,
    fallback_started_at: str,
) -> RunSummary:
    existing = await _load_summary(connection, run_id)
    if existing is not None:
        return existing

    cursor = await connection.execute(
        "SELECT mode, started_at FROM runs WHERE run_id = ?;",
        (run_id,),
    )
    row = await cursor.fetchone()
    mode = _parse_run_mode(row["mode"] if row is not None else "manual")
    started_at = row["started_at"] if row is not None else fallback_started_at
    return build_initial_run_summary(
        run_id=run_id,
        mode=mode,
        started_at=started_at,
    )


async def _load_summary(
    connection: aiosqlite.Connection,
    run_id: str,
) -> RunSummary | None:
    cursor = await connection.execute(
        "SELECT metadata_json FROM runs WHERE run_id = ?;",
        (run_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None

    metadata = loads_json(row["metadata_json"], {})
    if not isinstance(metadata, dict):
        return None
    summary_payload = metadata.get("run_summary")
    if not isinstance(summary_payload, dict):
        return None
    return RunSummary.model_validate(summary_payload)


async def _save_summary(
    connection: aiosqlite.Connection,
    summary: RunSummary,
) -> None:
    cursor = await connection.execute(
        "SELECT metadata_json FROM runs WHERE run_id = ?;",
        (summary.run_id,),
    )
    row = await cursor.fetchone()
    metadata = loads_json(row["metadata_json"] if row is not None else None, {})
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["run_summary"] = summary.model_dump(mode="json", by_alias=False)
    await connection.execute(
        "UPDATE runs SET metadata_json = ? WHERE run_id = ?;",
        (dumps_json(metadata), summary.run_id),
    )


def _parse_run_mode(mode: str) -> RunMode:
    try:
        return RunMode(mode)
    except ValueError:
        return RunMode.MANUAL


def _encode_optional_bool(value: bool | None) -> int | None:
    if value is None:
        return None
    return int(value)


def _decode_optional_bool(value: int | None) -> bool | None:
    if value is None:
        return None
    return bool(value)

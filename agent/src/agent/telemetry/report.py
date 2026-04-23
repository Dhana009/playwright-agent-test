from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field

from agent.core.logging import get_logger
from agent.storage.files import get_run_layout
from agent.storage.repos._common import loads_json, open_connection

LOGGER = get_logger(__name__)
NO_PROGRESS_BURN_RATE_ALERT_THRESHOLD = 0.10
NO_PROGRESS_CLUSTER_CALL_BUDGET = 3


class NoProgressRetryCluster(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    step_id: str | None = Field(default=None, alias="stepId")
    call_purpose: str = Field(alias="callPurpose")
    call_count: int = Field(alias="callCount")
    token_count: int = Field(alias="tokenCount")
    started_at: str = Field(alias="startedAt")
    ended_at: str = Field(alias="endedAt")


class RunBenchmarkReport(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    run_id: str = Field(alias="runId")
    mode: str
    status: str
    started_at: str = Field(alias="startedAt")
    ended_at: str | None = Field(default=None, alias="endedAt")
    generated_at: str = Field(alias="generatedAt")
    source: str
    counts: dict[str, int] = Field(default_factory=dict)
    kpis: dict[str, float | None] = Field(default_factory=dict)
    breakdowns: dict[str, Any] = Field(default_factory=dict)
    alerts: list[str] = Field(default_factory=list)


class RunReportWriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    run_id: str = Field(alias="runId")
    report_path: str = Field(alias="reportPath")


@dataclass(frozen=True)
class _RunRow:
    mode: str
    status: str
    started_at: str
    ended_at: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class _EventRow:
    event_type: str
    step_id: str | None
    ts: datetime
    payload: dict[str, Any]


@dataclass(frozen=True)
class _LLMCallRow:
    step_id: str | None
    call_purpose: str
    context_tier: str
    input_tokens: int
    output_tokens: int
    actual_cost: float
    prompt_cache_hit: bool | None
    no_progress_retry: bool
    created_at: datetime


@dataclass(frozen=True)
class _CacheDecisionRow:
    decision: str
    reasons: list[str]


@dataclass(frozen=True)
class _LearnedRepairRow:
    state: str
    validation_success_count: int
    validation_failure_count: int


class RunReportBuilder:
    def __init__(
        self,
        *,
        sqlite_path: str | Path | None = None,
        runs_root: str | Path | None = None,
    ) -> None:
        self._sqlite_path = sqlite_path
        self._runs_root = runs_root

    @classmethod
    def create(
        cls,
        *,
        sqlite_path: str | Path | None = None,
        runs_root: str | Path | None = None,
    ) -> "RunReportBuilder":
        return cls(sqlite_path=sqlite_path, runs_root=runs_root)

    async def build_report(self, *, run_id: str) -> RunBenchmarkReport:
        async with open_connection(self._sqlite_path) as connection:
            run_row = await _load_run_row(connection, run_id=run_id)
            if run_row is None:
                raise ValueError(f"Run not found for run_id={run_id}")

            events = await _load_events(connection, run_id=run_id)
            llm_calls = await _load_llm_calls(connection, run_id=run_id)
            cache_decisions = await _load_cache_decisions(connection, run_id=run_id)
            repairs = await _load_learned_repairs(connection, run_id=run_id)

        report = _compute_report(
            run_id=run_id,
            run_row=run_row,
            events=events,
            llm_calls=llm_calls,
            cache_decisions=cache_decisions,
            repairs=repairs,
        )
        LOGGER.info(
            "run_report_built",
            run_id=run_id,
            mode=report.mode,
            flow_completion_rate=report.kpis.get("flow_completion_rate"),
        )
        return report

    async def write_report(
        self,
        *,
        run_id: str,
        output_path: str | Path | None = None,
        report: RunBenchmarkReport | None = None,
    ) -> RunReportWriteResult:
        report_payload = report or await self.build_report(run_id=run_id)
        if output_path is None:
            destination = get_run_layout(run_id, self._runs_root).run_dir / "report.json"
        else:
            destination = Path(output_path)
            destination.parent.mkdir(parents=True, exist_ok=True)

        destination.write_text(
            report_payload.model_dump_json(indent=2, by_alias=True),
            encoding="utf-8",
        )
        LOGGER.info(
            "run_report_written",
            run_id=run_id,
            report_path=str(destination),
        )
        return RunReportWriteResult(runId=run_id, reportPath=str(destination))


async def _load_run_row(connection: aiosqlite.Connection, *, run_id: str) -> _RunRow | None:
    cursor = await connection.execute(
        """
        SELECT mode, status, started_at, ended_at, metadata_json
        FROM runs
        WHERE run_id = ?;
        """,
        (run_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None

    metadata = loads_json(row["metadata_json"], {})
    if not isinstance(metadata, dict):
        metadata = {}
    return _RunRow(
        mode=row["mode"],
        status=row["status"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        metadata=metadata,
    )


async def _load_events(connection: aiosqlite.Connection, *, run_id: str) -> list[_EventRow]:
    cursor = await connection.execute(
        """
        SELECT type, step_id, ts, payload_json
        FROM events
        WHERE run_id = ?
        ORDER BY ts ASC, event_id ASC;
        """,
        (run_id,),
    )
    rows = await cursor.fetchall()
    output: list[_EventRow] = []
    for row in rows:
        payload = loads_json(row["payload_json"], {})
        if not isinstance(payload, dict):
            payload = {}
        output.append(
            _EventRow(
                event_type=row["type"],
                step_id=row["step_id"],
                ts=_parse_dt(row["ts"]) or datetime.now(UTC),
                payload=payload,
            )
        )
    return output


async def _load_llm_calls(connection: aiosqlite.Connection, *, run_id: str) -> list[_LLMCallRow]:
    cursor = await connection.execute(
        """
        SELECT
            step_id,
            call_purpose,
            context_tier,
            input_tokens,
            output_tokens,
            actual_cost,
            prompt_cache_hit,
            no_progress_retry,
            created_at
        FROM llm_calls
        WHERE run_id = ?
        ORDER BY created_at ASC, call_id ASC;
        """,
        (run_id,),
    )
    rows = await cursor.fetchall()
    output: list[_LLMCallRow] = []
    for row in rows:
        prompt_hit_raw = row["prompt_cache_hit"]
        prompt_hit = None if prompt_hit_raw is None else bool(prompt_hit_raw)
        output.append(
            _LLMCallRow(
                step_id=row["step_id"],
                call_purpose=row["call_purpose"],
                context_tier=row["context_tier"],
                input_tokens=int(row["input_tokens"]),
                output_tokens=int(row["output_tokens"]),
                actual_cost=float(row["actual_cost"]),
                prompt_cache_hit=prompt_hit,
                no_progress_retry=bool(row["no_progress_retry"]),
                created_at=_parse_dt(row["created_at"]) or datetime.now(UTC),
            )
        )
    return output


async def _load_cache_decisions(
    connection: aiosqlite.Connection,
    *,
    run_id: str,
) -> list[_CacheDecisionRow]:
    cursor = await connection.execute(
        """
        SELECT decision, decision_reasons_json
        FROM cache_records
        WHERE run_id = ?
        ORDER BY cache_record_id ASC;
        """,
        (run_id,),
    )
    rows = await cursor.fetchall()
    output: list[_CacheDecisionRow] = []
    for row in rows:
        reasons = loads_json(row["decision_reasons_json"], [])
        if not isinstance(reasons, list):
            reasons = []
        normalized_reasons = [reason for reason in reasons if isinstance(reason, str) and reason.strip()]
        output.append(_CacheDecisionRow(decision=row["decision"], reasons=normalized_reasons))
    return output


async def _load_learned_repairs(
    connection: aiosqlite.Connection,
    *,
    run_id: str,
) -> list[_LearnedRepairRow]:
    cursor = await connection.execute(
        """
        SELECT state, validation_success_count, validation_failure_count
        FROM learned_repairs
        WHERE source_run_id = ?;
        """,
        (run_id,),
    )
    rows = await cursor.fetchall()
    return [
        _LearnedRepairRow(
            state=row["state"],
            validation_success_count=int(row["validation_success_count"]),
            validation_failure_count=int(row["validation_failure_count"]),
        )
        for row in rows
    ]


def _compute_report(
    *,
    run_id: str,
    run_row: _RunRow,
    events: list[_EventRow],
    llm_calls: list[_LLMCallRow],
    cache_decisions: list[_CacheDecisionRow],
    repairs: list[_LearnedRepairRow],
) -> RunBenchmarkReport:
    run_summary = run_row.metadata.get("run_summary")
    if not isinstance(run_summary, dict):
        run_summary = {}

    step_started_events = 0
    steps_started: set[str] = set()
    successful_steps: set[str] = set()
    first_pass_success_steps: set[str] = set()
    safely_escalated_steps: set[str] = set()

    pending_recovery_start: dict[str, datetime] = {}
    mttr_samples_ms: list[float] = []
    failure_incidents = 0

    run_completed_events = 0
    run_aborted_events = 0
    run_resumed_events = 0
    run_paused_events = 0

    for event in events:
        step_id = event.step_id
        if event.event_type == "step_started":
            step_started_events += 1
            if step_id:
                steps_started.add(step_id)
            continue

        if event.event_type == "step_succeeded":
            if step_id:
                successful_steps.add(step_id)
                if step_id in pending_recovery_start:
                    recovery_start = pending_recovery_start.pop(step_id)
                    mttr_samples_ms.append((event.ts - recovery_start).total_seconds() * 1000)

                attempt = event.payload.get("attempt")
                if isinstance(attempt, int) and attempt == 0:
                    first_pass_success_steps.add(step_id)
            continue

        if event.event_type in {"step_failed", "step_retried"}:
            failure_incidents += 1
            if step_id:
                pending_recovery_start[step_id] = event.ts
            continue

        if event.event_type == "intervention_recorded":
            if step_id:
                safely_escalated_steps.add(step_id)
            continue

        if event.event_type == "run_completed":
            run_completed_events += 1
            continue
        if event.event_type == "run_aborted":
            run_aborted_events += 1
            continue
        if event.event_type == "run_resumed":
            run_resumed_events += 1
            continue
        if event.event_type == "run_paused":
            run_paused_events += 1
            continue

    total_llm_calls = len(llm_calls)
    input_tokens = sum(call.input_tokens for call in llm_calls)
    output_tokens = sum(call.output_tokens for call in llm_calls)
    actual_cost = sum(call.actual_cost for call in llm_calls)
    total_tokens = input_tokens + output_tokens

    prompt_cache_hits = sum(1 for call in llm_calls if call.prompt_cache_hit is True)
    prompt_cache_misses = sum(1 for call in llm_calls if call.prompt_cache_hit is False)

    no_progress_tokens = sum(
        call.input_tokens + call.output_tokens for call in llm_calls if call.no_progress_retry
    )

    tier_resolution_by_purpose: dict[str, dict[str, int | float | None]] = {}
    for call in llm_calls:
        purpose = call.call_purpose
        if purpose not in tier_resolution_by_purpose:
            tier_resolution_by_purpose[purpose] = {
                "total_calls": 0,
                "tier0_calls": 0,
                "tier1_calls": 0,
                "tier2_or_higher_calls": 0,
                "tier0_tier1_ratio": None,
            }

        bucket = tier_resolution_by_purpose[purpose]
        bucket["total_calls"] = int(bucket["total_calls"]) + 1
        if call.context_tier == "tier0":
            bucket["tier0_calls"] = int(bucket["tier0_calls"]) + 1
        elif call.context_tier == "tier1":
            bucket["tier1_calls"] = int(bucket["tier1_calls"]) + 1
        else:
            bucket["tier2_or_higher_calls"] = int(bucket["tier2_or_higher_calls"]) + 1

    for purpose, bucket in tier_resolution_by_purpose.items():
        total_calls = int(bucket["total_calls"])
        tier0_calls = int(bucket["tier0_calls"])
        tier1_calls = int(bucket["tier1_calls"])
        bucket["tier0_tier1_ratio"] = _ratio(tier0_calls + tier1_calls, total_calls)
        tier_resolution_by_purpose[purpose] = bucket

    no_progress_clusters = _compute_no_progress_clusters(llm_calls)

    cache_reuse_decisions = sum(1 for row in cache_decisions if row.decision == "reuse")
    partial_refresh_decisions = sum(1 for row in cache_decisions if row.decision == "partial_refresh")
    full_refresh_decisions = sum(1 for row in cache_decisions if row.decision == "full_refresh")
    total_cache_decisions = len(cache_decisions)
    total_refresh_operations = partial_refresh_decisions + full_refresh_decisions

    refresh_reason_breakdown: dict[str, dict[str, float | int | None]] = {}
    for row in cache_decisions:
        if row.decision not in {"partial_refresh", "full_refresh"}:
            continue
        reasons = row.reasons or ["unspecified"]
        for reason in set(reasons):
            if reason not in refresh_reason_breakdown:
                refresh_reason_breakdown[reason] = {
                    "partial_refresh_count": 0,
                    "full_refresh_count": 0,
                    "total_count": 0,
                    "partial_refresh_ratio": None,
                    "full_refresh_ratio": None,
                }
            entry = refresh_reason_breakdown[reason]
            if row.decision == "partial_refresh":
                entry["partial_refresh_count"] = int(entry["partial_refresh_count"]) + 1
            if row.decision == "full_refresh":
                entry["full_refresh_count"] = int(entry["full_refresh_count"]) + 1
            entry["total_count"] = int(entry["total_count"]) + 1

    for reason, entry in refresh_reason_breakdown.items():
        partial_count = int(entry["partial_refresh_count"])
        full_count = int(entry["full_refresh_count"])
        total_count = int(entry["total_count"])
        entry["partial_refresh_ratio"] = _ratio(partial_count, total_count)
        entry["full_refresh_ratio"] = _ratio(full_count, total_count)
        refresh_reason_breakdown[reason] = entry

    repairs_promoted = sum(1 for repair in repairs if repair.state == "trusted")
    repair_validation_attempts = sum(
        1
        for repair in repairs
        if (repair.validation_success_count + repair.validation_failure_count) > 0
    )

    if repair_validation_attempts == 0:
        summary_attempts = _as_int(run_summary.get("repair_validation_attempts"), default=0)
        summary_promoted = _as_int(run_summary.get("repairs_promoted"), default=0)
        repair_validation_attempts = summary_attempts
        repairs_promoted = summary_promoted

    contradiction_count = _as_int(run_summary.get("contradiction_count"), default=0)
    contradiction_breakdown_raw = run_summary.get("contradiction_breakdown")
    if isinstance(contradiction_breakdown_raw, dict):
        contradiction_breakdown = {
            "stale_locator": _as_int(contradiction_breakdown_raw.get("stale_locator"), default=0),
            "content_drift": _as_int(contradiction_breakdown_raw.get("content_drift"), default=0),
            "structure_drift": _as_int(contradiction_breakdown_raw.get("structure_drift"), default=0),
        }
    else:
        contradiction_breakdown = {
            "stale_locator": 0,
            "content_drift": 0,
            "structure_drift": 0,
        }

    unique_steps_evaluated = len(steps_started)
    successful_step_count = len(successful_steps)
    safely_escalated_step_count = len(safely_escalated_steps)
    resolved_steps = len(successful_steps | safely_escalated_steps)

    flow_completed = bool(run_completed_events) or bool(run_summary.get("flow_completed", False))
    if run_aborted_events > 0 and run_completed_events == 0:
        flow_completed = False

    first_pass_step_success_rate = _ratio(len(first_pass_success_steps), unique_steps_evaluated)
    mttr_step_ms = _mean(mttr_samples_ms)
    restart_avoidance_rate = _ratio(
        max(failure_incidents - run_aborted_events, 0),
        failure_incidents,
    )
    prompt_cache_hit_ratio = _ratio(prompt_cache_hits, prompt_cache_hits + prompt_cache_misses)
    tier0_tier1_total = sum(
        int(bucket["tier0_calls"]) + int(bucket["tier1_calls"])
        for bucket in tier_resolution_by_purpose.values()
    )
    tier0_tier1_resolution_ratio = _ratio(tier0_tier1_total, total_llm_calls)

    kpis: dict[str, float | None] = {
        "tokens_per_successful_step": _ratio(total_tokens, successful_step_count),
        "cost_per_completed_flow": actual_cost if flow_completed else None,
        "llm_calls_per_completed_flow": _ratio(total_llm_calls, 1) if flow_completed else None,
        "input_tokens_per_call": _ratio(input_tokens, total_llm_calls),
        "output_tokens_per_call": _ratio(output_tokens, total_llm_calls),
        "token_efficiency_per_resolved_step": _ratio(resolved_steps, total_tokens),
        "prompt_cache_hit_ratio": prompt_cache_hit_ratio,
        "flow_completion_rate": 1.0 if flow_completed else 0.0,
        "first_pass_step_success_rate": first_pass_step_success_rate,
        "mttr_step_ms": mttr_step_ms,
        "restart_avoidance_rate": restart_avoidance_rate,
        "llm_assist_invocation_rate": _ratio(total_llm_calls, step_started_events),
        "context_reuse_ratio": _ratio(cache_reuse_decisions, total_cache_decisions),
        "cache_hit_rate": _ratio(cache_reuse_decisions, step_started_events),
        "partial_refresh_ratio": _ratio(partial_refresh_decisions, total_refresh_operations),
        "full_refresh_ratio": _ratio(full_refresh_decisions, total_refresh_operations),
        "contradiction_rate_per_100_steps": (
            _ratio(contradiction_count * 100, step_started_events) if step_started_events else None
        ),
        "repair_promotion_success_rate": _ratio(repairs_promoted, repair_validation_attempts),
        "tier0_tier1_resolution_ratio": tier0_tier1_resolution_ratio,
        "no_progress_token_burn_rate": _ratio(no_progress_tokens, total_tokens),
    }

    alerts: list[str] = []
    burn_rate = kpis["no_progress_token_burn_rate"]
    if burn_rate is not None and burn_rate > NO_PROGRESS_BURN_RATE_ALERT_THRESHOLD:
        alerts.append(
            "No-progress token burn rate exceeds threshold: "
            f"{burn_rate:.2%} > {NO_PROGRESS_BURN_RATE_ALERT_THRESHOLD:.0%}."
        )
    for cluster in no_progress_clusters:
        if cluster.call_count >= NO_PROGRESS_CLUSTER_CALL_BUDGET:
            alerts.append(
                "No-progress retry cluster exceeded call budget: "
                f"step={cluster.step_id or 'unknown'} purpose={cluster.call_purpose} "
                f"calls={cluster.call_count}."
            )

    contradiction_by_class = {
        key: {
            "count": value,
            "rate_per_100_steps": _ratio(value * 100, step_started_events) if step_started_events else None,
        }
        for key, value in contradiction_breakdown.items()
    }

    cache_by_mode = {
        run_row.mode: {
            "reuse_decisions": cache_reuse_decisions,
            "partial_refresh_decisions": partial_refresh_decisions,
            "full_refresh_decisions": full_refresh_decisions,
            "cache_hit_rate": kpis["cache_hit_rate"],
            "context_reuse_ratio": kpis["context_reuse_ratio"],
        }
    }

    breakdowns: dict[str, Any] = {
        "cache_by_mode": cache_by_mode,
        "refresh_by_reason": refresh_reason_breakdown,
        "contradiction_by_class": contradiction_by_class,
        "tier_resolution_by_purpose": tier_resolution_by_purpose,
        "no_progress_retry_clusters": [
            cluster.model_dump(mode="json", by_alias=True) for cluster in no_progress_clusters
        ],
        # Aliases to match docs / CLI expectations.
        "mode_scoped_cache_breakdown": cache_by_mode,
        "contradictions_by_class": contradiction_by_class,
    }

    counts = {
        "events": len(events),
        "step_started_events": step_started_events,
        "unique_steps_evaluated": unique_steps_evaluated,
        "successful_steps": successful_step_count,
        "first_pass_success_steps": len(first_pass_success_steps),
        "safely_escalated_steps": safely_escalated_step_count,
        "llm_calls": total_llm_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_decisions": total_cache_decisions,
        "cache_reuse_decisions": cache_reuse_decisions,
        "partial_refresh_decisions": partial_refresh_decisions,
        "full_refresh_decisions": full_refresh_decisions,
        "repair_validation_attempts": repair_validation_attempts,
        "repairs_promoted": repairs_promoted,
        "run_paused_events": run_paused_events,
        "run_resumed_events": run_resumed_events,
        "run_aborted_events": run_aborted_events,
        "run_completed_events": run_completed_events,
    }

    return RunBenchmarkReport(
        runId=run_id,
        mode=run_row.mode,
        status=run_row.status,
        startedAt=run_row.started_at,
        endedAt=run_row.ended_at,
        generatedAt=datetime.now(UTC).isoformat(),
        source="sqlite:runs+events+llm_calls+cache_records+learned_repairs",
        counts=counts,
        kpis=kpis,
        breakdowns=breakdowns,
        alerts=alerts,
    )


def _compute_no_progress_clusters(llm_calls: list[_LLMCallRow]) -> list[NoProgressRetryCluster]:
    clusters: list[NoProgressRetryCluster] = []
    current_key: tuple[str | None, str] | None = None
    current_calls = 0
    current_tokens = 0
    current_start: datetime | None = None
    current_end: datetime | None = None

    def flush() -> None:
        nonlocal current_key, current_calls, current_tokens, current_start, current_end
        if current_key is None or current_calls == 0 or current_start is None or current_end is None:
            current_key = None
            current_calls = 0
            current_tokens = 0
            current_start = None
            current_end = None
            return
        step_id, call_purpose = current_key
        clusters.append(
            NoProgressRetryCluster(
                stepId=step_id,
                callPurpose=call_purpose,
                callCount=current_calls,
                tokenCount=current_tokens,
                startedAt=current_start.isoformat(),
                endedAt=current_end.isoformat(),
            )
        )
        current_key = None
        current_calls = 0
        current_tokens = 0
        current_start = None
        current_end = None

    for call in llm_calls:
        if not call.no_progress_retry:
            flush()
            continue

        key = (call.step_id, call.call_purpose)
        call_tokens = call.input_tokens + call.output_tokens

        if current_key == key:
            current_calls += 1
            current_tokens += call_tokens
            current_end = call.created_at
            continue

        flush()
        current_key = key
        current_calls = 1
        current_tokens = call_tokens
        current_start = call.created_at
        current_end = call.created_at

    flush()
    return clusters


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _ratio(numerator: int | float, denominator: int | float) -> float | None:
    if denominator == 0:
        return None
    return float(numerator) / float(denominator)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / float(len(values))


def _as_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return default
        try:
            return int(stripped)
        except ValueError:
            return default
    return default

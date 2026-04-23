from __future__ import annotations

from datetime import UTC, datetime

from agent.telemetry.models import (
    CallPurpose,
    ContextTier,
    LLMCall,
    RunMode,
    RunSummary,
    TierResolutionByPurpose,
)


def build_initial_run_summary(
    *,
    run_id: str,
    mode: RunMode,
    started_at: datetime | str,
) -> RunSummary:
    started_dt = _to_datetime(started_at)
    return RunSummary(
        runId=run_id,
        mode=mode,
        startedAt=started_dt,
    )


def apply_llm_call_to_summary(
    summary: RunSummary,
    llm_call: LLMCall,
) -> RunSummary:
    payload = summary.model_dump(mode="python", by_alias=False)

    payload["total_llm_calls"] += 1
    payload["llm_assist_invocations"] += 1
    payload["context_evaluations"] += 1

    payload["input_tokens"] += llm_call.input_tokens
    payload["output_tokens"] += llm_call.output_tokens
    payload["preflight_input_tokens"] += llm_call.preflight_input_tokens
    payload["preflight_output_tokens"] += llm_call.preflight_output_tokens
    payload["cache_read"] += llm_call.cache_read
    payload["cache_write"] += llm_call.cache_write
    payload["est_cost"] += llm_call.est_cost
    payload["actual_cost"] += llm_call.actual_cost
    payload["total_latency_ms"] += llm_call.latency_ms

    if llm_call.prompt_cache_hit is True:
        payload["prompt_cache_hits"] += 1
    elif llm_call.prompt_cache_hit is False:
        payload["prompt_cache_misses"] += 1

    if llm_call.no_progress_retry:
        payload["no_progress_retry_count"] += 1
        payload["no_progress_tokens"] += llm_call.input_tokens + llm_call.output_tokens

    tier_by_purpose = {
        _normalize_call_purpose(key): value
        for key, value in payload["tier_resolution_by_purpose"].items()
    }
    purpose = _normalize_call_purpose(llm_call.call_purpose)
    tier_breakdown = tier_by_purpose.get(purpose) or TierResolutionByPurpose()

    tier_payload = tier_breakdown.model_dump(mode="python", by_alias=False)
    if _normalize_context_tier(llm_call.context_tier) is ContextTier.TIER_0:
        tier_payload["tier0_resolved"] += 1
    elif _normalize_context_tier(llm_call.context_tier) is ContextTier.TIER_1:
        tier_payload["tier1_resolved"] += 1
    else:
        tier_payload["tier2_or_higher_resolved"] += 1

    tier_by_purpose[purpose] = TierResolutionByPurpose.model_validate(tier_payload)
    payload["tier_resolution_by_purpose"] = tier_by_purpose
    return RunSummary.model_validate(payload)


def _normalize_call_purpose(value: CallPurpose | str) -> CallPurpose:
    if isinstance(value, CallPurpose):
        return value
    return CallPurpose(value)


def _normalize_context_tier(value: ContextTier | str) -> ContextTier:
    if isinstance(value, ContextTier):
        return value
    return ContextTier(value)


def _to_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed

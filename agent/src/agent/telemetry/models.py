from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from agent.core.ids import generate_event_id


class RunMode(str, Enum):
    MANUAL = "manual"
    LLM = "llm"
    HYBRID = "hybrid"


class CallPurpose(str, Enum):
    PLAN = "plan"
    REPAIR = "repair"
    CLASSIFICATION = "classification"
    REVIEW = "review"


class ContextTier(str, Enum):
    TIER_0 = "tier0"
    TIER_1 = "tier1"
    TIER_2 = "tier2"
    TIER_3 = "tier3"


class LLMCall(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)

    call_id: str = Field(default_factory=generate_event_id, alias="callId")
    run_id: str = Field(alias="runId")
    step_id: str | None = Field(default=None, alias="stepId")
    provider: str
    model: str
    call_purpose: CallPurpose = Field(alias="callPurpose")
    context_tier: ContextTier = Field(alias="contextTier")
    escalation_path: list[ContextTier] = Field(default_factory=list, alias="escalationPath")
    input_tokens: int = Field(default=0, ge=0, alias="inputTokens")
    output_tokens: int = Field(default=0, ge=0, alias="outputTokens")
    preflight_input_tokens: int = Field(default=0, ge=0, alias="preflightInputTokens")
    preflight_output_tokens: int = Field(default=0, ge=0, alias="preflightOutputTokens")
    cache_read: int = Field(default=0, ge=0, alias="cacheRead")
    cache_write: int = Field(default=0, ge=0, alias="cacheWrite")
    prompt_cache_hit: bool | None = Field(default=None, alias="promptCacheHit")
    est_cost: float = Field(default=0.0, ge=0.0, alias="estCost")
    actual_cost: float = Field(default=0.0, ge=0.0, alias="actualCost")
    latency_ms: int = Field(default=0, ge=0, alias="latencyMs")
    no_progress_retry: bool = Field(default=False, alias="noProgressRetry")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), alias="createdAt")


class ContradictionBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    stale_locator: int = Field(default=0, ge=0, alias="staleLocator")
    content_drift: int = Field(default=0, ge=0, alias="contentDrift")
    structure_drift: int = Field(default=0, ge=0, alias="structureDrift")


class TierResolutionByPurpose(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    tier0_resolved: int = Field(default=0, ge=0, alias="tier0Resolved")
    tier1_resolved: int = Field(default=0, ge=0, alias="tier1Resolved")
    tier2_or_higher_resolved: int = Field(default=0, ge=0, alias="tier2OrHigherResolved")


class RunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)

    run_id: str = Field(alias="runId")
    mode: RunMode
    started_at: datetime = Field(alias="startedAt")
    ended_at: datetime | None = Field(default=None, alias="endedAt")
    flow_completed: bool = Field(default=False, alias="flowCompleted")
    total_steps_evaluated: int = Field(default=0, ge=0, alias="totalStepsEvaluated")
    successful_steps: int = Field(default=0, ge=0, alias="successfulSteps")
    first_pass_success_steps: int = Field(default=0, ge=0, alias="firstPassSuccessSteps")
    safely_escalated_steps: int = Field(default=0, ge=0, alias="safelyEscalatedSteps")
    recovered_steps: int = Field(default=0, ge=0, alias="recoveredSteps")
    total_recovery_time_ms: int = Field(default=0, ge=0, alias="totalRecoveryTimeMs")
    restart_count: int = Field(default=0, ge=0, alias="restartCount")
    restart_opportunities: int = Field(default=0, ge=0, alias="restartOpportunities")
    llm_assist_invocations: int = Field(default=0, ge=0, alias="llmAssistInvocations")
    context_evaluations: int = Field(default=0, ge=0, alias="contextEvaluations")
    context_reused_steps: int = Field(default=0, ge=0, alias="contextReusedSteps")
    cache_reuse_decisions: int = Field(default=0, ge=0, alias="cacheReuseDecisions")
    partial_refresh_decisions: int = Field(default=0, ge=0, alias="partialRefreshDecisions")
    full_refresh_decisions: int = Field(default=0, ge=0, alias="fullRefreshDecisions")
    refresh_reason_counts: dict[str, int] = Field(default_factory=dict, alias="refreshReasonCounts")
    contradiction_count: int = Field(default=0, ge=0, alias="contradictionCount")
    contradiction_breakdown: ContradictionBreakdown = Field(
        default_factory=ContradictionBreakdown,
        alias="contradictionBreakdown",
    )
    repair_validation_attempts: int = Field(default=0, ge=0, alias="repairValidationAttempts")
    repairs_promoted: int = Field(default=0, ge=0, alias="repairsPromoted")
    total_llm_calls: int = Field(default=0, ge=0, alias="totalLlmCalls")
    input_tokens: int = Field(default=0, ge=0, alias="inputTokens")
    output_tokens: int = Field(default=0, ge=0, alias="outputTokens")
    preflight_input_tokens: int = Field(default=0, ge=0, alias="preflightInputTokens")
    preflight_output_tokens: int = Field(default=0, ge=0, alias="preflightOutputTokens")
    cache_read: int = Field(default=0, ge=0, alias="cacheRead")
    cache_write: int = Field(default=0, ge=0, alias="cacheWrite")
    prompt_cache_hits: int = Field(default=0, ge=0, alias="promptCacheHits")
    prompt_cache_misses: int = Field(default=0, ge=0, alias="promptCacheMisses")
    est_cost: float = Field(default=0.0, ge=0.0, alias="estCost")
    actual_cost: float = Field(default=0.0, ge=0.0, alias="actualCost")
    total_latency_ms: int = Field(default=0, ge=0, alias="totalLatencyMs")
    no_progress_retry_count: int = Field(default=0, ge=0, alias="noProgressRetryCount")
    no_progress_tokens: int = Field(default=0, ge=0, alias="noProgressTokens")
    tier_resolution_by_purpose: dict[CallPurpose, TierResolutionByPurpose] = Field(
        default_factory=dict,
        alias="tierResolutionByPurpose",
    )

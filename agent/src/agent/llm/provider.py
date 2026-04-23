from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from agent.core.config import LLMSettings
    from agent.storage.repos.telemetry import TelemetryRepository

from agent.llm._ported import NormalizedTransportResponse
from agent.telemetry.models import CallPurpose, ContextTier, LLMCall, RunSummary


class LLMProviderError(RuntimeError):
    pass


class LLMToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    name: str
    arguments: str


class LLMUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)


class LLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    content: str | None = None
    tool_calls: list[LLMToolCall] = Field(default_factory=list)
    finish_reason: str = "stop"
    reasoning: str | None = None
    usage: LLMUsage = Field(default_factory=LLMUsage)
    provider_data: dict[str, Any] = Field(default_factory=dict)
    llm_call: LLMCall | None = None
    run_summary: RunSummary | None = None


class LLMProvider(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        ...

    @property
    @abstractmethod
    def default_model(self) -> str:
        ...

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
        call_purpose: CallPurpose = CallPurpose.PLAN,
        context_tier: ContextTier = ContextTier.TIER_0,
        escalation_path: list[ContextTier] | None = None,
        preflight_input_tokens: int = 0,
        preflight_output_tokens: int = 0,
        est_cost: float = 0.0,
        actual_cost: float | None = None,
        no_progress_retry: bool = False,
    ) -> LLMResponse:
        ...


def to_llm_response(
    *,
    provider: str,
    model: str,
    normalized: NormalizedTransportResponse,
) -> LLMResponse:
    usage = normalized.usage
    return LLMResponse(
        provider=provider,
        model=model,
        content=normalized.content,
        tool_calls=[
            LLMToolCall(id=tool_call.id, name=tool_call.name, arguments=tool_call.arguments)
            for tool_call in (normalized.tool_calls or [])
        ],
        finish_reason=normalized.finish_reason,
        reasoning=normalized.reasoning,
        usage=LLMUsage(
            prompt_tokens=usage.prompt_tokens if usage is not None else 0,
            completion_tokens=usage.completion_tokens if usage is not None else 0,
            total_tokens=usage.total_tokens if usage is not None else 0,
            cached_tokens=usage.cached_tokens if usage is not None else 0,
            cache_write_tokens=usage.cache_write_tokens if usage is not None else 0,
        ),
        provider_data=normalized.provider_data or {},
    )


def build_provider_from_settings(
    settings: LLMSettings,
    *,
    timeout_seconds: float | None = None,
    telemetry_repository: TelemetryRepository | None = None,
) -> LLMProvider:
    provider_name = settings.provider.strip().lower().replace("-", "_")

    if provider_name == "openai":
        from agent.llm.openai import OpenAIProvider

        try:
            return OpenAIProvider(
                default_model=settings.model,
                reasoning_effort=settings.reasoning_effort,
                timeout_seconds=timeout_seconds,
                telemetry_repository=telemetry_repository,
            )
        except Exception as exc:  # pragma: no cover - depends on runtime env
            raise LLMProviderError(f"OpenAI provider setup failed: {exc}") from exc

    if provider_name in {"openai_compatible", "lm_studio", "lmstudio"}:
        from agent.llm.openai_compatible import OpenAICompatibleProvider

        try:
            return OpenAICompatibleProvider(
                default_model=settings.model,
                base_url=settings.api_base,
                reasoning_effort=settings.reasoning_effort,
                timeout_seconds=timeout_seconds,
                telemetry_repository=telemetry_repository,
            )
        except Exception as exc:  # pragma: no cover - depends on runtime env
            raise LLMProviderError(f"OpenAI-compatible provider setup failed: {exc}") from exc

    if provider_name == "anthropic":
        from agent.llm.anthropic import AnthropicProvider

        try:
            return AnthropicProvider(
                default_model=settings.model,
                timeout_seconds=timeout_seconds,
                telemetry_repository=telemetry_repository,
            )
        except Exception as exc:  # pragma: no cover - depends on runtime env
            raise LLMProviderError(f"Anthropic provider setup failed: {exc}") from exc

    raise LLMProviderError(
        "Unsupported LLM provider "
        f"'{settings.provider}'. Supported values: openai, anthropic, openai_compatible."
    )


def build_llm_call(
    *,
    run_id: str,
    step_id: str | None,
    provider: str,
    model: str,
    call_purpose: CallPurpose,
    context_tier: ContextTier,
    escalation_path: list[ContextTier] | None,
    input_tokens: int,
    output_tokens: int,
    preflight_input_tokens: int,
    preflight_output_tokens: int,
    cache_read: int,
    cache_write: int,
    est_cost: float,
    actual_cost: float | None,
    latency_ms: int,
    no_progress_retry: bool,
) -> LLMCall:
    return LLMCall(
        runId=run_id,
        stepId=step_id,
        provider=provider,
        model=model,
        callPurpose=call_purpose,
        contextTier=context_tier,
        escalationPath=escalation_path or [context_tier],
        inputTokens=input_tokens,
        outputTokens=output_tokens,
        preflightInputTokens=preflight_input_tokens,
        preflightOutputTokens=preflight_output_tokens,
        cacheRead=cache_read,
        cacheWrite=cache_write,
        promptCacheHit=(cache_read > 0),
        estCost=est_cost,
        actualCost=actual_cost if actual_cost is not None else est_cost,
        latencyMs=latency_ms,
        noProgressRetry=no_progress_retry,
        createdAt=datetime.now(UTC),
    )

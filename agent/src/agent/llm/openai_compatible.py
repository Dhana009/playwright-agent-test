from __future__ import annotations

# Ported from Hermes-Agent/agent/transports/chat_completions.py — adapted for agent/

import os
import time
from typing import TYPE_CHECKING, Any

from openai import AsyncOpenAI

from agent.llm.openai import OpenAITransport
from agent.llm.provider import LLMProvider, LLMProviderError, LLMResponse, to_llm_response
from agent.llm.provider import build_llm_call
from agent.telemetry.models import CallPurpose, ContextTier

if TYPE_CHECKING:
    from agent.storage.repos.telemetry import TelemetryRepository


class OpenAICompatibleProvider(LLMProvider):
    def __init__(
        self,
        *,
        default_model: str,
        base_url: str | None,
        timeout_seconds: float | None = None,
        api_key: str | None = None,
        telemetry_repository: TelemetryRepository | None = None,
    ) -> None:
        if not base_url:
            raise LLMProviderError(
                "OpenAI-compatible provider requires `llm.api_base` "
                "(for example LM Studio endpoint)."
            )

        resolved_api_key = api_key or os.getenv("OPENAI_API_KEY") or "lm-studio"

        self._default_model = default_model
        self._base_url = base_url
        self._client = (
            AsyncOpenAI(
                api_key=resolved_api_key,
                base_url=base_url,
                timeout=timeout_seconds,
            )
            if timeout_seconds is not None
            else AsyncOpenAI(api_key=resolved_api_key, base_url=base_url)
        )
        self._transport = OpenAITransport()
        self._telemetry_repository = telemetry_repository

    @property
    def provider_name(self) -> str:
        return "openai_compatible"

    @property
    def default_model(self) -> str:
        return self._default_model

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
        if not messages:
            raise LLMProviderError("OpenAI-compatible chat requires at least one message.")

        resolved_model = model or self._default_model
        api_kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": self._transport.convert_messages(messages),
        }
        if tools:
            api_kwargs["tools"] = self._transport.convert_tools(tools)
        if temperature is not None:
            api_kwargs["temperature"] = temperature
        if max_tokens is not None:
            api_kwargs["max_tokens"] = max_tokens
        if timeout_seconds is not None:
            api_kwargs["timeout"] = timeout_seconds
        if metadata:
            api_kwargs["metadata"] = metadata

        start = time.perf_counter()
        response = await self._client.chat.completions.create(**api_kwargs)
        latency_ms = int((time.perf_counter() - start) * 1000)

        normalized = self._transport.normalize_response(response, model=resolved_model)
        llm_response = to_llm_response(
            provider=self.provider_name,
            model=resolved_model,
            normalized=normalized,
        )
        if run_id is None:
            return llm_response

        llm_call = build_llm_call(
            run_id=run_id,
            step_id=step_id,
            provider=self.provider_name,
            model=resolved_model,
            call_purpose=call_purpose,
            context_tier=context_tier,
            escalation_path=escalation_path,
            input_tokens=llm_response.usage.prompt_tokens,
            output_tokens=llm_response.usage.completion_tokens,
            preflight_input_tokens=preflight_input_tokens,
            preflight_output_tokens=preflight_output_tokens,
            cache_read=llm_response.usage.cached_tokens,
            cache_write=llm_response.usage.cache_write_tokens,
            est_cost=est_cost,
            actual_cost=actual_cost,
            latency_ms=latency_ms,
            no_progress_retry=no_progress_retry,
        )

        run_summary = None
        if self._telemetry_repository is not None:
            run_summary = await self._telemetry_repository.record_llm_call(llm_call)

        return llm_response.model_copy(update={"llm_call": llm_call, "run_summary": run_summary})

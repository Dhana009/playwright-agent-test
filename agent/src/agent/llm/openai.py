from __future__ import annotations

# Ported from Hermes-Agent/agent/transports/chat_completions.py — adapted for agent/

import time
from typing import TYPE_CHECKING, Any

from openai import AsyncOpenAI

from agent.llm._ported import (
    NormalizedTransportResponse,
    ProviderTransport,
    TransportUsage,
    build_tool_call,
    canonicalize_tool_arguments,
)
from agent.llm.provider import LLMProvider, LLMProviderError, LLMResponse, to_llm_response
from agent.llm.provider import build_llm_call
from agent.telemetry.models import CallPurpose, ContextTier

if TYPE_CHECKING:
    from agent.storage.repos.telemetry import TelemetryRepository


class OpenAITransport(ProviderTransport):
    @property
    def provider_name(self) -> str:
        return "openai"

    def convert_messages(self, messages: list[dict[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
        return messages

    def convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return tools

    def normalize_response(
        self,
        response: Any,
        *,
        model: str,
    ) -> NormalizedTransportResponse:
        choice = response.choices[0]
        message = choice.message

        tool_calls = None
        if message.tool_calls:
            tool_calls = []
            for raw_tool_call in message.tool_calls:
                tool_calls.append(
                    build_tool_call(
                        id=raw_tool_call.id,
                        name=raw_tool_call.function.name,
                        arguments=canonicalize_tool_arguments(raw_tool_call.function.arguments),
                    )
                )

        usage = None
        if getattr(response, "usage", None):
            usage_data = response.usage
            prompt_details = getattr(usage_data, "prompt_tokens_details", None)
            usage = TransportUsage(
                prompt_tokens=getattr(usage_data, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage_data, "completion_tokens", 0) or 0,
                total_tokens=getattr(usage_data, "total_tokens", 0) or 0,
                cached_tokens=getattr(prompt_details, "cached_tokens", 0) or 0,
                cache_write_tokens=getattr(prompt_details, "cache_write_tokens", 0) or 0,
            )

        provider_data: dict[str, Any] = {}
        if getattr(message, "reasoning_content", None):
            provider_data["reasoning_content"] = message.reasoning_content
        if getattr(message, "reasoning", None):
            provider_data["reasoning"] = message.reasoning

        return NormalizedTransportResponse(
            content=_coerce_content_to_text(message.content),
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            reasoning=getattr(message, "reasoning", None),
            usage=usage,
            provider_data=provider_data or None,
        )


class OpenAIProvider(LLMProvider):
    def __init__(
        self,
        *,
        default_model: str,
        timeout_seconds: float | None = None,
        telemetry_repository: TelemetryRepository | None = None,
    ) -> None:
        self._default_model = default_model
        self._timeout_seconds = timeout_seconds
        self._client = AsyncOpenAI(timeout=timeout_seconds) if timeout_seconds else AsyncOpenAI()
        self._transport = OpenAITransport()
        self._telemetry_repository = telemetry_repository

    @property
    def provider_name(self) -> str:
        return "openai"

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
            raise LLMProviderError("OpenAI chat requires at least one message.")

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


def _coerce_content_to_text(content: Any) -> str | None:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif hasattr(block, "type") and getattr(block, "type") == "text":
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts) if parts else None
    return str(content)

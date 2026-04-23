from __future__ import annotations

# Ported from Hermes-Agent/agent/transports/anthropic.py — adapted for agent/

import json
import time
from typing import TYPE_CHECKING, Any

from anthropic import AsyncAnthropic

from agent.llm._ported import (
    NormalizedTransportResponse,
    ProviderTransport,
    TransportUsage,
    build_tool_call,
    canonicalize_tool_arguments,
    map_finish_reason,
)
from agent.llm.provider import LLMProvider, LLMProviderError, LLMResponse, to_llm_response
from agent.llm.provider import build_llm_call
from agent.telemetry.models import CallPurpose, ContextTier

if TYPE_CHECKING:
    from agent.storage.repos.telemetry import TelemetryRepository

_ANTHROPIC_FINISH_REASON_MAP = {
    "end_turn": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "refusal": "content_filter",
}


class AnthropicTransport(ProviderTransport):
    @property
    def provider_name(self) -> str:
        return "anthropic"

    def convert_messages(self, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        system_parts: list[str] = []
        converted_messages: list[dict[str, Any]] = []

        for message in messages:
            role = message.get("role")
            if role == "system":
                text = _coerce_content_to_text(message.get("content"))
                if text:
                    system_parts.append(text)
                continue

            if role == "user":
                converted_messages.append(
                    {
                        "role": "user",
                        "content": _to_anthropic_content_blocks(message.get("content")),
                    }
                )
                continue

            if role == "assistant":
                assistant_blocks: list[dict[str, Any]] = []
                text_content = _coerce_content_to_text(message.get("content"))
                if text_content:
                    assistant_blocks.append({"type": "text", "text": text_content})

                for tool_call in message.get("tool_calls", []):
                    function_block = tool_call.get("function", {})
                    tool_name = function_block.get("name")
                    if not tool_name:
                        continue
                    raw_arguments = function_block.get("arguments", {})
                    try:
                        parsed_arguments = (
                            json.loads(raw_arguments)
                            if isinstance(raw_arguments, str)
                            else raw_arguments
                        )
                    except json.JSONDecodeError:
                        parsed_arguments = {}
                    assistant_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tool_call.get("id") or "tool_call_missing_id",
                            "name": tool_name,
                            "input": parsed_arguments if isinstance(parsed_arguments, dict) else {},
                        }
                    )

                if assistant_blocks:
                    converted_messages.append(
                        {
                            "role": "assistant",
                            "content": assistant_blocks,
                        }
                    )
                continue

            if role == "tool":
                tool_call_id = message.get("tool_call_id")
                if not isinstance(tool_call_id, str) or not tool_call_id:
                    continue
                converted_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_call_id,
                                "content": _coerce_content_to_text(message.get("content")) or "",
                            }
                        ],
                    }
                )

        return {
            "system": "\n\n".join(system_parts) if system_parts else None,
            "messages": converted_messages,
        }

    def convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for tool in tools:
            if tool.get("type") != "function":
                continue
            function_schema = tool.get("function", {})
            tool_name = function_schema.get("name")
            if not tool_name:
                continue
            input_schema = function_schema.get("parameters") or {
                "type": "object",
                "properties": {},
            }
            converted.append(
                {
                    "name": tool_name,
                    "description": function_schema.get("description") or "",
                    "input_schema": input_schema,
                }
            )
        return converted

    def normalize_response(
        self,
        response: Any,
        *,
        model: str,
    ) -> NormalizedTransportResponse:
        content_parts: list[str] = []
        tool_calls: list[Any] = []
        for block in getattr(response, "content", []) or []:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text = getattr(block, "text", None)
                if isinstance(text, str) and text:
                    content_parts.append(text)
            elif block_type == "tool_use":
                tool_calls.append(
                    build_tool_call(
                        id=getattr(block, "id", None),
                        name=getattr(block, "name", "unknown_tool"),
                        arguments=canonicalize_tool_arguments(getattr(block, "input", {})),
                    )
                )

        usage_obj = getattr(response, "usage", None)
        usage = None
        if usage_obj is not None:
            prompt_tokens = getattr(usage_obj, "input_tokens", 0) or 0
            completion_tokens = getattr(usage_obj, "output_tokens", 0) or 0
            usage = TransportUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                cached_tokens=getattr(usage_obj, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(usage_obj, "cache_creation_input_tokens", 0) or 0,
            )

        finish_reason = map_finish_reason(
            getattr(response, "stop_reason", None),
            _ANTHROPIC_FINISH_REASON_MAP,
        )

        return NormalizedTransportResponse(
            content="\n".join(content_parts) if content_parts else None,
            tool_calls=tool_calls or None,
            finish_reason=finish_reason,
            usage=usage,
            provider_data={
                "stop_sequence": getattr(response, "stop_sequence", None),
            },
        )


class AnthropicProvider(LLMProvider):
    def __init__(
        self,
        *,
        default_model: str,
        timeout_seconds: float | None = None,
        telemetry_repository: TelemetryRepository | None = None,
    ) -> None:
        self._default_model = default_model
        self._timeout_seconds = timeout_seconds
        self._client = (
            AsyncAnthropic(timeout=timeout_seconds)
            if timeout_seconds is not None
            else AsyncAnthropic()
        )
        self._transport = AnthropicTransport()
        self._telemetry_repository = telemetry_repository

    @property
    def provider_name(self) -> str:
        return "anthropic"

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
            raise LLMProviderError("Anthropic chat requires at least one message.")

        resolved_model = model or self._default_model
        converted_payload = self._transport.convert_messages(messages)

        api_kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": converted_payload["messages"],
            "max_tokens": max_tokens if max_tokens is not None else 4096,
        }
        if converted_payload["system"] is not None:
            api_kwargs["system"] = converted_payload["system"]
        if tools:
            api_kwargs["tools"] = self._transport.convert_tools(tools)
        if temperature is not None:
            api_kwargs["temperature"] = temperature
        if timeout_seconds is not None:
            api_kwargs["timeout"] = timeout_seconds
        if metadata:
            api_kwargs["metadata"] = metadata

        start = time.perf_counter()
        response = await self._client.messages.create(**api_kwargs)
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


def _to_anthropic_content_blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return [{"type": "text", "text": ""}]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        blocks: list[dict[str, Any]] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                blocks.append({"type": "text", "text": item.get("text", "")})
            elif hasattr(item, "type") and getattr(item, "type") == "text":
                blocks.append({"type": "text", "text": getattr(item, "text", "")})
        return blocks or [{"type": "text", "text": ""}]
    return [{"type": "text", "text": str(content)}]

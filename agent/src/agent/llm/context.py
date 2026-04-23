from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import tiktoken
from pydantic import BaseModel, ConfigDict, Field

from agent.telemetry.models import ContextTier

_TIER_ORDER = {
    ContextTier.TIER_0: 0,
    ContextTier.TIER_1: 1,
    ContextTier.TIER_2: 2,
    ContextTier.TIER_3: 3,
}
_ORDER_TO_TIER = {value: key for key, value in _TIER_ORDER.items()}


class TokenPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    model: str
    input_tokens: int = Field(alias="inputTokens", ge=0)
    output_tokens: int = Field(alias="outputTokens", ge=0)
    total_tokens: int = Field(alias="totalTokens", ge=0)


class ContextBuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)

    tier: ContextTier
    messages: list[dict[str, Any]]
    included_sections: list[str] = Field(alias="includedSections", default_factory=list)
    preflight: TokenPreflight

    @property
    def preflight_input_tokens(self) -> int:
        return self.preflight.input_tokens

    @property
    def preflight_output_tokens(self) -> int:
        return self.preflight.output_tokens


@dataclass(slots=True)
class TokenPreflightEstimator:
    model: str
    default_output_tokens: int = 512

    def estimate_messages(self, messages: list[dict[str, Any]]) -> int:
        encoding = self._encoding_for_model(self.model)
        # Coarse but stable estimate across providers.
        total = 0
        for message in messages:
            total += 4
            total += len(encoding.encode(str(message.get("role", ""))))
            total += self._estimate_content_tokens(encoding, message.get("content"))
            name_value = message.get("name")
            if isinstance(name_value, str):
                total += len(encoding.encode(name_value))
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                total += len(encoding.encode(json.dumps(tool_calls, ensure_ascii=True)))
        return total + 2

    def preflight(
        self,
        messages: list[dict[str, Any]],
        *,
        output_tokens: int | None = None,
    ) -> TokenPreflight:
        input_tokens = self.estimate_messages(messages)
        output_budget = output_tokens if output_tokens is not None else self.default_output_tokens
        if output_budget < 0:
            output_budget = 0
        return TokenPreflight(
            model=self.model,
            inputTokens=input_tokens,
            outputTokens=output_budget,
            totalTokens=input_tokens + output_budget,
        )

    def _estimate_content_tokens(
        self,
        encoding: tiktoken.Encoding,
        content: Any,
    ) -> int:
        if content is None:
            return 0
        if isinstance(content, str):
            return len(encoding.encode(content))
        if isinstance(content, list):
            serialized = json.dumps(content, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
            return len(encoding.encode(serialized))
        if isinstance(content, dict):
            serialized = json.dumps(content, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
            return len(encoding.encode(serialized))
        return len(encoding.encode(str(content)))

    def _encoding_for_model(self, model: str) -> tiktoken.Encoding:
        try:
            return tiktoken.encoding_for_model(model)
        except KeyError:
            return tiktoken.get_encoding("cl100k_base")


class StagedContextBuilder:
    def __init__(
        self,
        *,
        model: str,
        default_output_tokens: int = 512,
    ) -> None:
        self._estimator = TokenPreflightEstimator(
            model=model,
            default_output_tokens=default_output_tokens,
        )

    def build_for_tier(
        self,
        *,
        tier: ContextTier,
        step: Any,
        outcome: Any | None = None,
        scoped_target: Any | None = None,
        history: list[Any] | None = None,
        contradictions: list[Any] | None = None,
        full_snapshot: Any | None = None,
        system_prompt: str | None = None,
        task_prompt: str | None = None,
        output_tokens: int | None = None,
    ) -> ContextBuildResult:
        payload: dict[str, Any] = {}
        included_sections: list[str] = []

        payload["step"] = _normalize_for_json(step)
        included_sections.append("step")

        if outcome is not None:
            payload["outcome"] = _normalize_for_json(outcome)
            included_sections.append("outcome")

        if self._includes(tier, ContextTier.TIER_1):
            payload["scopedTarget"] = _normalize_for_json(scoped_target)
            included_sections.append("scopedTarget")

        if self._includes(tier, ContextTier.TIER_2):
            payload["history"] = _normalize_for_json(history or [])
            payload["contradictions"] = _normalize_for_json(contradictions or [])
            included_sections.extend(["history", "contradictions"])

        if self._includes(tier, ContextTier.TIER_3):
            payload["fullSnapshot"] = _normalize_for_json(full_snapshot)
            included_sections.append("fullSnapshot")

        tier_payload = {
            "contextTier": tier.value,
            "context": payload,
        }
        messages = _build_messages(
            system_prompt=system_prompt,
            task_prompt=task_prompt,
            payload=tier_payload,
        )
        preflight = self._estimator.preflight(messages, output_tokens=output_tokens)
        return ContextBuildResult(
            tier=tier,
            messages=messages,
            includedSections=included_sections,
            preflight=preflight,
        )

    def build_escalation_sequence(
        self,
        *,
        target_tier: ContextTier,
        step: Any,
        outcome: Any | None = None,
        scoped_target: Any | None = None,
        history: list[Any] | None = None,
        contradictions: list[Any] | None = None,
        full_snapshot: Any | None = None,
        system_prompt: str | None = None,
        task_prompt: str | None = None,
        output_tokens: int | None = None,
    ) -> list[ContextBuildResult]:
        target_index = _TIER_ORDER[target_tier]
        sequence: list[ContextBuildResult] = []
        for tier_index in range(target_index + 1):
            tier = _ORDER_TO_TIER[tier_index]
            sequence.append(
                self.build_for_tier(
                    tier=tier,
                    step=step,
                    outcome=outcome,
                    scoped_target=scoped_target,
                    history=history,
                    contradictions=contradictions,
                    full_snapshot=full_snapshot,
                    system_prompt=system_prompt,
                    task_prompt=task_prompt,
                    output_tokens=output_tokens,
                )
            )
        return sequence

    def _includes(self, requested_tier: ContextTier, minimum_tier: ContextTier) -> bool:
        return _TIER_ORDER[requested_tier] >= _TIER_ORDER[minimum_tier]


def _build_messages(
    *,
    system_prompt: str | None,
    task_prompt: str | None,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    payload_json = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    user_content = payload_json
    if task_prompt:
        user_content = f"{task_prompt}\n\nContext JSON:\n{payload_json}"
    messages.append({"role": "user", "content": user_content})
    return messages


def _normalize_for_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, dict):
        return {str(key): _normalize_for_json(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_normalize_for_json(inner) for inner in value]
    if isinstance(value, tuple):
        return [_normalize_for_json(inner) for inner in value]
    if hasattr(value, "model_dump") and callable(getattr(value, "model_dump")):
        return value.model_dump(mode="json", by_alias=True)
    if hasattr(value, "__dict__"):
        try:
            return {
                str(key): _normalize_for_json(inner)
                for key, inner in vars(value).items()
                if not str(key).startswith("_")
            }
        except TypeError:
            return str(value)
    return value

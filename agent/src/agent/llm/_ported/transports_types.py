from __future__ import annotations

# Ported from Hermes-Agent/agent/transports/types.py — adapted for agent/

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TransportToolCall:
    id: str | None
    name: str
    arguments: str
    provider_data: dict[str, Any] | None = field(default=None, repr=False)


@dataclass(slots=True)
class TransportUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(slots=True)
class NormalizedTransportResponse:
    content: str | None
    tool_calls: list[TransportToolCall] | None
    finish_reason: str
    reasoning: str | None = None
    usage: TransportUsage | None = None
    provider_data: dict[str, Any] | None = field(default=None, repr=False)


def build_tool_call(
    *,
    id: str | None,
    name: str,
    arguments: Any,
    provider_data: dict[str, Any] | None = None,
) -> TransportToolCall:
    if isinstance(arguments, str):
        argument_string = arguments
    elif isinstance(arguments, dict):
        argument_string = json.dumps(arguments)
    else:
        argument_string = json.dumps(arguments)

    return TransportToolCall(
        id=id,
        name=name,
        arguments=argument_string,
        provider_data=provider_data,
    )


def map_finish_reason(raw_reason: str | None, mapping: dict[str, str]) -> str:
    if raw_reason is None:
        return "stop"
    return mapping.get(raw_reason, "stop")

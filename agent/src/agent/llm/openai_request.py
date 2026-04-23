from __future__ import annotations

from typing import Any

# GPT-5 and o-series models use `max_completion_tokens` in Chat Completions.
_MAX_COMPLETION_TOKENS_PREFIXES: tuple[str, ...] = ("gpt-5", "o1", "o3", "o4")

# `reasoning_effort` is only applicable to reasoning-capable model families.
_REASONING_EFFORT_PREFIXES: tuple[str, ...] = _MAX_COMPLETION_TOKENS_PREFIXES


def apply_openai_generation_controls(
    api_kwargs: dict[str, Any],
    *,
    model: str,
    max_tokens: int | None,
    reasoning_effort: str | None,
) -> None:
    normalized_model = model.strip().lower()

    if reasoning_effort is not None and _supports_reasoning_effort(normalized_model):
        api_kwargs["reasoning_effort"] = reasoning_effort

    if max_tokens is None:
        return

    token_limit_key = (
        "max_completion_tokens"
        if _uses_max_completion_tokens(normalized_model)
        else "max_tokens"
    )
    api_kwargs[token_limit_key] = max_tokens


def _uses_max_completion_tokens(model: str) -> bool:
    return model.startswith(_MAX_COMPLETION_TOKENS_PREFIXES)


def _supports_reasoning_effort(model: str) -> bool:
    return model.startswith(_REASONING_EFFORT_PREFIXES)

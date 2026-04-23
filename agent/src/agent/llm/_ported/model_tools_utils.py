from __future__ import annotations

# Ported from Hermes-Agent/model_tools.py — adapted for agent/

import json
from typing import Any


def canonicalize_tool_arguments(raw_arguments: Any) -> str:
    if isinstance(raw_arguments, str):
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            return raw_arguments
        normalized = _coerce_structure(parsed)
        return json.dumps(normalized, separators=(",", ":"))

    normalized = _coerce_structure(raw_arguments)
    return json.dumps(normalized, separators=(",", ":"))


def _coerce_structure(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _coerce_structure(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_coerce_structure(inner) for inner in value]
    if isinstance(value, str):
        return _coerce_scalar(value)
    return value


def _coerce_scalar(value: str) -> Any:
    boolean = _coerce_boolean(value)
    if boolean is not value:
        return boolean

    number = _coerce_number(value)
    if number is not value:
        return number

    return value


def _coerce_number(value: str) -> Any:
    try:
        parsed = float(value)
    except (ValueError, OverflowError):
        return value

    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return value
    if parsed == int(parsed):
        return int(parsed)
    return parsed


def _coerce_boolean(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value

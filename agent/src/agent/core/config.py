from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, Mapping, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "openai"
    model: str = "gpt-5.4-mini"
    api_base: str | None = None
    reasoning_effort: Literal["none", "low", "medium", "high", "xhigh"] | None = None


class CacheSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    decision_ttl_seconds: int = Field(default=300, ge=0)


class PolicySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_allow_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    review_threshold: float = Field(default=0.70, ge=0.0, le=1.0)
    domain_allowlist: list[str] = Field(default_factory=list)
    domain_denylist: list[str] = Field(default_factory=list)
    upload_root_allowlist: list[str] = Field(default_factory=list)
    allow_file_urls: bool = False


class StorageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sqlite_path: str = "runs/agent.sqlite"


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["manual", "llm", "hybrid"] = "manual"
    llm: LLMSettings = Field(default_factory=LLMSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    policy: PolicySettings = Field(default_factory=PolicySettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)

    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
        overrides: Mapping[str, Any] | None = None,
        env_prefix: str = "AGENT_",
    ) -> "Settings":
        config_file = Path(config_path) if config_path else _default_config_path()
        base_config = _read_yaml_config(config_file)
        env_config = _read_env_config(env_prefix)

        merged = _deep_merge(base_config, env_config)
        if overrides:
            merged = _deep_merge(merged, overrides)

        try:
            return cls.model_validate(merged)
        except ValidationError as exc:
            raise ValueError(
                f"Invalid configuration values in '{config_file}': {exc}"
            ) from exc


def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "default.yaml"


def _read_yaml_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in config file '{path}': {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"Config file '{path}' must contain a top-level mapping")

    return cast(dict[str, Any], payload)


def _read_env_config(prefix: str) -> dict[str, Any]:
    env_overrides: dict[str, Any] = {}

    for key, raw_value in os.environ.items():
        if not key.startswith(prefix):
            continue

        trimmed_key = key[len(prefix) :]
        parts = [part.lower() for part in trimmed_key.split("__") if part]
        if not parts:
            continue

        _set_nested_value(env_overrides, parts, _coerce_env_value(raw_value))

    return env_overrides


def _set_nested_value(target: dict[str, Any], path: list[str], value: Any) -> None:
    cursor = target
    for key in path[:-1]:
        if key not in cursor or not isinstance(cursor[key], dict):
            cursor[key] = {}
        cursor = cast(dict[str, Any], cursor[key])
    cursor[path[-1]] = value


def _coerce_env_value(raw_value: str) -> Any:
    lowered = raw_value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none"}:
        return None

    try:
        return int(raw_value)
    except ValueError:
        pass

    try:
        return float(raw_value)
    except ValueError:
        pass

    return raw_value


def _deep_merge(base: Mapping[str, Any], updates: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in updates.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(
                cast(Mapping[str, Any], existing), cast(Mapping[str, Any], value)
            )
        else:
            merged[key] = value
    return merged

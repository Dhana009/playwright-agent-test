from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

_ENV_KEYS = ("FLOWHUB_URL", "FLOWHUB_EMAIL", "FLOWHUB_PASSWORD")


@dataclass(frozen=True)
class TargetInfo:
    url: str
    email: str
    password: str

    def __repr__(self) -> str:
        return f"TargetInfo(url={self.url!r}, email={self.email!r}, password='***')"


def live_target(env_path: str | Path | None = None) -> TargetInfo:
    env_file = Path(env_path) if env_path is not None else _default_env_path()
    payload = _load_env_payload(env_file)
    missing = [key for key in _ENV_KEYS if not payload.get(key, "").strip()]
    if missing:
        missing_keys = ", ".join(missing)
        raise RuntimeError(
            f"Missing required key(s) in '{env_file}': {missing_keys}. "
            "Populate agent/.env.test before running live-site tests."
        )

    return TargetInfo(
        url=payload["FLOWHUB_URL"].strip(),
        email=payload["FLOWHUB_EMAIL"].strip(),
        password=payload["FLOWHUB_PASSWORD"].strip(),
    )


def _default_env_path() -> Path:
    return Path(__file__).resolve().parents[2] / ".env.test"


def _load_env_payload(env_file: Path) -> dict[str, str]:
    if not env_file.exists():
        raise RuntimeError(
            f"Missing env file '{env_file}'. "
            "Copy agent/.env.test.example to agent/.env.test and fill values."
        )

    payload = dotenv_values(env_file)
    normalized: dict[str, str] = {}
    for key in _ENV_KEYS:
        raw_value = payload.get(key)
        normalized[key] = "" if raw_value is None else str(raw_value)
    return normalized

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.core.config import Settings
from agent.core.ids import generate_human_session_id, generate_event_id

AnswerKind = Literal["y", "n", "skip", "free_text"]


def _agent_project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def default_human_sessions_root() -> Path:
    return _agent_project_root() / "artifacts" / "human-sessions"


class CheckpointAnswer(BaseModel):
    """One line in answers.jsonl (Phase B checkpoints)."""

    model_config = ConfigDict(extra="forbid")

    checkpoint_id: str
    question: str
    answer: str = Field(
        ...,
        description="y, n, skip, or free-text body when not y/n/skip.",
    )
    answer_kind: AnswerKind
    free_text_note: str | None = None
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))


class HumanSession:
    """Creates artifact layout and records environment + checkpoint answers."""

    def __init__(
        self,
        session_id: str,
        *,
        sessions_root: Path | None = None,
    ) -> None:
        if not session_id.strip():
            raise ValueError("session_id must be non-empty")
        self.session_id = session_id
        self._root = (sessions_root or default_human_sessions_root()).resolve()
        self._session_dir = self._root / session_id

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    @property
    def events_path(self) -> Path:
        return self._session_dir / "events.jsonl"

    @property
    def answers_path(self) -> Path:
        return self._session_dir / "answers.jsonl"

    @property
    def environment_snapshot_path(self) -> Path:
        return self._session_dir / "environment_snapshot.json"

    def ensure_layout(self) -> None:
        self._session_dir.mkdir(parents=True, exist_ok=True)
        (self._session_dir / "screenshots").mkdir(exist_ok=True)
        (self._session_dir / "traces").mkdir(exist_ok=True)

    def start(
        self,
        *,
        target_url: str,
        config_path: Path | None = None,
        env_file: Path | None = None,
    ) -> dict[str, Any]:
        self.ensure_layout()
        settings = Settings.load(config_path)
        snapshot = _build_environment_snapshot(
            target_url=target_url,
            settings=settings,
            config_path=config_path,
            env_file=env_file,
        )
        payload = json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=True)
        self.environment_snapshot_path.write_text(payload + "\n", encoding="utf-8")
        event = {
            "type": "session_started",
            "event_id": generate_event_id(),
            "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "session_id": self.session_id,
            "session_dir": str(self._session_dir),
            "target_url": target_url,
            "environment_snapshot_path": str(self.environment_snapshot_path),
        }
        _append_jsonl(self.events_path, event)
        return snapshot

    def append_checkpoint_answer(self, entry: CheckpointAnswer) -> None:
        self.ensure_layout()
        line = json.dumps(entry.model_dump(mode="json"), ensure_ascii=True)
        with self.answers_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")
        _append_jsonl(
            self.events_path,
            {
                "type": "checkpoint_answered",
                "event_id": generate_event_id(),
                "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "session_id": self.session_id,
                "checkpoint_id": entry.checkpoint_id,
            },
        )


def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")


def parse_answer_kind(raw: str) -> tuple[AnswerKind, str]:
    stripped = raw.strip()
    lower = stripped.lower()
    if lower in {"y", "yes"}:
        return "y", stripped
    if lower in {"n", "no"}:
        return "n", stripped
    if lower == "skip":
        return "skip", stripped
    return "free_text", stripped


def _git_head(agent_root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(agent_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if proc.returncode != 0:
            return None
        sha = proc.stdout.strip()
        return sha or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _agent_package_version() -> str:
    try:
        from importlib.metadata import version

        return version("playwright-agent")
    except Exception:
        return "unknown"


def _playwright_package_version() -> str:
    try:
        from importlib.metadata import version

        return version("playwright")
    except Exception:
        return "unknown"


def _chromium_version() -> str:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return p.chromium.version
    except Exception:
        return "unavailable"


def os_environ_keys_suspicious() -> set[str]:
    import os

    keys: set[str] = set()
    for key in os.environ:
        upper = key.upper()
        if any(
            token in upper
            for token in (
                "PASSWORD",
                "SECRET",
                "TOKEN",
                "API_KEY",
                "APIKEY",
                "OPENAI",
                "ANTHROPIC",
                "BEARER",
            )
        ):
            keys.add(key)
    return keys


def _build_environment_snapshot(
    *,
    target_url: str,
    settings: Settings,
    config_path: Path | None,
    env_file: Path | None,
) -> dict[str, Any]:
    agent_root = _agent_project_root()
    default_cfg = agent_root / "config" / "default.yaml"
    resolved_config = str(Path(config_path).resolve()) if config_path else str(default_cfg.resolve())
    return {
        "ts": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "git_sha": _git_head(agent_root),
        "agent_version": _agent_package_version(),
        "python_version": sys.version.split()[0],
        "playwright_package_version": _playwright_package_version(),
        "chromium_version": _chromium_version(),
        "config_path": resolved_config,
        "settings": settings.model_dump(mode="json"),
        "target_url": target_url,
        "env_file": str(env_file.resolve()) if env_file else None,
        "sensitive_env_keys_present": sorted(os_environ_keys_suspicious()),
    }


def new_session_id(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    return generate_human_session_id()

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

ErrorClass = Literal["syntax", "config", "runtime", "logical", "design", "flaky"]
Outcome = Literal["fixed", "open", "escalated", "flaky", "deferred", "escalated-unresolved"]


class BugEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"bug_{uuid4().hex[:12]}")
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    phase: str
    task: str
    feature: str
    error_class: ErrorClass
    summary: str
    hypothesis: str
    change: str
    outcome: Outcome
    user_decision: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)


class BugLogWriter:
    def __init__(self, run_id: str, artifacts_root: str | Path | None = None) -> None:
        if not run_id.strip():
            raise ValueError("run_id must be non-empty")
        self._run_id = run_id
        self._artifacts_root = Path(artifacts_root) if artifacts_root else _default_artifacts_root()
        self._log_path = self._artifacts_root / run_id / "bugs.jsonl"

    @property
    def path(self) -> Path:
        return self._log_path

    def append(self, entry: BugEntry) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(entry.model_dump(mode="json"), ensure_ascii=True)
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")

    def append_new(
        self,
        *,
        phase: str,
        task: str,
        feature: str,
        error_class: ErrorClass,
        summary: str,
        hypothesis: str,
        change: str,
        outcome: Outcome,
        user_decision: str | None = None,
        artifact_refs: list[str] | None = None,
    ) -> BugEntry:
        entry = BugEntry(
            phase=phase,
            task=task,
            feature=feature,
            error_class=error_class,
            summary=summary,
            hypothesis=hypothesis,
            change=change,
            outcome=outcome,
            user_decision=user_decision,
            artifact_refs=artifact_refs or [],
        )
        self.append(entry)
        return entry


def _default_artifacts_root() -> Path:
    return Path(__file__).resolve().parents[3] / "artifacts" / "test-runs"

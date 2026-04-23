from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent.core.ids import generate_audit_id
from agent.core.logging import get_logger
from agent.policy.approval import ApprovalDecision
from agent.stepgraph.models import Step
from agent.storage.files import get_run_layout
from agent.storage.repos._common import dumps_json


class AuditKind(str, Enum):
    APPROVAL = "approval"
    MODE_SWITCH = "mode_switch"
    TOOL_CALL = "tool_call"
    INTERVENTION = "intervention"
    RETRY = "retry"


class AuditEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    audit_id: str = Field(default_factory=generate_audit_id, alias="auditId")
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    run_id: str = Field(alias="runId")
    step_id: str | None = Field(default=None, alias="stepId")
    actor: str
    kind: AuditKind
    decision_path: list[str] = Field(default_factory=list, alias="decisionPath")
    payload: dict[str, Any] = Field(default_factory=dict)


class AuditLogger:
    def __init__(self, *, run_id: str, output_path: Path) -> None:
        self._logger = get_logger(__name__)
        self._run_id = run_id
        self._output_path = output_path

    @classmethod
    def for_run(cls, *, run_id: str, runs_root: str | Path | None = None) -> "AuditLogger":
        layout = get_run_layout(run_id=run_id, runs_root=runs_root)
        return cls(run_id=run_id, output_path=layout.run_dir / "audit.jsonl")

    @property
    def output_path(self) -> Path:
        return self._output_path

    def record(
        self,
        *,
        kind: AuditKind,
        actor: str,
        step_id: str | None = None,
        decision_path: list[str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditEntry:
        entry = AuditEntry(
            runId=self._run_id,
            stepId=step_id,
            actor=actor,
            kind=kind,
            decisionPath=decision_path or [],
            payload=payload or {},
        )
        self._append(entry)
        return entry

    def record_approval(
        self,
        *,
        step: Step,
        decision: ApprovalDecision,
        approved: bool,
        actor: str,
        attempt_index: int,
    ) -> AuditEntry:
        return self.record(
            kind=AuditKind.APPROVAL,
            actor=actor,
            step_id=step.step_id,
            decision_path=decision.decision_path,
            payload={
                "action": step.action,
                "level": decision.level.value,
                "summary": decision.summary,
                "reasonCodes": list(decision.reason_codes),
                "matchedSignals": list(decision.matched_signals),
                "approved": approved,
                "attemptIndex": attempt_index,
            },
        )

    def record_mode_switch(
        self,
        *,
        actor: str,
        previous_mode: str,
        new_mode: str,
        reason: str,
        step_id: str | None = None,
    ) -> AuditEntry:
        return self.record(
            kind=AuditKind.MODE_SWITCH,
            actor=actor,
            step_id=step_id,
            decision_path=["mode_controller", "mode_switch"],
            payload={
                "previousMode": previous_mode,
                "newMode": new_mode,
                "reason": reason,
            },
        )

    def record_tool_call(self, tool_event: Any) -> AuditEntry:
        payload = _model_payload(tool_event)
        return self.record(
            kind=AuditKind.TOOL_CALL,
            actor=str(payload.get("actor", "tool_layer")),
            step_id=payload.get("stepId") if isinstance(payload.get("stepId"), str) else None,
            decision_path=["tool_runtime", "tool_call"],
            payload=payload,
        )

    def record_retry(self, event: Any) -> AuditEntry:
        payload = _model_payload(event)
        return self.record(
            kind=AuditKind.RETRY,
            actor=str(payload.get("actor", "runner")),
            step_id=payload.get("step_id") if isinstance(payload.get("step_id"), str) else None,
            decision_path=["runner", "step_retry"],
            payload=payload,
        )

    def record_intervention(self, event: Any) -> AuditEntry:
        payload = _model_payload(event)
        return self.record(
            kind=AuditKind.INTERVENTION,
            actor=str(payload.get("actor", "operator")),
            step_id=payload.get("step_id") if isinstance(payload.get("step_id"), str) else None,
            decision_path=["fix_cmd", "intervention_recorded"],
            payload=payload,
        )

    def _append(self, entry: AuditEntry) -> None:
        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        line = dumps_json(entry.model_dump(mode="json", by_alias=True)) + "\n"
        with self._output_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        self._logger.debug("audit_entry_written", run_id=self._run_id, path=str(self._output_path))


def _model_payload(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json", by_alias=True)
        if isinstance(dumped, dict):
            return dumped
    if isinstance(value, dict):
        return dict(value)
    return {"value": str(value)}

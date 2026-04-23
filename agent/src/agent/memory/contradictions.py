from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent.core.ids import generate_conflict_id
from agent.core.logging import get_logger
from agent.storage.files import resolve_runs_root
from agent.storage.repos._common import dumps_json


class ContradictionType(str, Enum):
    STALE_LOCATOR = "stale_locator"
    CONTENT_DRIFT = "content_drift"
    STRUCTURE_DRIFT = "structure_drift"


class ContradictionPolicyOutcome(str, Enum):
    ACCEPT_NEW = "accept_new"
    KEEP_OLD = "keep_old"
    DUAL_TRACK_WITH_FALLBACK = "dual_track_with_fallback"
    REQUIRE_MANUAL_REVIEW = "require_manual_review"


class ContradictionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)

    conflict_id: str = Field(alias="conflictId")
    run_id: str = Field(alias="runId")
    step_id: str | None = Field(default=None, alias="stepId")
    contradiction_type: ContradictionType = Field(alias="contradictionType")
    decision: ContradictionPolicyOutcome
    decision_rationale: str = Field(alias="decisionRationale")
    old_selector: str | None = Field(default=None, alias="oldSelector")
    new_selector: str | None = Field(default=None, alias="newSelector")
    old_confidence: float | None = Field(default=None, ge=0.0, le=1.0, alias="oldConfidence")
    new_confidence: float | None = Field(default=None, ge=0.0, le=1.0, alias="newConfidence")
    rollback_ref: str = Field(alias="rollbackRef")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), alias="createdAt")
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContradictionResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)

    record: ContradictionRecord
    resolved_value: dict[str, Any] | None = Field(default=None, alias="resolvedValue")


@dataclass(frozen=True)
class ContradictionPolicyConfig:
    confidence_epsilon: float = 0.0
    require_manual_when_unvalidated: bool = True


@dataclass(frozen=True)
class ContradictionPersistence:
    runs_root: Path


class ContradictionResolver:
    """
    Classify contradictions, apply deterministic policy, and persist conflict records.
    """

    def __init__(
        self,
        persistence: ContradictionPersistence,
        *,
        policy_config: ContradictionPolicyConfig | None = None,
    ) -> None:
        self._logger = get_logger(__name__)
        self._p = persistence
        self._policy = policy_config or ContradictionPolicyConfig()

    @classmethod
    def create(
        cls,
        *,
        runs_root: str | Path | None = None,
        policy_config: ContradictionPolicyConfig | None = None,
    ) -> "ContradictionResolver":
        return cls(
            persistence=ContradictionPersistence(runs_root=resolve_runs_root(runs_root)),
            policy_config=policy_config,
        )

    @property
    def records_path(self) -> Path:
        return self._p.runs_root / "memory" / "contradictions.jsonl"

    @property
    def rollback_dir(self) -> Path:
        return self._p.runs_root / "memory" / "contradiction_rollbacks"

    async def resolve(
        self,
        *,
        run_id: str,
        step_id: str | None,
        old_value: dict[str, Any],
        new_value: dict[str, Any],
        old_selector: str | None = None,
        new_selector: str | None = None,
        old_confidence: float | None = None,
        new_confidence: float | None = None,
        route_changed: bool = False,
        frame_changed: bool = False,
        stale_ref_detected: bool = False,
        newer_evidence_validated: bool = True,
        manual_review_required: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> ContradictionResolution:
        contradiction_type = classify_contradiction(
            route_changed=route_changed,
            frame_changed=frame_changed,
            stale_ref_detected=stale_ref_detected,
        )
        decision, rationale = apply_policy(
            contradiction_type=contradiction_type,
            old_confidence=old_confidence,
            new_confidence=new_confidence,
            newer_evidence_validated=newer_evidence_validated,
            manual_review_required=manual_review_required,
            policy_config=self._policy,
        )

        conflict_id = generate_conflict_id()
        rollback_ref = self._persist_rollback_snapshot(
            conflict_id=conflict_id,
            run_id=run_id,
            step_id=step_id,
            old_value=old_value,
            old_selector=old_selector,
            old_confidence=old_confidence,
        )
        record = ContradictionRecord(
            conflictId=conflict_id,
            runId=run_id,
            stepId=step_id,
            contradictionType=contradiction_type,
            decision=decision,
            decisionRationale=rationale,
            oldSelector=old_selector,
            newSelector=new_selector,
            oldConfidence=old_confidence,
            newConfidence=new_confidence,
            rollbackRef=rollback_ref,
            metadata=dict(metadata or {}),
        )
        self._append_record(record)

        resolution = ContradictionResolution(
            record=record,
            resolvedValue=_resolve_value(decision=decision, old_value=old_value, new_value=new_value),
        )
        self._logger.info(
            "contradiction_resolved",
            conflict_id=record.conflict_id,
            run_id=record.run_id,
            step_id=record.step_id,
            contradiction_type=record.contradiction_type,
            decision=record.decision,
            rollback_ref=record.rollback_ref,
        )
        return resolution

    async def get(self, conflict_id: str) -> ContradictionRecord | None:
        if not self.records_path.exists():
            return None
        with self.records_path.open("r", encoding="utf-8") as file_obj:
            for line in file_obj:
                payload = line.strip()
                if not payload:
                    continue
                record = ContradictionRecord.model_validate(json.loads(payload))
                if record.conflict_id == conflict_id:
                    return record
        return None

    async def list(
        self,
        *,
        run_id: str | None = None,
        step_id: str | None = None,
        contradiction_type: ContradictionType | str | None = None,
        decision: ContradictionPolicyOutcome | str | None = None,
        limit: int = 500,
    ) -> list[ContradictionRecord]:
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        if not self.records_path.exists():
            return []

        contradiction_type_value = (
            contradiction_type.value
            if isinstance(contradiction_type, ContradictionType)
            else contradiction_type
        )
        decision_value = decision.value if isinstance(decision, ContradictionPolicyOutcome) else decision
        rows: list[ContradictionRecord] = []
        with self.records_path.open("r", encoding="utf-8") as file_obj:
            lines = file_obj.readlines()
        for line in reversed(lines):
            payload = line.strip()
            if not payload:
                continue
            record = ContradictionRecord.model_validate(json.loads(payload))
            if run_id is not None and record.run_id != run_id:
                continue
            if step_id is not None and record.step_id != step_id:
                continue
            if (
                contradiction_type_value is not None
                and record.contradiction_type != contradiction_type_value
            ):
                continue
            if decision_value is not None and record.decision != decision_value:
                continue
            rows.append(record)
            if len(rows) >= limit:
                break
        return rows

    def _append_record(self, record: ContradictionRecord) -> None:
        self.records_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.records_path.exists():
            self.records_path.write_text("", encoding="utf-8")
        with self.records_path.open("a", encoding="utf-8") as file_obj:
            payload = record.model_dump(mode="json", by_alias=True)
            file_obj.write(dumps_json(payload) + "\n")

    def _persist_rollback_snapshot(
        self,
        *,
        conflict_id: str,
        run_id: str,
        step_id: str | None,
        old_value: dict[str, Any],
        old_selector: str | None,
        old_confidence: float | None,
    ) -> str:
        self.rollback_dir.mkdir(parents=True, exist_ok=True)
        rollback_path = self.rollback_dir / f"{conflict_id}.json"
        payload = {
            "conflictId": conflict_id,
            "runId": run_id,
            "stepId": step_id,
            "oldSelector": old_selector,
            "oldConfidence": old_confidence,
            "oldValue": old_value,
            "savedAt": datetime.now(UTC).isoformat(),
        }
        rollback_path.write_text(dumps_json(payload), encoding="utf-8")
        return str(rollback_path)


def classify_contradiction(
    *,
    route_changed: bool,
    frame_changed: bool,
    stale_ref_detected: bool,
) -> ContradictionType:
    if stale_ref_detected:
        return ContradictionType.STALE_LOCATOR
    if route_changed or frame_changed:
        return ContradictionType.STRUCTURE_DRIFT
    return ContradictionType.CONTENT_DRIFT


def apply_policy(
    *,
    contradiction_type: ContradictionType,
    old_confidence: float | None,
    new_confidence: float | None,
    newer_evidence_validated: bool,
    manual_review_required: bool,
    policy_config: ContradictionPolicyConfig,
) -> tuple[ContradictionPolicyOutcome, str]:
    if manual_review_required:
        return (
            ContradictionPolicyOutcome.REQUIRE_MANUAL_REVIEW,
            "manual review required by caller policy",
        )

    if policy_config.require_manual_when_unvalidated and not newer_evidence_validated:
        return (
            ContradictionPolicyOutcome.REQUIRE_MANUAL_REVIEW,
            "newer evidence is not validated",
        )

    if old_confidence is not None and new_confidence is not None:
        if new_confidence + policy_config.confidence_epsilon >= old_confidence:
            return (
                ContradictionPolicyOutcome.ACCEPT_NEW,
                "validated newer evidence has equal-or-higher confidence",
            )
        return (
            ContradictionPolicyOutcome.DUAL_TRACK_WITH_FALLBACK,
            "newer evidence has lower confidence; retain previous fallback",
        )

    if contradiction_type == ContradictionType.STALE_LOCATOR:
        return (
            ContradictionPolicyOutcome.ACCEPT_NEW,
            "stale locator contradiction favors validated newer selector",
        )

    return (
        ContradictionPolicyOutcome.KEEP_OLD,
        "insufficient confidence data; preserve previous selector until revalidation",
    )


def _resolve_value(
    *,
    decision: ContradictionPolicyOutcome,
    old_value: dict[str, Any],
    new_value: dict[str, Any],
) -> dict[str, Any] | None:
    if decision == ContradictionPolicyOutcome.ACCEPT_NEW:
        return dict(new_value)
    if decision == ContradictionPolicyOutcome.KEEP_OLD:
        return dict(old_value)
    if decision == ContradictionPolicyOutcome.DUAL_TRACK_WITH_FALLBACK:
        merged = dict(new_value)
        fallback_selectors: list[str] = []
        for selector in _selectors_from_value(old_value):
            if selector not in fallback_selectors:
                fallback_selectors.append(selector)
        for selector in _selectors_from_value(new_value):
            if selector not in fallback_selectors:
                fallback_selectors.append(selector)
        if fallback_selectors:
            merged["fallbackSelectors"] = fallback_selectors
        return merged
    return None


def _selectors_from_value(value: dict[str, Any]) -> list[str]:
    selectors: list[str] = []
    primary = value.get("primarySelector")
    if isinstance(primary, str) and primary.strip():
        selectors.append(primary.strip())
    fallbacks = value.get("fallbackSelectors")
    if isinstance(fallbacks, list):
        for candidate in fallbacks:
            if isinstance(candidate, str) and candidate.strip():
                selectors.append(candidate.strip())
    return selectors

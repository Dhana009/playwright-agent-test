from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent.stepgraph.models import Step

_SUBMIT_MARKERS = (
    "submit",
    "save",
    "confirm",
    "complete",
    "checkout",
    "place order",
    "apply changes",
)
_DESTRUCTIVE_MARKERS = (
    "delete",
    "remove",
    "destroy",
    "purge",
    "drop",
    "reset",
    "revoke",
    "archive permanently",
)
_EXTERNAL_POST_MARKERS = (
    "send",
    "email",
    "webhook",
    "publish",
    "dispatch",
    "share externally",
    "post to",
)
_AUTH_MUTATION_MARKERS = (
    "role",
    "permission",
    "credential",
    "password",
    "token",
    "session",
    "auth",
    "mfa",
    "api key",
    "access level",
)


class ApprovalLevel(str, Enum):
    AUTO_ALLOW = "auto_allow"
    REVIEW = "review"
    HARD_APPROVAL = "hard_approval"


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    level: ApprovalLevel
    action: str
    summary: str
    reason_codes: list[str] = Field(default_factory=list, alias="reasonCodes")
    matched_signals: list[str] = Field(default_factory=list, alias="matchedSignals")
    decision_path: list[str] = Field(default_factory=list, alias="decisionPath")

    @property
    def requires_hard_approval(self) -> bool:
        return self.level is ApprovalLevel.HARD_APPROVAL


class HardApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    run_id: str = Field(alias="runId")
    step_id: str = Field(alias="stepId")
    action: str
    decision: ApprovalDecision
    attempt_index: int = Field(alias="attemptIndex", ge=0)


class ApprovalClassifier:
    def __init__(self) -> None:
        self._review_actions = {
            "click",
            "fill",
            "type",
            "press",
            "check",
            "uncheck",
            "select",
            "drag",
            "hover",
            "focus",
        }

    def classify(
        self,
        *,
        step: Step,
        metadata: Mapping[str, Any] | None = None,
        target_selectors: Sequence[str] | None = None,
    ) -> ApprovalDecision:
        action = step.action.strip().lower()
        md = metadata if metadata is not None else step.metadata
        selector_text = " ".join(target_selectors or [])
        text_blob = " ".join(
            [
                action,
                selector_text.lower(),
                _flatten_text_values(md).lower(),
            ]
        ).strip()
        decision_path = ["approval_classifier_v1", f"action={action}"]

        hard_reasons: list[str] = []
        matched_signals: list[str] = []

        if action == "upload":
            hard_reasons.append("local_file_upload")
            matched_signals.append("action=upload")

        submit_matches = _match_markers(text_blob, _SUBMIT_MARKERS)
        if submit_matches and action in {"click", "press"}:
            hard_reasons.append("final_submit_or_commit")
            matched_signals.extend(submit_matches)

        destructive_matches = _match_markers(text_blob, _DESTRUCTIVE_MARKERS)
        if destructive_matches:
            hard_reasons.append("destructive_mutation")
            matched_signals.extend(destructive_matches)

        external_matches = _match_markers(text_blob, _EXTERNAL_POST_MARKERS)
        if external_matches:
            hard_reasons.append("external_post_or_send")
            matched_signals.extend(external_matches)

        auth_matches = _match_markers(text_blob, _AUTH_MUTATION_MARKERS)
        if auth_matches:
            hard_reasons.append("auth_or_permission_mutation")
            matched_signals.extend(auth_matches)

        if hard_reasons:
            decision_path.append("classified=hard_approval")
            return ApprovalDecision(
                level=ApprovalLevel.HARD_APPROVAL,
                action=action,
                summary="Action requires explicit operator approval before execution.",
                reasonCodes=sorted(set(hard_reasons)),
                matchedSignals=sorted(set(matched_signals)),
                decisionPath=decision_path,
            )

        if action in self._review_actions:
            decision_path.append("classified=review")
            return ApprovalDecision(
                level=ApprovalLevel.REVIEW,
                action=action,
                summary="Action may mutate state and should be operator-reviewable.",
                reasonCodes=["state_mutation_possible"],
                matchedSignals=[],
                decisionPath=decision_path,
            )

        decision_path.append("classified=auto_allow")
        return ApprovalDecision(
            level=ApprovalLevel.AUTO_ALLOW,
            action=action,
            summary="Low-risk reversible action.",
            reasonCodes=["low_risk_reversible"],
            matchedSignals=[],
            decisionPath=decision_path,
        )


def _flatten_text_values(payload: Mapping[str, Any]) -> str:
    values: list[str] = []
    stack: list[Any] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, str):
            values.append(current)
    return " ".join(values)


def _match_markers(text_blob: str, markers: Sequence[str]) -> list[str]:
    if not text_blob:
        return []
    return [marker for marker in markers if marker in text_blob]

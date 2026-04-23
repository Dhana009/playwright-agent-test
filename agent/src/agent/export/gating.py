from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.stepgraph.models import Step, StepGraph


class ExportDecision(str, Enum):
    BLOCK = "block"
    REVIEW = "review"
    ALLOW = "allow"


class ExportGateReasonCode(str, Enum):
    LOW_CONFIDENCE_BLOCK = "low_confidence_block"
    REVIEW_BAND = "review_confidence_band"


class ExportGateThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    review_threshold: float = Field(default=0.70, ge=0.0, le=1.0, alias="reviewThreshold")
    allow_threshold: float = Field(default=0.85, ge=0.0, le=1.0, alias="allowThreshold")

    @model_validator(mode="after")
    def _validate_thresholds(self) -> "ExportGateThresholds":
        if self.review_threshold >= self.allow_threshold:
            raise ValueError("reviewThreshold must be less than allowThreshold")
        return self


class StepConfidenceGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)

    step_id: str = Field(alias="stepId")
    confidence_score: float | None = Field(default=None, alias="confidenceScore")
    decision: ExportDecision
    reason_code: str = Field(alias="reasonCode")
    reason: str


class ExportGateReason(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)

    code: ExportGateReasonCode
    message: str
    step_ids: list[str] = Field(default_factory=list, alias="stepIds")


class ExportConfidenceGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)

    decision: ExportDecision
    review_threshold: float = Field(alias="reviewThreshold")
    allow_threshold: float = Field(alias="allowThreshold")
    blocked_step_ids: list[str] = Field(default_factory=list, alias="blockedStepIds")
    review_step_ids: list[str] = Field(default_factory=list, alias="reviewStepIds")
    reasons: list[ExportGateReason] = Field(default_factory=list)
    step_results: list[StepConfidenceGateResult] = Field(default_factory=list, alias="stepResults")


def evaluate_export_confidence(
    step_graph: StepGraph,
    *,
    thresholds: ExportGateThresholds | None = None,
) -> ExportConfidenceGateResult:
    active_thresholds = thresholds or ExportGateThresholds()
    step_results: list[StepConfidenceGateResult] = []
    blocked_step_ids: list[str] = []
    review_step_ids: list[str] = []

    for step in step_graph.steps:
        step_result = evaluate_step_confidence(step=step, thresholds=active_thresholds)
        step_results.append(step_result)

        if step_result.decision == ExportDecision.BLOCK:
            blocked_step_ids.append(step_result.step_id)
        elif step_result.decision == ExportDecision.REVIEW:
            review_step_ids.append(step_result.step_id)

    if blocked_step_ids:
        decision = ExportDecision.BLOCK
    elif review_step_ids:
        decision = ExportDecision.REVIEW
    else:
        decision = ExportDecision.ALLOW

    reasons = _build_reasons(
        blocked_step_ids=blocked_step_ids,
        review_step_ids=review_step_ids,
        thresholds=active_thresholds,
    )

    return ExportConfidenceGateResult(
        decision=decision,
        reviewThreshold=active_thresholds.review_threshold,
        allowThreshold=active_thresholds.allow_threshold,
        blockedStepIds=blocked_step_ids,
        reviewStepIds=review_step_ids,
        reasons=reasons,
        stepResults=step_results,
    )


def evaluate_step_confidence(
    *,
    step: Step,
    thresholds: ExportGateThresholds,
) -> StepConfidenceGateResult:
    confidence_score = step.target.confidence_score if step.target is not None else None

    if confidence_score is None:
        return StepConfidenceGateResult(
            stepId=step.step_id,
            confidenceScore=None,
            decision=ExportDecision.ALLOW,
            reasonCode="no_locator_bundle",
            reason="step has no locator bundle confidence requirement",
        )

    if confidence_score < thresholds.review_threshold:
        return StepConfidenceGateResult(
            stepId=step.step_id,
            confidenceScore=confidence_score,
            decision=ExportDecision.BLOCK,
            reasonCode=ExportGateReasonCode.LOW_CONFIDENCE_BLOCK.value,
            reason=(
                f"confidence {confidence_score:.2f} is below block threshold "
                f"{thresholds.review_threshold:.2f}"
            ),
        )

    if confidence_score < thresholds.allow_threshold:
        return StepConfidenceGateResult(
            stepId=step.step_id,
            confidenceScore=confidence_score,
            decision=ExportDecision.REVIEW,
            reasonCode=ExportGateReasonCode.REVIEW_BAND.value,
            reason=(
                f"confidence {confidence_score:.2f} is between review threshold "
                f"{thresholds.review_threshold:.2f} and allow threshold "
                f"{thresholds.allow_threshold:.2f}"
            ),
        )

    return StepConfidenceGateResult(
        stepId=step.step_id,
        confidenceScore=confidence_score,
        decision=ExportDecision.ALLOW,
        reasonCode="confidence_allowed",
        reason=(
            f"confidence {confidence_score:.2f} meets allow threshold "
            f"{thresholds.allow_threshold:.2f}"
        ),
    )


def _build_reasons(
    *,
    blocked_step_ids: list[str],
    review_step_ids: list[str],
    thresholds: ExportGateThresholds,
) -> list[ExportGateReason]:
    reasons: list[ExportGateReason] = []

    if blocked_step_ids:
        reasons.append(
            ExportGateReason(
                code=ExportGateReasonCode.LOW_CONFIDENCE_BLOCK,
                message=(
                    f"one or more steps are below {thresholds.review_threshold:.2f}; "
                    "export is blocked"
                ),
                stepIds=blocked_step_ids,
            )
        )

    if review_step_ids:
        reasons.append(
            ExportGateReason(
                code=ExportGateReasonCode.REVIEW_BAND,
                message=(
                    f"one or more steps are between {thresholds.review_threshold:.2f} and "
                    f"{thresholds.allow_threshold:.2f}; explicit review annotation required"
                ),
                stepIds=review_step_ids,
            )
        )

    return reasons

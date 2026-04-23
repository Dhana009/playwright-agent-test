from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent.core.ids import generate_step_id


class StepMode(str, Enum):
    ACTION = "action"
    ASSERTION = "assertion"
    WAIT = "wait"
    NAVIGATION = "navigation"
    SYSTEM = "system"


class PreconditionType(str, Enum):
    ELEMENT_VISIBLE = "element_visible"
    ELEMENT_HIDDEN = "element_hidden"
    URL_MATCHES = "url_matches"
    TITLE_MATCHES = "title_matches"
    FRAME_SELECTED = "frame_selected"
    DIALOG_EXPECTED = "dialog_expected"
    CUSTOM = "custom"


class PostconditionType(str, Enum):
    ELEMENT_VISIBLE = "element_visible"
    ELEMENT_HIDDEN = "element_hidden"
    TEXT_MATCHES = "text_matches"
    URL_MATCHES = "url_matches"
    TITLE_MATCHES = "title_matches"
    VALUE_MATCHES = "value_matches"
    EVENT_EMITTED = "event_emitted"
    CUSTOM = "custom"


class RecoveryAction(str, Enum):
    RETRY = "retry"
    FORCE_FIX = "force_fix"
    MANUAL_FIX = "manual_fix"
    LLM_ASSIST = "llm_assist"
    SKIP = "skip"
    ABORT = "abort"


class TimeoutWaitUntil(str, Enum):
    LOAD = "load"
    DOM_CONTENT_LOADED = "domcontentloaded"
    NETWORK_IDLE = "networkidle"
    COMMIT = "commit"


class LocatorBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    primary_selector: str = Field(alias="primarySelector")
    fallback_selectors: list[str] = Field(default_factory=list, alias="fallbackSelectors")
    confidence_score: float = Field(alias="confidenceScore", ge=0.0, le=1.0)
    reasoning_hint: str | None = Field(default=None, alias="reasoningHint")
    frame_context: list[str] = Field(default_factory=list, alias="frameContext")


class Precondition(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: PreconditionType
    payload: dict[str, Any] = Field(default_factory=dict)


class Postcondition(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: PostconditionType
    payload: dict[str, Any] = Field(default_factory=dict)


class TimeoutPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    timeout_ms: int = Field(default=30_000, ge=0, alias="timeoutMs")
    poll_interval_ms: int = Field(default=250, ge=0, alias="pollIntervalMs")
    wait_until: TimeoutWaitUntil | None = Field(default=None, alias="waitUntil")


class RecoveryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    max_retries: int = Field(default=0, ge=0, alias="maxRetries")
    retry_backoff_ms: int = Field(default=0, ge=0, alias="retryBackoffMs")
    allowed_actions: list[RecoveryAction] = Field(
        default_factory=lambda: [RecoveryAction.RETRY],
        alias="allowedActions",
    )
    failure_reason_codes: list[str] = Field(
        default_factory=list,
        alias="failureReasonCodes",
    )


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    step_id: str = Field(default_factory=generate_step_id, alias="stepId")
    mode: StepMode
    action: str
    target: LocatorBundle | None = None
    preconditions: list[Precondition] = Field(default_factory=list)
    postconditions: list[Postcondition] = Field(default_factory=list)
    timeout_policy: TimeoutPolicy = Field(default_factory=TimeoutPolicy, alias="timeoutPolicy")
    recovery_policy: RecoveryPolicy = Field(
        default_factory=RecoveryPolicy,
        alias="recoveryPolicy",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class StepEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_step_id: str = Field(alias="fromStepId")
    to_step_id: str = Field(alias="toStepId")
    condition: str | None = None


class StepGraph(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    run_id: str = Field(alias="runId")
    steps: list[Step] = Field(default_factory=list)
    edges: list[StepEdge] = Field(default_factory=list)
    version: str = "1.0"

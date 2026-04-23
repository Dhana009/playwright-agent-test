from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.core.ids import generate_event_id


class EventType(str, Enum):
    STEP_STARTED = "step_started"
    STEP_SUCCEEDED = "step_succeeded"
    STEP_FAILED = "step_failed"
    STEP_RETRIED = "step_retried"
    RUN_PAUSED = "run_paused"
    RUN_RESUMED = "run_resumed"
    INTERVENTION_RECORDED = "intervention_recorded"
    MODE_SWITCHED = "mode_switched"
    RUN_COMPLETED = "run_completed"
    RUN_ABORTED = "run_aborted"


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    event_id: str = Field(default_factory=generate_event_id, alias="event_id")
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    run_id: str
    step_id: str | None = None
    actor: str = Field(min_length=1)
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)


class StepStartedEvent(Event):
    type: Literal[EventType.STEP_STARTED] = EventType.STEP_STARTED
    step_id: str


class StepSucceededEvent(Event):
    type: Literal[EventType.STEP_SUCCEEDED] = EventType.STEP_SUCCEEDED
    step_id: str


class StepFailedEvent(Event):
    type: Literal[EventType.STEP_FAILED] = EventType.STEP_FAILED
    step_id: str


class StepRetriedEvent(Event):
    type: Literal[EventType.STEP_RETRIED] = EventType.STEP_RETRIED
    step_id: str


class RunPausedEvent(Event):
    type: Literal[EventType.RUN_PAUSED] = EventType.RUN_PAUSED


class RunResumedEvent(Event):
    type: Literal[EventType.RUN_RESUMED] = EventType.RUN_RESUMED


class InterventionRecordedEvent(Event):
    type: Literal[EventType.INTERVENTION_RECORDED] = EventType.INTERVENTION_RECORDED
    step_id: str


class ModeSwitchedEvent(Event):
    type: Literal[EventType.MODE_SWITCHED] = EventType.MODE_SWITCHED


class RunCompletedEvent(Event):
    type: Literal[EventType.RUN_COMPLETED] = EventType.RUN_COMPLETED


class RunAbortedEvent(Event):
    type: Literal[EventType.RUN_ABORTED] = EventType.RUN_ABORTED


ExecutionEvent = Annotated[
    StepStartedEvent
    | StepSucceededEvent
    | StepFailedEvent
    | StepRetriedEvent
    | RunPausedEvent
    | RunResumedEvent
    | InterventionRecordedEvent
    | ModeSwitchedEvent
    | RunCompletedEvent
    | RunAbortedEvent,
    Field(discriminator="type"),
]

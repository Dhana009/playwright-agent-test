from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class CacheDecision(str, Enum):
    REUSE = "reuse"
    PARTIAL_REFRESH = "partial_refresh"
    FULL_REFRESH = "full_refresh"


class ContextFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    route_template: str = Field(alias="routeTemplate")
    dom_hash: str = Field(alias="domHash")
    frame_hash: str = Field(alias="frameHash")
    modal_state: str = Field(alias="modalState")


class CacheRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)

    run_id: str = Field(alias="runId")
    step_id: str = Field(alias="stepId")
    fingerprint: ContextFingerprint
    decision: CacheDecision
    decision_reasons: list[str] = Field(default_factory=list, alias="decisionReasons")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        alias="createdAt",
    )

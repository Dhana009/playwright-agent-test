from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    current_step_id: str = Field(alias="currentStepId")
    event_offset: int = Field(ge=0, alias="eventOffset")
    browser_session_id: str = Field(alias="browserSessionId")
    tab_id: str = Field(alias="tabId")
    frame_path: list[str] = Field(default_factory=list, alias="framePath")
    storage_state_ref: str | None = Field(default=None, alias="storageStateRef")
    paused_recovery_state: dict[str, Any] | None = Field(
        default=None,
        alias="pausedRecoveryState",
    )

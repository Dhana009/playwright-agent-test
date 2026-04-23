from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent.core.logging import get_logger
from agent.execution.checkpoint_writer import CheckpointWriter
from agent.execution.events import EventType, ModeSwitchedEvent
from agent.policy.audit import AuditLogger
from agent.storage.repos.events import EventRepository


class RuntimeMode(str, Enum):
    MANUAL = "manual"
    LLM = "llm"
    HYBRID = "hybrid"


class RuntimeBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1)
    current_step_id: str | None = None
    browser_session_id: str | None = None
    tab_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModeSwitchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    previous_mode: RuntimeMode
    active_mode: RuntimeMode
    changed: bool
    reason: str
    actor: str
    runtime_state_reset: bool = False
    event_id: str | None = None
    run_id: str | None = None
    step_id: str | None = None


class ModeController:
    def __init__(self, *, initial_mode: RuntimeMode) -> None:
        self._logger = get_logger(__name__)
        self._active_mode = initial_mode

    @property
    def active_mode(self) -> RuntimeMode:
        return self._active_mode

    async def switch_mode(
        self,
        *,
        target_mode: RuntimeMode | str,
        reason: str,
        actor: str = "operator",
        binding: RuntimeBinding | None = None,
        sqlite_path: str | Path | None = None,
        runs_root: str | Path | None = None,
    ) -> ModeSwitchResult:
        resolved_target = _normalize_mode(target_mode)
        previous_mode = self._active_mode
        changed = resolved_target != previous_mode

        if changed:
            self._active_mode = resolved_target

        event_id: str | None = None
        if binding is not None:
            payload = {
                "previousMode": previous_mode.value,
                "newMode": resolved_target.value,
                "reason": reason,
                "runtimeStateReset": False,
                "browserSessionId": binding.browser_session_id,
                "tabId": binding.tab_id,
                "metadata": dict(binding.metadata),
            }
            event = ModeSwitchedEvent(
                run_id=binding.run_id,
                step_id=binding.current_step_id,
                actor=actor,
                type=EventType.MODE_SWITCHED,
                payload=payload,
            )
            writer = CheckpointWriter.for_run(
                run_id=binding.run_id,
                sqlite_path=sqlite_path,
                runs_root=runs_root,
            )
            await writer.emit_event(event)
            audit_logger = AuditLogger.for_run(run_id=binding.run_id, runs_root=runs_root)
            audit_logger.record_mode_switch(
                actor=actor,
                previous_mode=previous_mode.value,
                new_mode=resolved_target.value,
                reason=reason,
                step_id=binding.current_step_id,
            )
            event_id = event.event_id

        self._logger.info(
            "runtime_mode_switched",
            previous_mode=previous_mode.value,
            active_mode=self._active_mode.value,
            changed=changed,
            reason=reason,
            actor=actor,
            run_id=binding.run_id if binding is not None else None,
            step_id=binding.current_step_id if binding is not None else None,
            runtime_state_reset=False,
            event_id=event_id,
        )

        return ModeSwitchResult(
            previous_mode=previous_mode,
            active_mode=self._active_mode,
            changed=changed,
            reason=reason,
            actor=actor,
            runtime_state_reset=False,
            event_id=event_id,
            run_id=binding.run_id if binding is not None else None,
            step_id=binding.current_step_id if binding is not None else None,
        )


async def resolve_mode_for_run(
    *,
    run_id: str,
    fallback_mode: RuntimeMode | str,
    sqlite_path: str | Path | None = None,
) -> RuntimeMode:
    resolved_fallback = _normalize_mode(fallback_mode)
    events = await EventRepository(sqlite_path=sqlite_path).load_for_run(run_id, limit=5000)
    for event in reversed(events):
        if event.type != EventType.MODE_SWITCHED:
            continue
        payload = event.payload
        new_mode = payload.get("newMode") or payload.get("new_mode")
        if isinstance(new_mode, str):
            try:
                return RuntimeMode(new_mode.strip().lower())
            except ValueError:
                continue
    return resolved_fallback


def _normalize_mode(value: RuntimeMode | str) -> RuntimeMode:
    if isinstance(value, RuntimeMode):
        return value
    normalized = value.strip().lower()
    return RuntimeMode(normalized)

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.core.logging import get_logger
from agent.execution.checkpoint import Checkpoint
from agent.execution.events import Event
from agent.storage.files import RunLayout, get_run_layout
from agent.storage.repos._common import dumps_json
from agent.storage.repos.checkpoints import CheckpointRepository
from agent.storage.repos.events import EventRepository


@dataclass
class ExecutionPersistence:
    run_id: str
    event_repo: EventRepository
    checkpoint_repo: CheckpointRepository
    layout: RunLayout

    event_offset: int = 0

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must be non-empty")


class CheckpointWriter:
    def __init__(
        self,
        persistence: ExecutionPersistence,
        *,
        also_write_jsonl: bool = True,
    ) -> None:
        self._logger = get_logger(__name__)
        self._p = persistence
        self._also_write_jsonl = also_write_jsonl

    @classmethod
    def for_run(
        cls,
        *,
        run_id: str,
        sqlite_path: str | Path | None = None,
        runs_root: str | Path | None = None,
        also_write_jsonl: bool = True,
    ) -> "CheckpointWriter":
        layout = get_run_layout(run_id, runs_root=runs_root)
        persistence = ExecutionPersistence(
            run_id=run_id,
            event_repo=EventRepository(sqlite_path=sqlite_path),
            checkpoint_repo=CheckpointRepository(sqlite_path=sqlite_path),
            layout=layout,
        )
        return cls(persistence, also_write_jsonl=also_write_jsonl)

    @property
    def event_offset(self) -> int:
        return self._p.event_offset

    async def emit_event(self, event: Event) -> None:
        if event.run_id != self._p.run_id:
            raise ValueError("event.run_id mismatch")

        if self._also_write_jsonl:
            await self._append_event_jsonl(event)

        await self._p.event_repo.save(event)
        self._p.event_offset += 1

    async def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        await self._p.checkpoint_repo.save(self._p.run_id, checkpoint)

    async def checkpoint_now(
        self,
        *,
        current_step_id: str,
        browser_session_id: str,
        tab_id: str,
        frame_path: list[str] | None = None,
        storage_state_ref: str | None = None,
        paused_recovery_state: dict[str, Any] | None = None,
    ) -> Checkpoint:
        checkpoint = Checkpoint(
            currentStepId=current_step_id,
            eventOffset=self._p.event_offset,
            browserSessionId=browser_session_id,
            tabId=tab_id,
            framePath=list(frame_path or []),
            storageStateRef=storage_state_ref,
            pausedRecoveryState=paused_recovery_state,
        )
        await self.save_checkpoint(checkpoint)
        return checkpoint

    async def _append_event_jsonl(self, event: Event) -> None:
        payload = event.model_dump(mode="json")
        line = dumps_json(payload) + "\n"
        path = self._p.layout.events_jsonl
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8") if not path.exists() else None
        # append
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
        self._logger.debug("event_jsonl_appended", run_id=self._p.run_id, path=str(path))


class RunnerEventSink:
    """
    Adapter to pass into `StepGraphRunner(event_sink=...)`.
    """

    def __init__(self, writer: CheckpointWriter) -> None:
        self._writer = writer

    async def emit(self, event: Any) -> None:
        if isinstance(event, Event):
            await self._writer.emit_event(event)


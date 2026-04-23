from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from agent.cache.engine import CacheEngine
from agent.core.config import Settings
from agent.core.logging import get_logger
from agent.execution.browser import BrowserSession
from agent.execution.checkpoint_writer import CheckpointWriter, RunnerEventSink
from agent.execution.runner import StepGraphRunner
from agent.execution.snapshot import SnapshotEngine
from agent.execution.tools import ToolRuntime
from agent.policy.approval import ApprovalClassifier, HardApprovalRequest
from agent.policy.audit import AuditLogger
from agent.policy.restrictions import RestrictionsPolicy
from agent.stepgraph.models import StepGraph
from agent.storage.files import get_run_layout
from agent.storage.repos.checkpoints import CheckpointRepository
from agent.storage.repos.step_graph import StepGraphRepository


app = typer.Typer(help="Run/resume/pause Step Graph executions (manual mode).")
LOGGER = get_logger(__name__)


def _pause_marker_path(run_id: str) -> Path:
    layout = get_run_layout(run_id)
    return layout.run_dir / ".pause"


def _is_pause_requested(run_id: str) -> bool:
    return _pause_marker_path(run_id).exists()


async def _run_graph(
    graph: StepGraph,
    *,
    start_step_id: str | None = None,
    auto_approve_hard: bool = False,
) -> None:
    run_id = graph.run_id
    pause_marker = _pause_marker_path(run_id)
    pause_marker.unlink(missing_ok=True)
    settings = Settings.load()

    step_graph_repo = StepGraphRepository(sqlite_path=settings.storage.sqlite_path)
    await step_graph_repo.save(graph)

    writer = CheckpointWriter.for_run(run_id=run_id, sqlite_path=settings.storage.sqlite_path)
    sink = RunnerEventSink(writer)
    audit_logger = AuditLogger.for_run(run_id=run_id)
    restrictions_policy = RestrictionsPolicy.from_settings(settings.policy)

    session = BrowserSession(headless=False)
    await session.start()
    try:
        _, context = await session.new_context()
        page = await context.new_page()
        tab_id = session.get_tab_id(page)
        if tab_id is None:
            raise RuntimeError("Failed to acquire tab_id for newly created page.")

        # The runner requires `metadata.tabId` per step. Since the CLI owns the session/tab,
        # we normalize every step to target the newly created tab.
        for step in graph.steps:
            step.metadata["tabId"] = tab_id

        snapshot_engine = SnapshotEngine(session)
        cache_engine = CacheEngine(session)

        def _emit_tool_audit(event: object) -> None:
            audit_logger.record_tool_call(event)

        runtime = ToolRuntime(
            session,
            snapshot_engine=snapshot_engine,
            event_emitter=_emit_tool_audit,
        )

        def _hard_approval_prompt(request: HardApprovalRequest) -> bool:
            if auto_approve_hard:
                typer.echo(
                    f"[auto-approve] hard approval granted for {request.step_id} ({request.action})"
                )
                return True

            typer.echo("")
            typer.echo(f"HARD APPROVAL REQUIRED: step={request.step_id} action={request.action}")
            typer.echo(f"  reason_codes={','.join(request.decision.reason_codes)}")
            if request.decision.matched_signals:
                typer.echo(f"  matched_signals={','.join(request.decision.matched_signals)}")
            return typer.confirm("Approve this action?", default=False)

        runner = StepGraphRunner(
            runtime,
            event_sink=sink,
            cache_engine=cache_engine,
            snapshot_engine=snapshot_engine,
            approval_classifier=ApprovalClassifier(),
            hard_approval_resolver=_hard_approval_prompt,
            restrictions_policy=restrictions_policy,
            audit_logger=audit_logger,
        )

        await runner.run(graph, start_step_id=start_step_id, pause_requested=lambda: _is_pause_requested(run_id))

        # best-effort checkpoint on clean completion (or pause) at "current step"
        last_step_id = graph.steps[-1].step_id if graph.steps else graph.run_id
        await writer.checkpoint_now(
            current_step_id=last_step_id,
            browser_session_id=session.browser_session_id,
            tab_id=tab_id,
            frame_path=[],
        )
    finally:
        await session.stop()


@app.command("run")
def run(
    stepgraph_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    auto_approve_hard: bool = typer.Option(
        False,
        "--auto-approve-hard",
        help="Auto-approve hard-risk actions instead of prompting interactively.",
    ),
) -> None:
    """
    Start a run from a Step Graph JSON file.

    The Step Graph JSON must include `runId`. This command binds all steps to the
    active browser tab by injecting `metadata.tabId` at runtime.
    """

    async def _impl() -> None:
        graph = StepGraph.model_validate_json(stepgraph_path.read_text(encoding="utf-8"))
        await _run_graph(graph, auto_approve_hard=auto_approve_hard)

    asyncio.run(_impl())


@app.command("resume")
def resume(
    run_id: str = typer.Argument(...),
    auto_approve_hard: bool = typer.Option(
        False,
        "--auto-approve-hard",
        help="Auto-approve hard-risk actions instead of prompting interactively.",
    ),
) -> None:
    """
    Resume a paused/interrupted run from the latest checkpoint.
    """

    async def _impl() -> None:
        settings = Settings.load()
        checkpoint_repo = CheckpointRepository(sqlite_path=settings.storage.sqlite_path)
        checkpoint = await checkpoint_repo.load_latest(run_id)
        if checkpoint is None:
            raise typer.BadParameter(f"No checkpoint found for run_id '{run_id}'.")

        step_graph_repo = StepGraphRepository(sqlite_path=settings.storage.sqlite_path)
        graph = await step_graph_repo.load(run_id)
        if graph is None:
            raise typer.BadParameter(
                f"No step graph found for run_id '{run_id}'. Run `agent run <stepgraph.json>` first."
            )

        await _run_graph(
            graph,
            start_step_id=checkpoint.current_step_id,
            auto_approve_hard=auto_approve_hard,
        )

    asyncio.run(_impl())


@app.command("pause")
def pause(run_id: str = typer.Argument(...)) -> None:
    """
    Request an in-flight run to pause (creates a pause marker in the run dir).
    """
    marker = _pause_marker_path(run_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("pause\n", encoding="utf-8")
    LOGGER.info("pause_requested", run_id=run_id, marker=str(marker))


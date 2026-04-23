from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import typer

from agent.core.logging import get_logger
from agent.execution.checkpoint_writer import CheckpointWriter
from agent.execution.events import EventType, InterventionRecordedEvent
from agent.policy.audit import AuditLogger
from agent.stepgraph.models import LocatorBundle
from agent.storage.repos.events import EventRepository
from agent.storage.repos.step_graph import StepGraphRepository


app = typer.Typer(help="Manual fix flow for failed steps.")
LOGGER = get_logger(__name__)


@dataclass(frozen=True)
class FailedStepInfo:
    step_id: str
    error: str | None


def _pick_latest_failed_step(events: list[dict[str, Any]]) -> FailedStepInfo | None:
    # Events are stored as validated `Event` models, but the repo returns base `Event`.
    # We only rely on `type`, `step_id`, and `payload.error`.
    failed = [e for e in events if e.get("type") == EventType.STEP_FAILED.value and e.get("step_id")]
    if not failed:
        return None
    last = failed[-1]
    payload = last.get("payload") or {}
    return FailedStepInfo(step_id=str(last["step_id"]), error=payload.get("error"))


def _force_fix_bundle(bundle: LocatorBundle) -> LocatorBundle:
    # Deterministic "force" for now: treat all selectors as fallbacks so runner will try all.
    selectors = [bundle.primary_selector, *bundle.fallback_selectors]
    selectors = [s for s in selectors if s.strip()]
    if not selectors:
        return bundle
    primary = selectors[0]
    fallbacks = selectors[1:]
    return LocatorBundle(
        primarySelector=primary,
        fallbackSelectors=fallbacks,
        confidenceScore=bundle.confidence_score,
        reasoningHint=bundle.reasoning_hint,
        frameContext=bundle.frame_context,
    )


async def _apply_fix_and_resume(
    *,
    run_id: str,
    step_id: str,
    fix_type: Literal["force-fix", "manual-fix"],
    selector: str | None,
) -> None:
    step_graph_repo = StepGraphRepository()
    graph = await step_graph_repo.load(run_id)
    if graph is None:
        raise typer.BadParameter(f"No step graph found for run_id '{run_id}'.")

    step = next((s for s in graph.steps if s.step_id == step_id), None)
    if step is None:
        raise typer.BadParameter(f"Step '{step_id}' not found in run '{run_id}'.")
    if step.target is None:
        raise typer.BadParameter(f"Step '{step_id}' has no target locator bundle.")

    before = step.target.primary_selector
    if fix_type == "manual-fix":
        if selector is None or not selector.strip():
            raise typer.BadParameter("--selector is required for manual-fix.")
        step.target.primary_selector = selector
    else:
        step.target = _force_fix_bundle(step.target)

    await step_graph_repo.save(graph)

    writer = CheckpointWriter.for_run(run_id=run_id)

    intervention_event = InterventionRecordedEvent(
        run_id=run_id,
        step_id=step_id,
        actor="operator",
        type=EventType.INTERVENTION_RECORDED,
        payload={
            "fix_type": fix_type,
            "selector_before": before,
            "selector_after": step.target.primary_selector,
        },
    )
    await writer.emit_event(intervention_event)
    audit_logger = AuditLogger.for_run(run_id=run_id)
    audit_logger.record_intervention(intervention_event)

    # Resume execution from the fixed step using the same runner wiring as the CLI run command.
    # Browser session ownership is handled by the caller (Phase 5.3 starts a new BrowserSession).
    raise typer.BadParameter(
        "Fix recorded. Resume via `agent resume <run_id>` (Phase 5.3) to continue from checkpoint."
    )


@app.command("fix")
def fix(
    run_id: str = typer.Argument(...),
    step_id: str | None = typer.Option(None, "--step-id", help="Step id to fix (defaults to latest failed)."),
    fix_type: Literal["force-fix", "manual-fix"] = typer.Option(
        "manual-fix",
        "--type",
        help="Which fix flow to apply.",
    ),
    selector: str | None = typer.Option(None, "--selector", help="Selector for manual-fix."),
) -> None:
    """
    Apply a manual operator fix and record `intervention_recorded`.
    """
    # typer commands are sync; use asyncio via a minimal loop runner
    async def _impl() -> None:
        if step_id is None:
            repo = EventRepository()
            events = await repo.load_for_run(run_id, limit=500)
            event_dicts = [e.model_dump(mode="json") for e in events]
            latest = _pick_latest_failed_step(event_dicts)
            if latest is None:
                raise typer.BadParameter(f"No failed step found for run_id '{run_id}'.")
            chosen_step_id = latest.step_id
            LOGGER.info(
                "fix_target_selected",
                run_id=run_id,
                step_id=chosen_step_id,
                error=latest.error,
            )
        else:
            chosen_step_id = step_id

        await _apply_fix_and_resume(
            run_id=run_id,
            step_id=chosen_step_id,
            fix_type=fix_type,
            selector=selector,
        )

    import asyncio

    asyncio.run(_impl())


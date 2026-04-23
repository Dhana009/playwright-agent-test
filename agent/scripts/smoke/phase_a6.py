from __future__ import annotations

import argparse
import asyncio
import json
import signal
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(SCRIPT_DIR))

from agent.cache.engine import CacheEngine  # noqa: E402
from agent.cli.fix_cmd import _apply_fix_and_resume  # noqa: E402
from agent.core.config import Settings  # noqa: E402
from agent.core.ids import generate_run_id  # noqa: E402
from agent.execution.browser import BrowserSession  # noqa: E402
from agent.execution.checkpoint_writer import CheckpointWriter, RunnerEventSink  # noqa: E402
from agent.execution.events import (  # noqa: E402
    EventType,
    RunCompletedEvent,
    RunResumedEvent,
    StepSucceededEvent,
)
from agent.execution.runner import StepGraphRunner  # noqa: E402
from agent.execution.snapshot import SnapshotEngine  # noqa: E402
from agent.execution.tools import ToolRuntime  # noqa: E402
from agent.policy.approval import ApprovalClassifier, HardApprovalRequest  # noqa: E402
from agent.policy.audit import AuditLogger  # noqa: E402
from agent.policy.restrictions import RestrictionsPolicy  # noqa: E402
from agent.stepgraph.models import (  # noqa: E402
    LocatorBundle,
    RecoveryPolicy,
    Step,
    StepGraph,
    StepMode,
    TimeoutPolicy,
)
from agent.storage.files import get_run_layout  # noqa: E402
from agent.storage.repos.cache import CacheRepository  # noqa: E402
from agent.storage.repos.checkpoints import CheckpointRepository  # noqa: E402
from agent.storage.repos.events import EventRepository  # noqa: E402
from agent.storage.repos.step_graph import StepGraphRepository  # noqa: E402
from fixtures import running_server  # noqa: E402
from _runner import SmokeRunner  # noqa: E402


async def _resolve_tab_id(session: BrowserSession, page) -> str:
    for _ in range(30):
        tab_id = session.get_tab_id(page)
        if tab_id is not None:
            return tab_id
        await asyncio.sleep(0.01)
    raise RuntimeError("Timed out waiting for BrowserSession tab registration")


class _PauseAfterFirstSuccessSink:
    """Creates a `.pause` marker in the run dir after the first step succeeds."""

    def __init__(self, writer: CheckpointWriter, marker: Path) -> None:
        self._inner = RunnerEventSink(writer)
        self._marker = marker
        self._armed = True

    async def emit(self, event: Any) -> None:
        await self._inner.emit(event)
        if self._armed and isinstance(event, StepSucceededEvent):
            self._marker.parent.mkdir(parents=True, exist_ok=True)
            self._marker.write_text("pause\n", encoding="utf-8")
            self._armed = False


async def _checkpoint_after_run(
    *,
    writer: CheckpointWriter,
    event_repo: EventRepository,
    run_id: str,
    graph: StepGraph,
    browser_session_id: str,
    tab_id: str,
) -> None:
    events = await event_repo.load_for_run(run_id, limit=5000)
    last = events[-1] if events else None
    if last is not None and last.type == EventType.RUN_PAUSED:
        next_step_id = (last.payload or {}).get("next_step_id")
        if not isinstance(next_step_id, str) or not next_step_id.strip():
            raise RuntimeError("run_paused event missing payload.next_step_id")
        await writer.checkpoint_now(
            current_step_id=next_step_id,
            browser_session_id=browser_session_id,
            tab_id=tab_id,
            frame_path=[],
        )
        return

    last_step_id = graph.steps[-1].step_id if graph.steps else graph.run_id
    await writer.checkpoint_now(
        current_step_id=last_step_id,
        browser_session_id=browser_session_id,
        tab_id=tab_id,
        frame_path=[],
    )


async def _run_graph_harness(
    graph: StepGraph,
    *,
    sqlite_path: Path,
    runs_root: Path,
    start_step_id: str | None = None,
    headless: bool = True,
    auto_approve_hard: bool = True,
    event_sink: Any | None = None,
    pause_requested: Callable[[], bool] | None = None,
    resume_warmup_url: str | None = None,
) -> None:
    run_id = graph.run_id
    settings = Settings.load(overrides={"storage": {"sqlite_path": str(sqlite_path)}})
    step_graph_repo = StepGraphRepository(sqlite_path=sqlite_path)
    await step_graph_repo.save(graph)

    writer = CheckpointWriter.for_run(
        run_id=run_id,
        sqlite_path=sqlite_path,
        runs_root=runs_root,
    )
    sink = event_sink if event_sink is not None else RunnerEventSink(writer)
    audit_logger = AuditLogger.for_run(run_id=run_id, runs_root=runs_root)
    restrictions_policy = RestrictionsPolicy.from_settings(settings.policy)
    event_repo = EventRepository(sqlite_path=sqlite_path)

    session = BrowserSession(headless=headless)
    await session.start()
    try:
        _, context = await session.new_context()
        page = await context.new_page()
        tab_id = await _resolve_tab_id(session, page)
        for step in graph.steps:
            step.metadata["tabId"] = tab_id

        if start_step_id is not None and resume_warmup_url:
            await page.goto(resume_warmup_url, wait_until="domcontentloaded", timeout=30_000)

        snapshot_engine = SnapshotEngine(session)
        cache_engine = CacheEngine(
            session,
            cache_repo=CacheRepository(sqlite_path=sqlite_path),
        )

        def _emit_tool_audit(event: object) -> None:
            audit_logger.record_tool_call(event)

        runtime = ToolRuntime(
            session,
            snapshot_engine=snapshot_engine,
            event_emitter=_emit_tool_audit,
        )

        def _hard_approval_prompt(request: HardApprovalRequest) -> bool:
            if auto_approve_hard:
                return True
            return False

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

        if start_step_id is not None:
            await writer.emit_event(
                RunResumedEvent(
                    run_id=run_id,
                    actor="smoke_a6",
                    type=EventType.RUN_RESUMED,
                    payload={"start_step_id": start_step_id},
                )
            )

        try:
            await runner.run(
                graph,
                start_step_id=start_step_id,
                pause_requested=pause_requested,
            )
        except Exception:
            events = await event_repo.load_for_run(run_id, limit=5000)
            failed_step_id: str | None = None
            for ev in reversed(events):
                if ev.type == EventType.STEP_FAILED and ev.step_id:
                    failed_step_id = ev.step_id
                    break
            if failed_step_id is not None:
                await writer.checkpoint_now(
                    current_step_id=failed_step_id,
                    browser_session_id=session.browser_session_id,
                    tab_id=tab_id,
                    frame_path=[],
                )
            raise

        await _checkpoint_after_run(
            writer=writer,
            event_repo=event_repo,
            run_id=run_id,
            graph=graph,
            browser_session_id=session.browser_session_id,
            tab_id=tab_id,
        )
    finally:
        await session.stop()


def _event_counts(events: list[Any]) -> Counter[str]:
    return Counter(str(ev.type) for ev in events)


def _parse_events_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


async def _run_with_pre_step_checkpoints(
    *,
    graph: StepGraph,
    sqlite_path: Path,
    runs_root: Path,
    base_url: str,
    headless: bool = True,
) -> None:
    """Checkpoint immediately before each step, then run that step (for SIGKILL durability)."""
    run_id = graph.run_id
    settings = Settings.load(overrides={"storage": {"sqlite_path": str(sqlite_path)}})
    writer = CheckpointWriter.for_run(
        run_id=run_id,
        sqlite_path=sqlite_path,
        runs_root=runs_root,
    )
    sink = RunnerEventSink(writer)
    audit_logger = AuditLogger.for_run(run_id=run_id, runs_root=runs_root)
    restrictions_policy = RestrictionsPolicy.from_settings(settings.policy)

    session = BrowserSession(headless=headless)
    await session.start()
    try:
        _, context = await session.new_context()
        page = await context.new_page()
        tab_id = await _resolve_tab_id(session, page)
        for step in graph.steps:
            step.metadata["tabId"] = tab_id
            if step.action == "navigate":
                u = str(step.metadata.get("url", "")).replace("http://fixture.test", base_url)
                step.metadata["url"] = u

        snapshot_engine = SnapshotEngine(session)
        cache_engine = CacheEngine(
            session,
            cache_repo=CacheRepository(sqlite_path=sqlite_path),
        )

        def _emit_tool_audit(event: object) -> None:
            audit_logger.record_tool_call(event)

        runtime = ToolRuntime(
            session,
            snapshot_engine=snapshot_engine,
            event_emitter=_emit_tool_audit,
        )

        runner = StepGraphRunner(
            runtime,
            event_sink=sink,
            cache_engine=cache_engine,
            snapshot_engine=snapshot_engine,
            approval_classifier=ApprovalClassifier(),
            hard_approval_resolver=lambda _r: True,
            restrictions_policy=restrictions_policy,
            audit_logger=audit_logger,
        )

        for step in graph.steps:
            await writer.checkpoint_now(
                current_step_id=step.step_id,
                browser_session_id=session.browser_session_id,
                tab_id=tab_id,
                frame_path=[],
            )
            await asyncio.sleep(0.08)
            await runner._run_step(graph, step)

        await sink.emit(
            RunCompletedEvent(
                run_id=run_id,
                actor="runner",
                type=EventType.RUN_COMPLETED,
                payload={},
            )
        )
    finally:
        await session.stop()


async def _sigkill_child_async(config_path: str) -> None:
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    sqlite_path = Path(cfg["sqlite_path"])
    runs_root = Path(cfg["runs_root"])
    base_url = str(cfg["base_url"])
    graph = StepGraph.model_validate(cfg["graph"])
    await _run_with_pre_step_checkpoints(
        graph=graph,
        sqlite_path=sqlite_path,
        runs_root=runs_root,
        base_url=base_url,
        headless=True,
    )


def _sigkill_child_sync() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    # argv: [<this_script>, "--a6-sigkill-child", "--config", <path>, ...]
    args = parser.parse_args(sys.argv[2:])
    asyncio.run(_sigkill_child_async(args.config))


async def main() -> int:
    smoke = SmokeRunner(phase="A6", default_task="A6.1")

    with smoke.case("a6_1_full_run_events", task="A6.1", feature="runner_checkpoint"):
        with tempfile.TemporaryDirectory(prefix="a6-smoke-") as tmp:
            root = Path(tmp)
            db = root / "t.sqlite"
            runs_root = root / "runs"
            run_id = generate_run_id()
            sid1, sid2 = "a6_full_s01", "a6_full_s02"
            with running_server() as fx:
                url = f"{fx.base_url}/dashboard.html"
                graph = StepGraph(
                    runId=run_id,
                    steps=[
                        Step(
                            stepId=sid1,
                            mode=StepMode.NAVIGATION,
                            action="navigate",
                            metadata={"url": url},
                        ),
                        Step(
                            stepId=sid2,
                            mode=StepMode.ASSERTION,
                            action="assert_visible",
                            target=LocatorBundle(
                                primarySelector="[data-testid='dashboard-title']",
                                fallbackSelectors=[],
                                confidenceScore=0.9,
                            ),
                        ),
                    ],
                )
                await _run_graph_harness(graph, sqlite_path=db, runs_root=runs_root)
                repo = EventRepository(sqlite_path=db)
                events = await repo.load_for_run(run_id, limit=5000)
                c = _event_counts(events)
                smoke.check(c[EventType.STEP_SUCCEEDED.value] == 2, f"Expected 2 step_succeeded, got {dict(c)}")
                smoke.check(c[EventType.RUN_COMPLETED.value] == 1, f"Expected 1 run_completed, got {dict(c)}")

    with smoke.case("a6_2_pause_resume_no_replay", task="A6.2", feature="runner_pause"):
        with tempfile.TemporaryDirectory(prefix="a6-smoke-") as tmp:
            root = Path(tmp)
            db = root / "t.sqlite"
            runs_root = root / "runs"
            run_id = generate_run_id()
            sid1, sid2 = "a6_pause_s01", "a6_pause_s02"
            marker = get_run_layout(run_id, runs_root=runs_root).run_dir / ".pause"
            marker.unlink(missing_ok=True)
            with running_server() as fx:
                url = f"{fx.base_url}/dashboard.html"
                graph = StepGraph(
                    runId=run_id,
                    steps=[
                        Step(
                            stepId=sid1,
                            mode=StepMode.NAVIGATION,
                            action="navigate",
                            metadata={"url": url},
                        ),
                        Step(
                            stepId=sid2,
                            mode=StepMode.ASSERTION,
                            action="assert_visible",
                            target=LocatorBundle(
                                primarySelector="[data-testid='dashboard-title']",
                                fallbackSelectors=[],
                                confidenceScore=0.9,
                            ),
                        ),
                    ],
                )
                writer = CheckpointWriter.for_run(
                    run_id=run_id,
                    sqlite_path=db,
                    runs_root=runs_root,
                )
                pause_sink = _PauseAfterFirstSuccessSink(writer, marker)
                await _run_graph_harness(
                    graph,
                    sqlite_path=db,
                    runs_root=runs_root,
                    event_sink=pause_sink,
                    pause_requested=lambda: marker.exists(),
                )
                repo = EventRepository(sqlite_path=db)
                mid_events = await repo.load_for_run(run_id, limit=5000)
                mid_started_s1 = sum(
                    1
                    for e in mid_events
                    if e.type == EventType.STEP_STARTED and e.step_id == sid1
                )
                smoke.check(mid_started_s1 == 1, f"Expected single step_started for {sid1}, got {mid_started_s1}")
                ck_repo = CheckpointRepository(sqlite_path=db)
                cp = await ck_repo.load_latest(run_id)
                smoke.check(cp is not None, "Expected checkpoint after pause")
                assert cp is not None
                smoke.check(cp.current_step_id == sid2, f"Checkpoint must resume at {sid2}, got {cp.current_step_id}")

                await _run_graph_harness(
                    graph,
                    sqlite_path=db,
                    runs_root=runs_root,
                    start_step_id=sid2,
                    resume_warmup_url=url,
                )
                final = await repo.load_for_run(run_id, limit=5000)
                c = _event_counts(final)
                smoke.check(c[EventType.RUN_COMPLETED.value] == 1, f"Expected run_completed, got {dict(c)}")
                s1_starts = sum(
                    1 for e in final if e.type == EventType.STEP_STARTED and e.step_id == sid1
                )
                smoke.check(s1_starts == 1, f"Resume must not re-execute step 1; step_started({sid1})={s1_starts}")

    with smoke.case("a6_3_retry_delayed_visible", task="A6.3", feature="runner_retry"):
        with tempfile.TemporaryDirectory(prefix="a6-smoke-") as tmp:
            root = Path(tmp)
            db = root / "t.sqlite"
            runs_root = root / "runs"
            run_id = generate_run_id()
            sid1, sid2 = "a6_retry_s01", "a6_retry_s02"
            with running_server() as fx:
                url = f"{fx.base_url}/delayed_visible.html"
                graph = StepGraph(
                    runId=run_id,
                    steps=[
                        Step(
                            stepId=sid1,
                            mode=StepMode.NAVIGATION,
                            action="navigate",
                            metadata={"url": url},
                        ),
                        Step(
                            stepId=sid2,
                            mode=StepMode.ASSERTION,
                            action="assert_visible",
                            target=LocatorBundle(
                                primarySelector="[data-testid='delayed-box']",
                                fallbackSelectors=[],
                                confidenceScore=0.9,
                            ),
                            timeoutPolicy=TimeoutPolicy(timeoutMs=400),
                            recoveryPolicy=RecoveryPolicy(
                                maxRetries=1,
                                retryBackoffMs=900,
                            ),
                        ),
                    ],
                )
                await _run_graph_harness(graph, sqlite_path=db, runs_root=runs_root)
                repo = EventRepository(sqlite_path=db)
                events = await repo.load_for_run(run_id, limit=5000)
                retries = [e for e in events if e.type == EventType.STEP_RETRIED and e.step_id == sid2]
                smoke.check(len(retries) == 1, f"Expected exactly one step_retried for {sid2}, got {len(retries)}")
                smoke.check(
                    _event_counts(events)[EventType.STEP_SUCCEEDED.value] == 2,
                    "Expected two successful steps after retry",
                )

    with smoke.case("a6_4_manual_fix_then_resume", task="A6.4", feature="fix_cmd"):
        with tempfile.TemporaryDirectory(prefix="a6-smoke-") as tmp:
            root = Path(tmp)
            db = root / "t.sqlite"
            runs_root = root / "runs"
            run_id = generate_run_id()
            sid1, sid2 = "a6_fix_s01", "a6_fix_s02"
            with running_server() as fx:
                url = f"{fx.base_url}/dashboard.html"
                step_graph_repo = StepGraphRepository(sqlite_path=db)
                graph = StepGraph(
                    runId=run_id,
                    steps=[
                        Step(
                            stepId=sid1,
                            mode=StepMode.NAVIGATION,
                            action="navigate",
                            metadata={"url": url},
                        ),
                        Step(
                            stepId=sid2,
                            mode=StepMode.ASSERTION,
                            action="assert_visible",
                            target=LocatorBundle(
                                primarySelector="#bogus-selector-not-on-page",
                                fallbackSelectors=[],
                                confidenceScore=0.5,
                            ),
                        ),
                    ],
                )
                try:
                    await _run_graph_harness(graph, sqlite_path=db, runs_root=runs_root)
                except Exception:
                    pass
                else:
                    raise AssertionError("Expected failed run for broken selector")

                repo = EventRepository(sqlite_path=db)
                events = await repo.load_for_run(run_id, limit=5000)
                smoke.check(
                    any(e.type == EventType.STEP_FAILED for e in events),
                    "Expected a step_failed event",
                )

                await _apply_fix_and_resume(
                    run_id=run_id,
                    step_id=sid2,
                    fix_type="manual-fix",
                    selector="[data-testid='dashboard-title']",
                    sqlite_path=db,
                )
                after_fix = await repo.load_for_run(run_id, limit=5000)
                smoke.check(
                    any(e.type == EventType.INTERVENTION_RECORDED for e in after_fix),
                    "Expected intervention_recorded after fix",
                )

                graph_fixed = await step_graph_repo.load(run_id)
                smoke.check(graph_fixed is not None, "Step graph must load after manual fix")
                assert graph_fixed is not None
                await _run_graph_harness(
                    graph_fixed,
                    sqlite_path=db,
                    runs_root=runs_root,
                    start_step_id=sid2,
                    resume_warmup_url=url,
                )
                final = await repo.load_for_run(run_id, limit=5000)
                smoke.check(
                    any(e.type == EventType.RUN_RESUMED for e in final),
                    "Expected run_resumed on resume leg",
                )
                smoke.check(
                    _event_counts(final)[EventType.RUN_COMPLETED.value] >= 1,
                    "Expected run_completed after successful resume",
                )

    with smoke.case("a6_5_sigkill_resume_jsonl", task="A6.5", feature="durability"):
        with tempfile.TemporaryDirectory(prefix="a6-smoke-") as tmp:
            root = Path(tmp)
            db = root / "t.sqlite"
            runs_root = root / "runs"
            run_id = generate_run_id()
            sid1, sid2 = "a6_kill_s01", "a6_kill_s02"
            with running_server() as fx:
                url = f"{fx.base_url}/delayed_visible.html"
                graph = StepGraph(
                    runId=run_id,
                    steps=[
                        Step(
                            stepId=sid1,
                            mode=StepMode.NAVIGATION,
                            action="navigate",
                            metadata={"url": "http://fixture.test/delayed_visible.html"},
                        ),
                        Step(
                            stepId=sid2,
                            mode=StepMode.ASSERTION,
                            action="assert_visible",
                            target=LocatorBundle(
                                primarySelector="[data-testid='delayed-box']",
                                fallbackSelectors=[],
                                confidenceScore=0.9,
                            ),
                            timeoutPolicy=TimeoutPolicy(timeoutMs=60_000),
                        ),
                    ],
                )
                step_graph_repo = StepGraphRepository(sqlite_path=db)
                await step_graph_repo.save(graph)

                cfg_path = root / "sigkill.json"
                cfg_path.write_text(
                    json.dumps(
                        {
                            "sqlite_path": str(db),
                            "runs_root": str(runs_root),
                            "base_url": fx.base_url,
                            "graph": graph.model_dump(mode="json", by_alias=True),
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--a6-sigkill-child",
                        "--config",
                        str(cfg_path),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                ck_repo = CheckpointRepository(sqlite_path=db)
                killed = False
                for _ in range(500):
                    cp = await ck_repo.load_latest(run_id)
                    if cp is not None and cp.current_step_id == sid2:
                        await asyncio.sleep(0.45)
                        proc.send_signal(signal.SIGKILL)
                        killed = True
                        break
                    await asyncio.sleep(0.1)
                proc.wait(timeout=30)
                smoke.check(killed, "Timed out waiting for pre-step-2 checkpoint (SIGKILL child)")

                cp2 = await ck_repo.load_latest(run_id)
                smoke.check(cp2 is not None and cp2.current_step_id == sid2, "Latest checkpoint must be step 2")

                events_path = get_run_layout(run_id, runs_root=runs_root).events_jsonl
                rows = _parse_events_jsonl(events_path)
                smoke.check(bool(rows), "events.jsonl must remain parseable with at least one row")

                graph2 = await step_graph_repo.load(run_id)
                smoke.check(graph2 is not None, "Step graph must load after SIGKILL")
                assert graph2 is not None
                resume_u = f"{fx.base_url}/delayed_visible.html"
                await _run_graph_harness(
                    graph2,
                    sqlite_path=db,
                    runs_root=runs_root,
                    start_step_id=sid2,
                    resume_warmup_url=resume_u,
                )
                repo = EventRepository(sqlite_path=db)
                final = await repo.load_for_run(run_id, limit=5000)
                smoke.check(
                    _event_counts(final)[EventType.RUN_COMPLETED.value] >= 1,
                    "Resume after SIGKILL must reach run_completed",
                )

    return smoke.finalize()


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--a6-sigkill-child":
        _sigkill_child_sync()
    else:
        raise SystemExit(asyncio.run(main()))

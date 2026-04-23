from __future__ import annotations

import asyncio
import sys
import tempfile
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(SCRIPT_DIR))

from agent.cache.engine import CacheEngine  # noqa: E402
from agent.cache.models import CacheDecision  # noqa: E402
from agent.core.config import Settings  # noqa: E402
from agent.core.ids import generate_run_id  # noqa: E402
from agent.execution.browser import BrowserSession  # noqa: E402
from agent.execution.checkpoint_writer import CheckpointWriter, RunnerEventSink  # noqa: E402
from agent.execution.events import EventType  # noqa: E402
from agent.execution.runner import StepGraphRunner  # noqa: E402
from agent.execution.snapshot import SnapshotEngine  # noqa: E402
from agent.execution.tools import ToolRuntime  # noqa: E402
from agent.policy.approval import ApprovalClassifier, HardApprovalRequest  # noqa: E402
from agent.policy.audit import AuditLogger  # noqa: E402
from agent.policy.restrictions import RestrictionsPolicy  # noqa: E402
from agent.stepgraph.models import (  # noqa: E402
    LocatorBundle,
    RecoveryAction,
    RecoveryPolicy,
    Step,
    StepGraph,
    StepMode,
)
from agent.storage.repos.cache import CacheRepository  # noqa: E402
from agent.storage.repos.events import EventRepository  # noqa: E402
from agent.storage.repos.step_graph import StepGraphRepository  # noqa: E402
from fixtures import running_server  # noqa: E402
from phase_a6 import _resolve_tab_id, _run_graph_harness  # noqa: E402
from _runner import SmokeRunner  # noqa: E402


def _http_get(url: str) -> None:
    with urllib.request.urlopen(url, timeout=5.0) as response:  # noqa: S310
        response.read()


async def _latest_decisions(
    cache_repo: CacheRepository,
    run_id: str,
    step_ids: list[str],
) -> dict[str, CacheDecision]:
    out: dict[str, CacheDecision] = {}
    for sid in step_ids:
        rows = await cache_repo.load_for_run(run_id, step_id=sid, limit=1)
        if not rows:
            raise AssertionError(f"No cache row for step {sid!r}")
        out[sid] = rows[0].decision
    return out


async def _all_cache_rows_for_step(
    cache_repo: CacheRepository,
    run_id: str,
    step_id: str,
    *,
    limit: int = 30,
) -> list:
    return await cache_repo.load_for_run(run_id, step_id=step_id, limit=limit)


async def _full_run_then_rerun_assertion_tail(
    graph: StepGraph,
    *,
    sqlite_path: Path,
    runs_root: Path,
    between_full_and_tail: Callable[[], Awaitable[None]] | None = None,
    tail_from_step_index: int = 1,
    tail_step_ids: list[str] | None = None,
) -> None:
    """
    Run the full graph once, optionally await a fixture hook, then re-execute assertion
    steps in the **same** browser session so `frame_hash` stays stable across cache compares.
    """
    run_id = graph.run_id
    settings = Settings.load(overrides={"storage": {"sqlite_path": str(sqlite_path)}})
    await StepGraphRepository(sqlite_path=sqlite_path).save(graph)

    writer = CheckpointWriter.for_run(run_id=run_id, sqlite_path=sqlite_path, runs_root=runs_root)
    sink = RunnerEventSink(writer)
    audit_logger = AuditLogger.for_run(run_id=run_id, runs_root=runs_root)
    restrictions_policy = RestrictionsPolicy.from_settings(settings.policy)

    session = BrowserSession(headless=True)
    await session.start()
    try:
        _, context = await session.new_context()
        page = await context.new_page()
        tab_id = await _resolve_tab_id(session, page)
        for step in graph.steps:
            step.metadata["tabId"] = tab_id

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

        def _hard_ok(_request: HardApprovalRequest) -> bool:
            return True

        runner = StepGraphRunner(
            runtime,
            event_sink=sink,
            cache_engine=cache_engine,
            snapshot_engine=snapshot_engine,
            approval_classifier=ApprovalClassifier(),
            hard_approval_resolver=_hard_ok,
            restrictions_policy=restrictions_policy,
            audit_logger=audit_logger,
        )

        await runner.run(graph)
        await asyncio.sleep(0.35)
        if between_full_and_tail is not None:
            await between_full_and_tail()
        if tail_step_ids is not None:
            by_id = {s.step_id: s for s in graph.steps}
            tail_steps = [by_id[sid] for sid in tail_step_ids]
        else:
            tail_steps = graph.steps[tail_from_step_index:]
        for step in tail_steps:
            await runner._run_step(graph, step)
    finally:
        await session.stop()


def _dashboard_graph(
    run_id: str,
    base_url: str,
    *,
    sid_nav: str,
    sid_title: str,
    sid_orders: str,
) -> StepGraph:
    # Align with fixture_state.js (query param) so the first cache fingerprint matches
    # post-poll URL shape and tail replays get `reuse` instead of spurious `route_changed`.
    dash = f"{base_url}/dashboard.html?fixtureRv=1"
    return StepGraph(
        runId=run_id,
        steps=[
            Step(
                stepId=sid_nav,
                mode=StepMode.NAVIGATION,
                action="navigate",
                metadata={"url": dash},
            ),
            Step(
                stepId=sid_title,
                mode=StepMode.ASSERTION,
                action="assert_visible",
                target=LocatorBundle(
                    primarySelector="[data-testid='dashboard-title']",
                    fallbackSelectors=[],
                    confidenceScore=0.95,
                ),
            ),
            Step(
                stepId=sid_orders,
                mode=StepMode.ASSERTION,
                action="assert_visible",
                target=LocatorBundle(
                    primarySelector="#orders-list",
                    fallbackSelectors=[],
                    confidenceScore=0.9,
                ),
            ),
        ],
    )


async def main() -> int:
    smoke = SmokeRunner(phase="A7", default_task="A7.1")

    with smoke.case("a7_1_second_run_mostly_reuse", task="A7.1", feature="cache_reuse"):
        with tempfile.TemporaryDirectory(prefix="a7-smoke-") as tmp:
            root = Path(tmp)
            db = root / "t.sqlite"
            runs_root = root / "runs"
            run_id = generate_run_id()
            s_nav, s_title, s_orders = "a7_r1_nav", "a7_r1_title", "a7_r1_orders"
            with running_server() as fx:
                graph = _dashboard_graph(run_id, fx.base_url, sid_nav=s_nav, sid_title=s_title, sid_orders=s_orders)
                await _full_run_then_rerun_assertion_tail(graph, sqlite_path=db, runs_root=runs_root)

            cache_repo = CacheRepository(sqlite_path=db)
            latest = await _latest_decisions(cache_repo, run_id, [s_title, s_orders])
            smoke.check(
                latest[s_title] == CacheDecision.REUSE and latest[s_orders] == CacheDecision.REUSE,
                f"Expected assertion steps to reuse cache on same-session replay, got {latest}",
            )

    with smoke.case("a7_2_region_mutation_partial_only", task="A7.2", feature="cache_invalidation"):
        with tempfile.TemporaryDirectory(prefix="a7-smoke-") as tmp:
            root = Path(tmp)
            db = root / "t.sqlite"
            runs_root = root / "runs"
            run_id = generate_run_id()
            s_nav, s_title, s_orders = "a7_r2_nav", "a7_r2_title", "a7_r2_orders"
            with running_server() as fx:
                graph = _dashboard_graph(run_id, fx.base_url, sid_nav=s_nav, sid_title=s_title, sid_orders=s_orders)

                async def _mutate_region() -> None:
                    _http_get(f"{fx.base_url}/mutate/region")
                    await asyncio.sleep(0.45)

                await _full_run_then_rerun_assertion_tail(
                    graph,
                    sqlite_path=db,
                    runs_root=runs_root,
                    between_full_and_tail=_mutate_region,
                )

            cache_repo = CacheRepository(sqlite_path=db)
            rows_title = await cache_repo.load_for_run(run_id, step_id=s_title, limit=1)
            rows_orders = await cache_repo.load_for_run(run_id, step_id=s_orders, limit=1)
            smoke.check(rows_title and rows_title[0].decision == CacheDecision.REUSE, "Title scope should reuse cache")
            smoke.check(
                rows_orders and rows_orders[0].decision == CacheDecision.PARTIAL_REFRESH,
                "Orders scope should partial_refresh after region mutation",
            )
            reasons = rows_orders[0].decision_reasons if rows_orders else []
            smoke.check(
                "dom_mutation_in_target_scope" in reasons,
                f"Expected dom_mutation_in_target_scope in reasons, got {reasons!r}",
            )

    with smoke.case("a7_3_route_mutation_full_refresh", task="A7.3", feature="cache_invalidation"):
        with tempfile.TemporaryDirectory(prefix="a7-smoke-") as tmp:
            root = Path(tmp)
            db = root / "t.sqlite"
            runs_root = root / "runs"
            run_id = generate_run_id()
            s_nav, s_title, s_orders = "a7_r3_nav", "a7_r3_title", "a7_r3_orders"
            with running_server() as fx:
                graph = _dashboard_graph(run_id, fx.base_url, sid_nav=s_nav, sid_title=s_title, sid_orders=s_orders)

                async def _mutate_route() -> None:
                    _http_get(f"{fx.base_url}/mutate/route")
                    await asyncio.sleep(1.0)

                await _full_run_then_rerun_assertion_tail(
                    graph,
                    sqlite_path=db,
                    runs_root=runs_root,
                    between_full_and_tail=_mutate_route,
                )

            cache_repo = CacheRepository(sqlite_path=db)
            for sid in (s_title, s_orders):
                rows = await cache_repo.load_for_run(run_id, step_id=sid, limit=1)
                smoke.check(rows, f"Missing cache row for {sid}")
                assert rows
                smoke.check(
                    rows[0].decision == CacheDecision.FULL_REFRESH,
                    f"Step {sid} should full_refresh after route mutation, got {rows[0].decision!r}",
                )
                smoke.check(
                    "route_changed" in rows[0].decision_reasons,
                    f"Expected route_changed in {rows[0].decision_reasons!r}",
                )

    with smoke.case("a7_4_stale_ref_cache_invalidation", task="A7.4", feature="stale_ref"):
        with tempfile.TemporaryDirectory(prefix="a7-smoke-") as tmp:
            root = Path(tmp)
            db = root / "t.sqlite"
            runs_root = root / "runs"
            run_id = generate_run_id()
            s_nav = "a7_r4_nav"
            s_click = "a7_r4_click"
            with running_server() as fx:
                dash = f"{fx.base_url}/dashboard.html"
                g1 = StepGraph(
                    runId=run_id,
                    steps=[
                        Step(
                            stepId=s_nav,
                            mode=StepMode.NAVIGATION,
                            action="navigate",
                            metadata={"url": dash},
                        ),
                    ],
                )
                await _run_graph_harness(g1, sqlite_path=db, runs_root=runs_root)

                _http_get(f"{fx.base_url}/mutate/stale-ref")
                await asyncio.sleep(0.45)

                g2 = StepGraph(
                    runId=run_id,
                    steps=[
                        Step(
                            stepId=s_nav,
                            mode=StepMode.NAVIGATION,
                            action="navigate",
                            metadata={"url": dash},
                        ),
                        Step(
                            stepId=s_click,
                            mode=StepMode.ACTION,
                            action="click",
                            target=LocatorBundle(
                                primarySelector="[data-testid='primary-action']",
                                fallbackSelectors=[],
                                confidenceScore=0.9,
                            ),
                            recovery_policy=RecoveryPolicy(
                                maxRetries=1,
                                retryBackoffMs=50,
                                allowedActions=[RecoveryAction.RETRY],
                            ),
                        ),
                    ],
                )
                try:
                    await _run_graph_harness(g2, sqlite_path=db, runs_root=runs_root)
                except Exception:
                    pass
                else:
                    raise AssertionError("Expected click to fail after stale-ref mutation")

            cache_repo = CacheRepository(sqlite_path=db)
            rows = await _all_cache_rows_for_step(cache_repo, run_id, s_click, limit=20)
            smoke.check(
                any("stale_ref_locator_mismatch" in r.decision_reasons for r in rows),
                f"Expected stale_ref_locator_mismatch in one of {[(r.decision, r.decision_reasons) for r in rows]}",
            )

            event_repo = EventRepository(sqlite_path=db)
            events = await event_repo.load_for_run(run_id, limit=5000)
            smoke.check(
                any(e.type == EventType.STEP_RETRIED and e.step_id == s_click for e in events),
                "Expected step_retried on stale-ref click step",
            )

    return smoke.finalize()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

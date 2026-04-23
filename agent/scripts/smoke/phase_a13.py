from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
PLAYWRIGHT_CLI = WORKSPACE_ROOT / "playwright-cli"
FIXTURE_GRAPH = PROJECT_ROOT / "scripts" / "fixtures" / "graphs" / "fixture_login_and_navigate.json"

sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(SCRIPT_DIR))

from agent.cache.models import CacheDecision  # noqa: E402
from agent.cli.fix_cmd import _apply_fix_and_resume  # noqa: E402
from agent.core.ids import generate_run_id, generate_step_id  # noqa: E402
from agent.core.mode import ModeController, RuntimeBinding, RuntimeMode  # noqa: E402
from agent.execution.events import EventType  # noqa: E402
from agent.export.manifest import PortableManifest, PortableManifestWriter  # noqa: E402
from agent.export.spec_writer import PlaywrightSpecWriter  # noqa: E402
from agent.memory.models import RepairLifecycleState  # noqa: E402
from agent.memory.repairs import LearnedRepairStore  # noqa: E402
from agent.stepgraph.models import Step, StepEdge, StepGraph, StepMode  # noqa: E402
from agent.storage.repos.cache import CacheRepository  # noqa: E402
from agent.storage.repos.events import EventRepository  # noqa: E402
from agent.storage.repos.step_graph import StepGraphRepository  # noqa: E402
from agent.telemetry.report import BENCHMARK_KPI_FIELD_SPECS, RunReportBuilder  # noqa: E402
from fixtures import running_server  # noqa: E402
from phase_a6 import _event_counts, _run_graph_harness  # noqa: E402
from phase_a7 import _full_run_then_rerun_assertion_tail  # noqa: E402
from _runner import SmokeRunner  # noqa: E402

CLICK_STEP_ID = "01A0STEPLOGINNAVIGATE0000004"
DASHBOARD_STEP_ID = "01A0STEPLOGINNAVIGATE0000005"
GOOD_TITLE_SELECTOR = "[data-testid='dashboard-title']"


def _load_login_graph(run_id: str, base_url: str) -> StepGraph:
    raw = json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))
    raw["runId"] = run_id
    graph = StepGraph.model_validate(raw)
    steps = []
    for step in graph.steps:
        meta = dict(step.metadata)
        for key in ("url", "frameUrl"):
            val = meta.get(key)
            if isinstance(val, str):
                meta[key] = re.sub(
                    r"https?://127\.0\.0\.1:\d+",
                    base_url.rstrip("/"),
                    val,
                )
        steps.append(step.model_copy(update={"metadata": meta}))
    graph = graph.model_copy(update={"steps": steps})

    # Match A7: explicit dashboard URL with fixtureRv so fixture_state polling does not
    # change route/modal fingerprints between the full run and same-session tail replay.
    dash_idx = next(i for i, s in enumerate(graph.steps) if s.step_id == DASHBOARD_STEP_ID)
    nav_id = generate_step_id()
    nav_step = Step(
        step_id=nav_id,
        mode=StepMode.NAVIGATION,
        action="navigate",
        metadata={"url": f"{base_url.rstrip('/')}/dashboard.html?fixtureRv=1"},
    )
    new_steps = list(graph.steps[:dash_idx]) + [nav_step] + list(graph.steps[dash_idx:])
    new_edges: list[StepEdge] = []
    for e in graph.edges:
        if e.from_step_id == CLICK_STEP_ID and e.to_step_id == DASHBOARD_STEP_ID:
            new_edges.append(
                StepEdge(from_step_id=CLICK_STEP_ID, to_step_id=nav_id),
            )
            new_edges.append(StepEdge(from_step_id=nav_id, to_step_id=DASHBOARD_STEP_ID))
        else:
            new_edges.append(e)
    return graph.model_copy(update={"steps": new_steps, "edges": new_edges})


def _ensure_playwright_cli_deps(smoke: SmokeRunner) -> Path:
    marker = PLAYWRIGHT_CLI / "node_modules" / "@playwright" / "test"
    if not marker.is_dir():
        proc = subprocess.run(
            ["npm", "ci"],
            cwd=str(PLAYWRIGHT_CLI),
            capture_output=True,
            text=True,
            check=False,
        )
        smoke.check(proc.returncode == 0, f"npm ci failed in playwright-cli: {proc.stderr or proc.stdout}")
        smoke.check(marker.is_dir(), "npm ci did not install @playwright/test")
    return PLAYWRIGHT_CLI


def _run_playwright_test_once(cli_root: Path, spec_rel: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["npx", "playwright", "test", spec_rel, "--reporter=list"],
        cwd=str(cli_root),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CI": "1"},
    )


async def main() -> int:
    smoke = SmokeRunner(phase="A13", default_task="A13.1")
    kpi_keys = [spec[1] for spec in BENCHMARK_KPI_FIELD_SPECS]

    with smoke.case(
        "a13_integration_fixture_chain",
        task="A13.1",
        feature="integration_chain",
    ):
        with tempfile.TemporaryDirectory(prefix="a13-smoke-") as tmpd:
            root = Path(tmpd).resolve()
            db = root / "chain.sqlite"
            runs_root = root / "runs"
            run_id = generate_run_id()

            with running_server(port=0) as server:
                dash_url = f"{server.base_url}/dashboard.html"
                graph = _load_login_graph(run_id, server.base_url)
                login_url = f"{server.base_url}/login.html"

                await _full_run_then_rerun_assertion_tail(
                    graph,
                    sqlite_path=db,
                    runs_root=runs_root,
                    tail_step_ids=[DASHBOARD_STEP_ID],
                )

                cache_repo = CacheRepository(sqlite_path=db)
                dash_rows = await cache_repo.load_for_run(run_id, step_id=DASHBOARD_STEP_ID, limit=1)
                smoke.check(
                    dash_rows and dash_rows[0].decision == CacheDecision.REUSE,
                    f"Expected latest cache decision reuse for dashboard step, got {dash_rows!r}",
                )

                events_repo = EventRepository(sqlite_path=db)
                mid_events = await events_repo.load_for_run(run_id, limit=20_000)
                mid_counts = _event_counts(mid_events)
                smoke.check(
                    mid_counts[EventType.RUN_COMPLETED.value] >= 1,
                    f"Expected run_completed after full graph, got {dict(mid_counts)}",
                )
                smoke.check(
                    mid_counts[EventType.STEP_SUCCEEDED.value] >= 7,
                    "Expected >= 7 step_succeeded (6 full steps + dashboard tail replay)",
                )

                step_graph_repo = StepGraphRepository(sqlite_path=db)
                broken_graph = await step_graph_repo.load(run_id)
                smoke.check(broken_graph is not None, "Graph must load before break")
                assert broken_graph is not None
                dash_step = next(s for s in broken_graph.steps if s.step_id == DASHBOARD_STEP_ID)
                smoke.check(dash_step.target is not None, "Dashboard step needs target")
                assert dash_step.target is not None
                dash_step.target.primary_selector = "#bogus-a13-not-on-page"
                dash_step.target.fallback_selectors = []
                await step_graph_repo.save(broken_graph)

                try:
                    await _run_graph_harness(broken_graph, sqlite_path=db, runs_root=runs_root)
                except Exception:
                    pass
                else:
                    raise AssertionError("Expected failed run after breaking dashboard selector")

                fail_events = await events_repo.load_for_run(run_id, limit=20_000)
                smoke.check(
                    any(e.type == EventType.STEP_FAILED for e in fail_events),
                    "Expected step_failed after broken replay",
                )

                await _apply_fix_and_resume(
                    run_id=run_id,
                    step_id=DASHBOARD_STEP_ID,
                    fix_type="manual-fix",
                    selector=GOOD_TITLE_SELECTOR,
                    sqlite_path=db,
                    runs_root=runs_root,
                )

                post_fix_events = await events_repo.load_for_run(run_id, limit=20_000)
                smoke.check(
                    any(e.type == EventType.INTERVENTION_RECORDED for e in post_fix_events),
                    "Expected intervention_recorded after manual fix",
                )

                graph_fixed = await step_graph_repo.load(run_id)
                smoke.check(graph_fixed is not None, "Step graph must load after fix")
                assert graph_fixed is not None

                await ModeController(initial_mode=RuntimeMode.MANUAL).switch_mode(
                    target_mode=RuntimeMode.LLM,
                    reason="a13_fixture_integration_llm_mode_smoke",
                    actor="smoke_a13",
                    binding=RuntimeBinding(
                        run_id=run_id,
                        current_step_id=DASHBOARD_STEP_ID,
                        browser_session_id="bs_a13",
                        tab_id="tab_a13",
                    ),
                    sqlite_path=db,
                    runs_root=runs_root,
                )

                mode_events = await events_repo.load_for_run(run_id, limit=20_000)
                smoke.check(
                    any(e.type == EventType.MODE_SWITCHED for e in mode_events),
                    "Expected mode_switched after ModeController.switch_mode",
                )

                await _run_graph_harness(
                    graph_fixed,
                    sqlite_path=db,
                    runs_root=runs_root,
                    start_step_id=DASHBOARD_STEP_ID,
                    resume_warmup_url=dash_url,
                )

                final_events = await events_repo.load_for_run(run_id, limit=20_000)
                final_counts = _event_counts(final_events)
                smoke.check(
                    final_counts[EventType.RUN_COMPLETED.value] >= 2,
                    f"Expected >=2 run_completed (happy path + resume), got {dict(final_counts)}",
                )

                store = LearnedRepairStore.create(sqlite_path=db)
                await store.record_manual_fix_candidate(
                    domain="fixture.a13",
                    normalized_route_template="/dashboard.html",
                    frame_context=[],
                    target_semantic_key="dashboard_title",
                    source_run_id=run_id,
                    source_step_id=DASHBOARD_STEP_ID,
                    actor="smoke_a13",
                    confidence_score=0.9,
                )
                learned = await store.list(source_run_id=run_id, source_step_id=DASHBOARD_STEP_ID, limit=10)
                smoke.check(len(learned) >= 1, "Expected learned repair candidate row")
                smoke.check(
                    learned[0].state == RepairLifecycleState.CANDIDATE,
                    f"Expected candidate repair, got {learned[0].state!r}",
                )

                manifest_writer = PortableManifestWriter.create(sqlite_path=db, runs_root=runs_root)
                manifest_result = await manifest_writer.write_manifest(run_id=run_id)
                manifest_text = Path(manifest_result.manifest_path).read_text(encoding="utf-8")
                smoke.check("fixture-password" not in manifest_text, "Password must not leak into manifest")
                PortableManifest.model_validate(json.loads(manifest_text))

                report = await RunReportBuilder.create(sqlite_path=db, runs_root=runs_root).build_report(
                    run_id=run_id,
                )
                for key in kpi_keys:
                    smoke.check(key in report.kpis, f"Missing KPI key {key!r} (report contract)")
                smoke.check(
                    report.breakdowns.get("cache_by_mode") is not None,
                    "Expected cache_by_mode breakdown",
                )

                if os.environ.get("SKIP_PLAYWRIGHT_A13", "").strip().lower() in {"1", "true", "yes"}:
                    print("SKIP_PLAYWRIGHT_A13 set — skipping npx playwright test")
                else:
                    cli_root = _ensure_playwright_cli_deps(smoke)
                    spec_path = root / f"{run_id}.spec.ts"
                    await PlaywrightSpecWriter.create(sqlite_path=db, runs_root=runs_root).write_spec(
                        run_id=run_id,
                        output_path=spec_path,
                        test_name="A13 integration export",
                        target_url=login_url,
                    )

                    tests_dir = cli_root / "tests"
                    tests_dir.mkdir(parents=True, exist_ok=True)
                    borrowed = tests_dir / "_a13_integration_export.spec.ts"
                    shutil.copyfile(spec_path, borrowed)
                    try:
                        proc = _run_playwright_test_once(cli_root, "tests/_a13_integration_export.spec.ts")
                        out = f"{proc.stderr or ''}\n{proc.stdout or ''}"
                        if proc.returncode != 0 and (
                            "Executable doesn't exist" in out or "npx playwright install" in out
                        ):
                            inst = subprocess.run(
                                ["npx", "playwright", "install", "chromium"],
                                cwd=str(cli_root),
                                capture_output=True,
                                text=True,
                                check=False,
                            )
                            smoke.check(
                                inst.returncode == 0,
                                f"playwright install chromium failed:\n{inst.stderr}\n{inst.stdout}",
                            )
                            proc = _run_playwright_test_once(cli_root, "tests/_a13_integration_export.spec.ts")
                        smoke.check(
                            proc.returncode == 0,
                            f"playwright test failed:\n{proc.stdout}\n{proc.stderr}",
                        )
                    finally:
                        borrowed.unlink(missing_ok=True)

    return smoke.finalize()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

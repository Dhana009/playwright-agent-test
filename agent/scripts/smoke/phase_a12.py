from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(SCRIPT_DIR))

from agent.cache.models import CacheDecision, CacheRecord, ContextFingerprint  # noqa: E402
from agent.cli.bench_cmd import (  # noqa: E402
    BenchCaseResult,
    RuntimeMode,
    _build_cases,
    _mean_kpis,
)
from agent.core.ids import generate_run_id, generate_step_id  # noqa: E402
from agent.execution.events import (  # noqa: E402
    RunCompletedEvent,
    StepStartedEvent,
    StepSucceededEvent,
)
from agent.storage.repos._common import dumps_json, ensure_run, open_connection  # noqa: E402
from agent.storage.repos.cache import CacheRepository  # noqa: E402
from agent.storage.repos.events import EventRepository  # noqa: E402
from agent.storage.repos.telemetry import TelemetryRepository  # noqa: E402
from agent.stepgraph.models import Step, StepGraph, StepMode, TimeoutPolicy  # noqa: E402
from agent.telemetry.models import CallPurpose, ContextTier, LLMCall  # noqa: E402
from agent.telemetry.report import BENCHMARK_KPI_FIELD_SPECS, RunReportBuilder  # noqa: E402
from fixtures import running_server  # noqa: E402
from _runner import SmokeRunner  # noqa: E402


async def _seed_report_fixture_run(*, sqlite_path: Path, run_id: str) -> tuple[str, str]:
    """Insert a completed run with events, LLM calls, and one cache decision."""
    started = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    ended = datetime.now(UTC).isoformat()
    # Keep metadata free of partial `run_summary` blobs — `record_llm_call` merges into RunSummary.
    metadata: dict[str, object] = {}
    async with open_connection(sqlite_path) as conn:
        await ensure_run(connection=conn, run_id=run_id, started_at=started)
        await conn.execute(
            """
            UPDATE runs SET mode = ?, status = ?, ended_at = ?, metadata_json = ?
            WHERE run_id = ?;
            """,
            ("manual", "completed", ended, dumps_json(metadata), run_id),
        )
        await conn.commit()

    events = EventRepository(sqlite_path=sqlite_path)
    t0 = datetime.now(UTC)
    s1, s2 = generate_step_id(), generate_step_id()
    await events.save(
        StepStartedEvent(run_id=run_id, step_id=s1, actor="a12", ts=t0, payload={})
    )
    await events.save(
        StepSucceededEvent(
            run_id=run_id,
            step_id=s1,
            actor="a12",
            ts=t0 + timedelta(milliseconds=10),
            payload={"attempt": 0},
        )
    )
    await events.save(
        StepStartedEvent(
            run_id=run_id,
            step_id=s2,
            actor="a12",
            ts=t0 + timedelta(milliseconds=20),
            payload={},
        )
    )
    await events.save(
        StepSucceededEvent(
            run_id=run_id,
            step_id=s2,
            actor="a12",
            ts=t0 + timedelta(milliseconds=30),
            payload={"attempt": 0},
        )
    )
    await events.save(
        RunCompletedEvent(
            run_id=run_id,
            actor="a12",
            ts=t0 + timedelta(milliseconds=40),
            payload={},
        )
    )

    tel = TelemetryRepository(sqlite_path=sqlite_path)
    await tel.record_llm_call(
        LLMCall(
            runId=run_id,
            stepId=s1,
            provider="fixture",
            model="fixture-model",
            callPurpose=CallPurpose.PLAN,
            contextTier=ContextTier.TIER_0,
            escalationPath=[ContextTier.TIER_0],
            inputTokens=40,
            outputTokens=10,
            preflightInputTokens=0,
            preflightOutputTokens=0,
            cacheRead=0,
            cacheWrite=0,
            estCost=0.0,
            actualCost=0.01,
            latencyMs=12,
            noProgressRetry=False,
        )
    )
    await tel.record_llm_call(
        LLMCall(
            runId=run_id,
            stepId=s2,
            provider="fixture",
            model="fixture-model",
            callPurpose=CallPurpose.REPAIR,
            contextTier=ContextTier.TIER_2,
            escalationPath=[ContextTier.TIER_0, ContextTier.TIER_1, ContextTier.TIER_2],
            inputTokens=20,
            outputTokens=5,
            preflightInputTokens=0,
            preflightOutputTokens=0,
            cacheRead=0,
            cacheWrite=0,
            estCost=0.0,
            actualCost=0.005,
            latencyMs=8,
            noProgressRetry=True,
        )
    )

    fp = ContextFingerprint(
        routeTemplate="/x",
        domHash="h1",
        frameHash="f1",
        modalState="closed",
    )
    await CacheRepository(sqlite_path=sqlite_path).save(
        CacheRecord(
            runId=run_id,
            stepId=s1,
            fingerprint=fp,
            decision=CacheDecision.REUSE,
            decisionReasons=["stable"],
        )
    )
    await CacheRepository(sqlite_path=sqlite_path).save(
        CacheRecord(
            runId=run_id,
            stepId=s2,
            fingerprint=fp,
            decision=CacheDecision.PARTIAL_REFRESH,
            decisionReasons=["stale_ref"],
        )
    )
    return s1, s2


def _bench_case_stub(
    *,
    case_id: str,
    run_id: str,
    mode: RuntimeMode,
    kpis: dict[str, float | None],
) -> BenchCaseResult:
    return BenchCaseResult(
        caseId=case_id,
        runId=run_id,
        mode=mode,
        storageStateEnabled=False,
        learnedRepairsEnabled=False,
        status="completed",
        durationMs=1,
        reportPath=None,
        kpis=dict(kpis),
        counts={},
        error=None,
    )


async def main() -> int:
    smoke = SmokeRunner(phase="A12", default_task="A12.1")
    kpi_keys = [spec[1] for spec in BENCHMARK_KPI_FIELD_SPECS]

    with smoke.case("a12_1_kpi_keys_in_report", task="A12.1", feature="kpi_report"):
        with tempfile.TemporaryDirectory(prefix="a12-report-") as tmp:
            db = Path(tmp) / "r.sqlite"
            run_id = generate_run_id()
            await _seed_report_fixture_run(sqlite_path=db, run_id=run_id)
            report = await RunReportBuilder.create(sqlite_path=db).build_report(run_id=run_id)
            for key in kpi_keys:
                smoke.check(key in report.kpis, f"Missing KPI key {key!r} (docs/08 / report contract)")
            smoke.check(
                report.breakdowns.get("cache_by_mode") is not None,
                "Expected cache_by_mode breakdown per docs/08",
            )
            smoke.check(
                report.breakdowns.get("tier_resolution_by_purpose") is not None,
                "Expected tier_resolution_by_purpose breakdown per docs/08",
            )

    with smoke.case("a12_2_bench_case_matrix", task="A12.2", feature="bench_harness"):
        cases = _build_cases(
            modes=[RuntimeMode.MANUAL, RuntimeMode.LLM, RuntimeMode.HYBRID],
            storage_values=[False],
            repair_values=[False],
        )
        smoke.check(len(cases) == 3, f"Expected 3 mode-only cases, got {len(cases)}")
        modes_found = {c.mode for c in cases}
        smoke.check(
            modes_found == {RuntimeMode.MANUAL, RuntimeMode.LLM, RuntimeMode.HYBRID},
            modes_found,
        )

    with smoke.case("a12_2_mean_kpis_aggregation", task="A12.2", feature="bench_harness"):
        results = [
            _bench_case_stub(
                case_id="c1",
                run_id="run_1",
                mode=RuntimeMode.MANUAL,
                kpis={"flow_completion_rate": 1.0, "cache_hit_rate": 0.0},
            ),
            _bench_case_stub(
                case_id="c2",
                run_id="run_2",
                mode=RuntimeMode.LLM,
                kpis={"flow_completion_rate": 1.0, "cache_hit_rate": 0.5},
            ),
            _bench_case_stub(
                case_id="c3",
                run_id="run_3",
                mode=RuntimeMode.HYBRID,
                kpis={"flow_completion_rate": 1.0, "cache_hit_rate": 1.0},
            ),
        ]
        means = _mean_kpis(results)
        smoke.check(
            abs(means["flow_completion_rate"] - 1.0) < 1e-9,
            means["flow_completion_rate"],
        )
        smoke.check(
            abs(means["cache_hit_rate"] - statistics.mean([0.0, 0.5, 1.0])) < 1e-9,
            means["cache_hit_rate"],
        )

    with smoke.case("a12_2_fixture_bench_cli", task="A12.2", feature="bench_harness"):
        if os.environ.get("SKIP_BENCH_A12", "").strip().lower() in {"1", "true", "yes"}:
            print("SKIP_BENCH_A12 set — skipping browser bench matrix")
        else:
            with running_server(port=0) as server:
                tiny = StepGraph(
                    runId=generate_run_id(),
                    version="1.0",
                    steps=[
                        Step(
                            mode=StepMode.NAVIGATION,
                            action="navigate",
                            metadata={
                                "url": f"{server.base_url}/login.html",
                            },
                            timeout_policy=TimeoutPolicy(timeoutMs=30_000),
                        )
                    ],
                    edges=[],
                )
                with tempfile.TemporaryDirectory(prefix="a12-bench-") as btmp:
                    bpath = Path(btmp)
                    graph_path = bpath / "tiny_nav.json"
                    graph_path.write_text(
                        tiny.model_dump_json(indent=2, by_alias=True),
                        encoding="utf-8",
                    )
                    db_path = bpath / "bench.sqlite"
                    runs_root = bpath / "runs"
                    summary_path = bpath / "summary.json"
                    proc = subprocess.run(
                        [
                            "uv",
                            "run",
                            "python",
                            "-m",
                            "agent.cli",
                            "bench",
                            str(graph_path),
                            "--modes",
                            "manual,llm,hybrid",
                            "--storage-state-variant",
                            "without",
                            "--learned-repairs-variant",
                            "without",
                            "--sqlite-path",
                            str(db_path),
                            "--runs-root",
                            str(runs_root),
                            "--output-path",
                            str(summary_path),
                        ],
                        cwd=str(PROJECT_ROOT),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    smoke.check(proc.returncode == 0, f"bench CLI failed:\n{proc.stdout}\n{proc.stderr}")
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    smoke.check(summary.get("totalCases") == 3, summary.get("totalCases"))
                    smoke.check(summary.get("succeededCases") == 3, summary.get("succeededCases"))
                    mean_kpis = summary.get("meanKpis") or {}
                    smoke.check(
                        "flow_completion_rate" in mean_kpis,
                        f"meanKpis missing flow_completion_rate: {mean_kpis.keys()}",
                    )
                    for case in summary.get("cases", []):
                        smoke.check(case.get("status") == "completed", case)
                        mode = (case.get("mode") or "").lower()
                        smoke.check(mode in {"manual", "llm", "hybrid"}, mode)

    return smoke.finalize()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

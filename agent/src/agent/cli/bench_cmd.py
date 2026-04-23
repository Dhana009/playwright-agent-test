from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import typer
from pydantic import BaseModel, ConfigDict, Field
from rich.console import Console
from rich.table import Table

from agent.cache.engine import CacheEngine
from agent.core.config import Settings
from agent.core.ids import generate_run_id
from agent.core.mode import ModeController, RuntimeBinding, RuntimeMode
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
from agent.storage.repos._common import dumps_json, ensure_run, loads_json, open_connection
from agent.storage.repos.step_graph import StepGraphRepository
from agent.telemetry.report import RunBenchmarkReport, RunReportBuilder

app = typer.Typer(help="Benchmark harness for comparative run experiments.")
CONSOLE = Console()


class VariantSelection(str, Enum):
    BOTH = "both"
    WITH = "with"
    WITHOUT = "without"


class BenchCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)

    case_id: str = Field(alias="caseId")
    run_id: str = Field(alias="runId")
    mode: RuntimeMode
    storage_state_enabled: bool = Field(alias="storageStateEnabled")
    learned_repairs_enabled: bool = Field(alias="learnedRepairsEnabled")
    status: str
    duration_ms: int = Field(alias="durationMs")
    report_path: str | None = Field(default=None, alias="reportPath")
    kpis: dict[str, float | None] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


class BenchSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)

    bench_id: str = Field(alias="benchId")
    generated_at: str = Field(alias="generatedAt")
    stepgraph_path: str = Field(alias="stepgraphPath")
    sqlite_path: str = Field(alias="sqlitePath")
    modes: list[RuntimeMode]
    storage_state_variant: VariantSelection = Field(alias="storageStateVariant")
    learned_repairs_variant: VariantSelection = Field(alias="learnedRepairsVariant")
    total_cases: int = Field(alias="totalCases")
    succeeded_cases: int = Field(alias="succeededCases")
    failed_cases: int = Field(alias="failedCases")
    mean_kpis: dict[str, float] = Field(default_factory=dict, alias="meanKpis")
    cases: list[BenchCaseResult]
    notes: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class _BenchCase:
    case_id: str
    mode: RuntimeMode
    storage_state_enabled: bool
    learned_repairs_enabled: bool


@app.callback()
def _root() -> None:
    """
    Run benchmark matrix experiments and aggregate KPIs.
    """


@app.command("bench")
def bench(
    stepgraph_path: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    modes: str = typer.Option(
        "manual,llm,hybrid",
        "--modes",
        help="Comma-separated run modes to benchmark (manual,llm,hybrid).",
    ),
    storage_state_variant: VariantSelection = typer.Option(
        VariantSelection.BOTH,
        "--storage-state-variant",
        help="Run with/without storage-state reuse, or both.",
    ),
    storage_state_path: Path | None = typer.Option(
        None,
        "--storage-state-path",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Storage-state JSON path used when storage-state variant includes 'with'.",
    ),
    learned_repairs_variant: VariantSelection = typer.Option(
        VariantSelection.BOTH,
        "--learned-repairs-variant",
        help="Run with/without learned repairs, or both.",
    ),
    headless: bool = typer.Option(
        True,
        "--headless/--headed",
        help="Run browser headless (default) or headed.",
    ),
    auto_approve_hard: bool = typer.Option(
        True,
        "--auto-approve-hard/--prompt-hard-approval",
        help="Auto-approve hard-risk actions for unattended benchmark runs.",
    ),
    sqlite_path: Path | None = typer.Option(
        None,
        "--sqlite-path",
        exists=False,
        dir_okay=False,
        file_okay=True,
        help="Optional sqlite path override.",
    ),
    runs_root: Path | None = typer.Option(
        None,
        "--runs-root",
        exists=False,
        file_okay=False,
        dir_okay=True,
        help="Optional runs root override for report outputs.",
    ),
    output_path: Path | None = typer.Option(
        None,
        "--output-path",
        exists=False,
        dir_okay=False,
        file_okay=True,
        help="Optional benchmark aggregate output path.",
    ),
    stop_on_failure: bool = typer.Option(
        False,
        "--stop-on-failure/--continue-on-failure",
        help="Stop matrix execution after first failed case.",
    ),
) -> None:
    """
    Run experiment matrix and aggregate KPI reports.
    """

    async def _impl() -> None:
        selected_modes = _parse_modes(modes)
        settings = Settings.load()
        resolved_sqlite_path = str(sqlite_path) if sqlite_path is not None else settings.storage.sqlite_path
        resolved_runs_root = str(runs_root) if runs_root is not None else None

        storage_values = _variant_values(storage_state_variant)
        repair_values = _variant_values(learned_repairs_variant)
        if True in storage_values and storage_state_path is None:
            raise typer.BadParameter(
                "--storage-state-path is required when --storage-state-variant includes 'with'."
            )

        base_graph = StepGraph.model_validate_json(stepgraph_path.read_text(encoding="utf-8"))
        bench_id = f"bench_{generate_run_id()}"
        bench_cases = _build_cases(
            modes=selected_modes,
            storage_values=storage_values,
            repair_values=repair_values,
        )
        report_builder = RunReportBuilder.create(
            sqlite_path=resolved_sqlite_path,
            runs_root=resolved_runs_root,
        )

        results: list[BenchCaseResult] = []
        for case in bench_cases:
            run_id = generate_run_id()
            case_graph = base_graph.model_copy(deep=True, update={"run_id": run_id})
            started_at = datetime.now(UTC)
            case_error: str | None = None
            case_status = "completed"
            report_path: str | None = None
            report_payload: RunBenchmarkReport | None = None

            try:
                await _execute_case(
                    graph=case_graph,
                    case=case,
                    sqlite_path=resolved_sqlite_path,
                    runs_root=resolved_runs_root,
                    storage_state_path=str(storage_state_path) if case.storage_state_enabled else None,
                    headless=headless,
                    auto_approve_hard=auto_approve_hard,
                    bench_id=bench_id,
                )
            except Exception as exc:
                case_status = "failed"
                case_error = str(exc)

            try:
                report_payload = await report_builder.build_report(run_id=run_id)
                write_result = await report_builder.write_report(run_id=run_id, report=report_payload)
                report_path = write_result.report_path
            except Exception as exc:
                if case_error is None:
                    case_error = f"failed to build report: {exc}"
                case_status = "failed"

            duration_ms = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
            case_result = BenchCaseResult(
                caseId=case.case_id,
                runId=run_id,
                mode=case.mode,
                storageStateEnabled=case.storage_state_enabled,
                learnedRepairsEnabled=case.learned_repairs_enabled,
                status=case_status,
                durationMs=duration_ms,
                reportPath=report_path,
                kpis=dict(report_payload.kpis) if report_payload is not None else {},
                counts=dict(report_payload.counts) if report_payload is not None else {},
                error=case_error,
            )
            results.append(case_result)
            _print_case_result(case_result)

            if case_status == "failed" and stop_on_failure:
                break

        summary = BenchSummary(
            benchId=bench_id,
            generatedAt=datetime.now(UTC).isoformat(),
            stepgraphPath=str(stepgraph_path),
            sqlitePath=resolved_sqlite_path,
            modes=selected_modes,
            storageStateVariant=storage_state_variant,
            learnedRepairsVariant=learned_repairs_variant,
            totalCases=len(results),
            succeededCases=sum(1 for row in results if row.status == "completed"),
            failedCases=sum(1 for row in results if row.status != "completed"),
            meanKpis=_mean_kpis(results),
            cases=results,
            notes=[
                "Learned-repairs variant is recorded in run metadata for matrix analysis.",
                "Per-case run report is written to runs/<run_id>/report.json.",
            ],
        )

        destination = output_path or _default_output_path(bench_id=bench_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            summary.model_dump_json(indent=2, by_alias=True),
            encoding="utf-8",
        )
        _print_summary(summary, output_path=destination)

    asyncio.run(_impl())


def _parse_modes(value: str) -> list[RuntimeMode]:
    raw_parts = [part.strip().lower() for part in value.split(",")]
    modes: list[RuntimeMode] = []
    for part in raw_parts:
        if not part:
            continue
        try:
            candidate = RuntimeMode(part)
        except ValueError as exc:
            raise typer.BadParameter(
                f"Unknown mode '{part}'. Supported values: manual,llm,hybrid."
            ) from exc
        if candidate not in modes:
            modes.append(candidate)
    if not modes:
        raise typer.BadParameter("At least one mode must be provided.")
    return modes


def _variant_values(selection: VariantSelection) -> list[bool]:
    if selection is VariantSelection.BOTH:
        return [False, True]
    if selection is VariantSelection.WITH:
        return [True]
    return [False]


def _build_cases(
    *,
    modes: list[RuntimeMode],
    storage_values: list[bool],
    repair_values: list[bool],
) -> list[_BenchCase]:
    cases: list[_BenchCase] = []
    index = 1
    for mode in modes:
        for storage_enabled in storage_values:
            for repairs_enabled in repair_values:
                storage_label = "with_storage" if storage_enabled else "without_storage"
                repairs_label = "with_repairs" if repairs_enabled else "without_repairs"
                case_id = f"case_{index:02d}_{mode.value}_{storage_label}_{repairs_label}"
                cases.append(
                    _BenchCase(
                        case_id=case_id,
                        mode=mode,
                        storage_state_enabled=storage_enabled,
                        learned_repairs_enabled=repairs_enabled,
                    )
                )
                index += 1
    return cases


async def _execute_case(
    *,
    graph: StepGraph,
    case: _BenchCase,
    sqlite_path: str,
    runs_root: str | None,
    storage_state_path: str | None,
    headless: bool,
    auto_approve_hard: bool,
    bench_id: str,
) -> None:
    run_id = graph.run_id
    step_graph_repo = StepGraphRepository(sqlite_path=sqlite_path)
    await step_graph_repo.save(graph)
    await _update_run_row(
        sqlite_path=sqlite_path,
        run_id=run_id,
        mode=case.mode.value,
        status="running",
        ended_at=None,
        metadata_updates={
            "benchmark_case": {
                "benchId": bench_id,
                "caseId": case.case_id,
                "mode": case.mode.value,
                "storageStateEnabled": case.storage_state_enabled,
                "learnedRepairsEnabled": case.learned_repairs_enabled,
            }
        },
    )
    if case.mode is not RuntimeMode.MANUAL:
        await ModeController(initial_mode=RuntimeMode.MANUAL).switch_mode(
            target_mode=case.mode,
            reason=f"benchmark_case:{case.case_id}",
            actor="bench_harness",
            binding=RuntimeBinding(
                run_id=run_id,
                current_step_id=graph.steps[0].step_id if graph.steps else None,
            ),
            sqlite_path=sqlite_path,
            runs_root=runs_root,
        )

    writer = CheckpointWriter.for_run(run_id=run_id, sqlite_path=sqlite_path, runs_root=runs_root)
    sink = RunnerEventSink(writer)
    audit_logger = AuditLogger.for_run(run_id=run_id, runs_root=runs_root)

    settings = Settings.load()
    restrictions_policy = RestrictionsPolicy.from_settings(settings.policy)
    session = BrowserSession(headless=headless)
    status = "completed"
    context_id: str | None = None
    tab_id: str | None = None

    await session.start()
    try:
        context_kwargs: dict[str, str] = {}
        if storage_state_path is not None:
            context_kwargs["storage_state"] = storage_state_path
        context_id, context = await session.new_context(**context_kwargs)
        page = await context.new_page()
        tab_id = session.get_tab_id(page)
        if tab_id is None:
            raise RuntimeError("Failed to acquire tab_id for benchmark case.")

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
                return True
            return typer.confirm(
                f"Approve hard-risk action for step={request.step_id} ({request.action})?",
                default=False,
            )

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
        await runner.run(graph, pause_requested=lambda: False)
        last_step_id = graph.steps[-1].step_id if graph.steps else graph.run_id
        await writer.checkpoint_now(
            current_step_id=last_step_id,
            browser_session_id=session.browser_session_id,
            tab_id=tab_id,
            frame_path=[],
        )

        if storage_state_path is not None and context_id is not None:
            layout = get_run_layout(run_id, runs_root=runs_root)
            await session.save_storage_state(context_id=context_id, path=layout.storage_state_json)
    except Exception:
        status = "failed"
        raise
    finally:
        await session.stop()
        await _update_run_row(
            sqlite_path=sqlite_path,
            run_id=run_id,
            mode=case.mode.value,
            status=status,
            ended_at=datetime.now(UTC).isoformat(),
            metadata_updates=None,
        )


async def _update_run_row(
    *,
    sqlite_path: str,
    run_id: str,
    mode: str,
    status: str,
    ended_at: str | None,
    metadata_updates: dict[str, Any] | None,
) -> None:
    async with open_connection(sqlite_path) as connection:
        await ensure_run(connection, run_id=run_id, started_at=datetime.now(UTC).isoformat())
        cursor = await connection.execute(
            "SELECT metadata_json FROM runs WHERE run_id = ?;",
            (run_id,),
        )
        row = await cursor.fetchone()
        metadata = loads_json(row["metadata_json"] if row is not None else None, {})
        if not isinstance(metadata, dict):
            metadata = {}
        if metadata_updates:
            metadata.update(metadata_updates)

        await connection.execute(
            """
            UPDATE runs
            SET mode = ?, status = ?, ended_at = ?, metadata_json = ?
            WHERE run_id = ?;
            """,
            (
                mode,
                status,
                ended_at,
                dumps_json(metadata),
                run_id,
            ),
        )
        await connection.commit()


def _mean_kpis(results: list[BenchCaseResult]) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for result in results:
        for key, value in result.kpis.items():
            if value is None:
                continue
            buckets.setdefault(key, []).append(float(value))

    return {key: (sum(values) / len(values)) for key, values in buckets.items() if values}


def _default_output_path(*, bench_id: str) -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "artifacts"
        / "bench"
        / bench_id
        / "summary.json"
    )


def _print_case_result(result: BenchCaseResult) -> None:
    mode_label = result.mode.value if isinstance(result.mode, RuntimeMode) else str(result.mode)
    table = Table(title=f"Benchmark Case: {result.case_id}")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Run ID", result.run_id)
    table.add_row("Mode", mode_label)
    table.add_row("Storage state", "enabled" if result.storage_state_enabled else "disabled")
    table.add_row("Learned repairs", "enabled" if result.learned_repairs_enabled else "disabled")
    table.add_row("Status", result.status)
    table.add_row("Duration (ms)", str(result.duration_ms))
    table.add_row(
        "Flow completion rate",
        _format_metric(result.kpis.get("flow_completion_rate"), as_percent=True),
    )
    table.add_row(
        "Tokens / successful step",
        _format_metric(result.kpis.get("tokens_per_successful_step"), as_percent=False),
    )
    if result.error:
        table.add_row("Error", result.error)
    if result.report_path:
        table.add_row("Report", result.report_path)
    CONSOLE.print(table)


def _print_summary(summary: BenchSummary, *, output_path: Path) -> None:
    overview = Table(title="Benchmark Summary")
    overview.add_column("Field", style="bold")
    overview.add_column("Value")
    overview.add_row("Bench ID", summary.bench_id)
    overview.add_row("Cases", str(summary.total_cases))
    overview.add_row("Succeeded", str(summary.succeeded_cases))
    overview.add_row("Failed", str(summary.failed_cases))
    overview.add_row("Output", str(output_path))
    CONSOLE.print(overview)

    mean_table = Table(title="Mean KPIs")
    mean_table.add_column("KPI", style="bold")
    mean_table.add_column("Value", justify="right")
    for key, value in sorted(summary.mean_kpis.items()):
        mean_table.add_row(key, _format_metric(value, as_percent=_is_ratio_kpi(key)))
    CONSOLE.print(mean_table)


def _is_ratio_kpi(key: str) -> bool:
    ratio_markers = ("rate", "ratio")
    return any(marker in key for marker in ratio_markers)


def _format_metric(value: float | None, *, as_percent: bool) -> str:
    if value is None:
        return "-"
    if as_percent:
        return f"{value:.2%}"
    return f"{value:.4f}"


def main() -> None:
    app()


if __name__ == "__main__":
    main()

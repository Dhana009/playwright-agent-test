from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from agent.core.config import Settings
from agent.telemetry.report import RunBenchmarkReport, RunReportBuilder

app = typer.Typer(help="Benchmark and KPI reporting commands.")
CONSOLE = Console()


@app.callback()
def _root() -> None:
    """
    Run KPI report commands.
    """


@app.command("report")
def report(
    run_id: str = typer.Argument(..., help="Run id to summarize."),
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
        help="Optional runs root override for report output.",
    ),
    output_path: Path | None = typer.Option(
        None,
        "--output-path",
        exists=False,
        dir_okay=False,
        file_okay=True,
        help="Optional report.json destination.",
    ),
) -> None:
    """
    Print run KPIs as a rich table and write report.json.
    """

    async def _impl() -> None:
        settings = Settings.load()
        resolved_sqlite_path = (
            str(sqlite_path) if sqlite_path is not None else settings.storage.sqlite_path
        )
        builder = RunReportBuilder.create(
            sqlite_path=resolved_sqlite_path,
            runs_root=str(runs_root) if runs_root is not None else None,
        )
        report_payload = await builder.build_report(run_id=run_id)
        result = await builder.write_report(
            run_id=run_id,
            output_path=str(output_path) if output_path is not None else None,
            report=report_payload,
        )
        _print_report(report_payload, report_path=result.report_path)

    asyncio.run(_impl())


def _print_report(report: RunBenchmarkReport, *, report_path: str) -> None:
    overview = Table(title="Run Overview")
    overview.add_column("Field", style="bold")
    overview.add_column("Value")
    overview.add_row("Run ID", report.run_id)
    overview.add_row("Mode", report.mode)
    overview.add_row("Status", report.status)
    overview.add_row("Started At", report.started_at)
    overview.add_row("Ended At", report.ended_at or "-")
    overview.add_row("Report Path", report_path)
    CONSOLE.print(overview)

    kpi_table = Table(title="KPI Summary")
    kpi_table.add_column("KPI", style="bold")
    kpi_table.add_column("Value", justify="right")
    for label, key, as_percent in _kpi_rows():
        raw = report.kpis.get(key)
        kpi_table.add_row(label, _format_metric(raw, as_percent=as_percent))
    CONSOLE.print(kpi_table)

    if report.alerts:
        alert_table = Table(title="Alerts")
        alert_table.add_column("Message", style="bold red")
        for alert in report.alerts:
            alert_table.add_row(alert)
        CONSOLE.print(alert_table)


def _kpi_rows() -> list[tuple[str, str, bool]]:
    return [
        ("Tokens / successful step", "tokens_per_successful_step", False),
        ("Cost / completed flow", "cost_per_completed_flow", False),
        ("LLM calls / completed flow", "llm_calls_per_completed_flow", False),
        ("Input tokens / call", "input_tokens_per_call", False),
        ("Output tokens / call", "output_tokens_per_call", False),
        ("Token efficiency / resolved step", "token_efficiency_per_resolved_step", False),
        ("Prompt cache hit ratio", "prompt_cache_hit_ratio", True),
        ("Flow completion rate", "flow_completion_rate", True),
        ("First-pass step success rate", "first_pass_step_success_rate", True),
        ("MTTR-step (ms)", "mttr_step_ms", False),
        ("Restart avoidance rate", "restart_avoidance_rate", True),
        ("LLM assist invocation rate", "llm_assist_invocation_rate", True),
        ("Context reuse ratio", "context_reuse_ratio", True),
        ("Cache hit rate", "cache_hit_rate", True),
        ("Partial refresh ratio", "partial_refresh_ratio", True),
        ("Full refresh ratio", "full_refresh_ratio", True),
        ("Contradiction rate per 100 steps", "contradiction_rate_per_100_steps", False),
        ("Repair promotion success rate", "repair_promotion_success_rate", True),
        ("Tier-0/Tier-1 resolution ratio", "tier0_tier1_resolution_ratio", True),
        ("No-progress token burn rate", "no_progress_token_burn_rate", True),
    ]


def _format_metric(value: Any, *, as_percent: bool) -> str:
    if value is None:
        return "-"
    if isinstance(value, (int, float)):
        if as_percent:
            return f"{value:.2%}"
        return f"{value:.4f}" if isinstance(value, float) else str(value)
    return str(value)


def main() -> None:
    app()


if __name__ == "__main__":
    main()

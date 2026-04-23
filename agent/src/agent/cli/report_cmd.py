from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from agent.core.config import Settings
from agent.telemetry.report import BENCHMARK_KPI_FIELD_SPECS, RunBenchmarkReport, RunReportBuilder

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
    return list(BENCHMARK_KPI_FIELD_SPECS)


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

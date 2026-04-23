from __future__ import annotations

import typer

from agent.cli.bench_cmd import bench as bench_command
from agent.cli.export_cmd import export as export_command
from agent.cli.fix_cmd import fix as fix_command
from agent.cli.mode_cmd import app as mode_app
from agent.cli.record_cmd import record as record_command
from agent.cli.report_cmd import report as report_command
from agent.cli.run_cmd import pause as pause_command
from agent.cli.run_cmd import resume as resume_command
from agent.cli.run_cmd import run as run_command

app = typer.Typer(
    help="Playwright Agent CLI.",
    no_args_is_help=True,
)

app.command("record")(record_command)
app.command("run")(run_command)
app.command("resume")(resume_command)
app.command("pause")(pause_command)
app.command("fix")(fix_command)
app.add_typer(mode_app, name="mode")
app.command("report")(report_command)
app.command("bench")(bench_command)
app.command("export")(export_command)


def main() -> None:
    app()


if __name__ == "__main__":
    main()

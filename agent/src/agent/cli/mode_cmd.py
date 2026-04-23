from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from agent.core.config import Settings
from agent.core.mode import ModeController, RuntimeBinding, RuntimeMode, resolve_mode_for_run

app = typer.Typer(help="Runtime mode switching commands.")


@app.callback()
def _root() -> None:
    """
    Runtime mode utilities.
    """


@app.command("set")
def set_mode(
    mode: RuntimeMode = typer.Argument(..., help="Target mode: manual | llm | hybrid."),
    run_id: str | None = typer.Option(
        None,
        "--run-id",
        help="Optional run id. If provided, emits mode_switched event for that run.",
    ),
    reason: str = typer.Option("operator_request", "--reason", help="Reason for mode switch."),
    actor: str = typer.Option("operator", "--actor", help="Actor recorded in event payload."),
    current_step_id: str | None = typer.Option(
        None,
        "--current-step-id",
        help="Optional current step id for event payload.",
    ),
    browser_session_id: str | None = typer.Option(
        None,
        "--browser-session-id",
        help="Optional browser session id for event payload.",
    ),
    tab_id: str | None = typer.Option(None, "--tab-id", help="Optional active tab id for event payload."),
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
        help="Optional runs root override for events.jsonl output.",
    ),
) -> None:
    """
    Switch runtime mode without resetting run/session/checkpoint state.
    """

    async def _impl() -> None:
        settings = Settings.load()
        fallback_mode = RuntimeMode(settings.mode)

        current_mode = fallback_mode
        if run_id is not None:
            current_mode = await resolve_mode_for_run(
                run_id=run_id,
                fallback_mode=fallback_mode,
                sqlite_path=str(sqlite_path) if sqlite_path is not None else None,
            )

        controller = ModeController(initial_mode=current_mode)
        binding = (
            RuntimeBinding(
                run_id=run_id,
                current_step_id=current_step_id,
                browser_session_id=browser_session_id,
                tab_id=tab_id,
            )
            if run_id is not None
            else None
        )

        result = await controller.switch_mode(
            target_mode=mode,
            reason=reason,
            actor=actor,
            binding=binding,
            sqlite_path=str(sqlite_path) if sqlite_path is not None else None,
            runs_root=str(runs_root) if runs_root is not None else None,
        )

        typer.echo(f"previous_mode={result.previous_mode}")
        typer.echo(f"active_mode={result.active_mode}")
        typer.echo(f"changed={result.changed}")
        typer.echo(f"runtime_state_reset={result.runtime_state_reset}")
        if result.run_id is not None:
            typer.echo(f"run_id={result.run_id}")
        if result.step_id is not None:
            typer.echo(f"step_id={result.step_id}")
        if result.event_id is not None:
            typer.echo(f"event_id={result.event_id}")

    asyncio.run(_impl())


def main() -> None:
    app()


if __name__ == "__main__":
    main()

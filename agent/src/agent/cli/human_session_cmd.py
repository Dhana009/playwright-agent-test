from __future__ import annotations

from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from agent.testing.human_session import (
    CheckpointAnswer,
    HumanSession,
    new_session_id,
    parse_answer_kind,
)

app = typer.Typer(help="Phase B human verification sessions.")
CONSOLE = Console()


def _default_env_file() -> Path:
    return Path(__file__).resolve().parents[3] / ".env.test"


def _resolve_target_url(explicit: str | None) -> str:
    import os

    url = (explicit or os.environ.get("FLOWHUB_URL") or "").strip()
    if not url:
        CONSOLE.print(
            "[red]Set FLOWHUB_URL (e.g. in agent/.env.test) or pass --target-url.[/red]",
        )
        raise typer.Exit(code=1)
    return url


@app.command("start")
def start(
    session_id: str | None = typer.Option(
        None,
        "--session-id",
        help="Directory name under artifacts/human-sessions/. Default: new ULID-based id.",
    ),
    target_url: str | None = typer.Option(
        None,
        "--target-url",
        envvar="FLOWHUB_URL",
        help="FlowHub (or other) base URL; defaults to FLOWHUB_URL.",
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        exists=True,
        dir_okay=False,
        file_okay=True,
        help="YAML config path; default: agent/config/default.yaml via Settings.load().",
    ),
    env_file: Path | None = typer.Option(
        None,
        "--env-file",
        exists=True,
        dir_okay=False,
        file_okay=True,
        help="Dotenv file to load before resolving FLOWHUB_URL (default: agent/.env.test if present).",
    ),
    skip_env_file: bool = typer.Option(
        False,
        "--skip-env-file",
        help="Do not load agent/.env.test automatically.",
    ),
) -> None:
    """Create human-sessions/<id>/ with snapshot, events.jsonl, answers.jsonl, screenshots/, traces/."""

    agent_root = Path(__file__).resolve().parents[3]
    resolved_env = env_file
    if not skip_env_file and resolved_env is None:
        candidate = agent_root / ".env.test"
        if candidate.is_file():
            resolved_env = candidate
    if resolved_env is not None:
        load_dotenv(resolved_env, override=False)

    sid = new_session_id(session_id)
    url = _resolve_target_url(target_url)
    session = HumanSession(sid)
    session.start(target_url=url, config_path=config, env_file=resolved_env)

    CONSOLE.print(
        Panel.fit(
            f"[bold]Session[/bold] {sid}\n"
            f"[bold]Root[/bold] {session.session_dir}\n"
            f"[bold]Target[/bold] {url}\n\n"
            "Next: [cyan]agent human checkpoint -s "
            f"{sid}[/cyan] --id … --question …",
            title="Human session started",
        ),
    )


@app.command("checkpoint")
def checkpoint(
    session_id: str = typer.Option(
        ...,
        "--session",
        "-s",
        help="Session id from human start.",
    ),
    checkpoint_id: str = typer.Option(
        ...,
        "--id",
        help="Stable checkpoint id (e.g. b1_c1).",
    ),
    question: str = typer.Option(
        ...,
        "--question",
        "-q",
        help="Question shown to the operator.",
    ),
) -> None:
    """Prompt for one checkpoint; append a line to answers.jsonl."""

    session = HumanSession(session_id)
    if not session.session_dir.is_dir():
        raise typer.BadParameter(f"Session directory not found: {session.session_dir}")

    CONSOLE.print(Panel(question, title=f"Checkpoint [bold]{checkpoint_id}[/bold]"))
    CONSOLE.print(f"[dim]Artifacts: {session.answers_path}[/dim]")
    CONSOLE.print(
        "[dim]Reply with y / n / skip, or type free text.[/dim]",
    )
    answer_line = CONSOLE.input("[bold]Answer[/bold]: ").rstrip("\n")
    note_line = CONSOLE.input("[bold]Optional note[/bold] (empty to skip): ").strip()
    kind, primary = parse_answer_kind(answer_line)
    entry = CheckpointAnswer(
        checkpoint_id=checkpoint_id,
        question=question,
        answer=primary,
        answer_kind=kind,
        free_text_note=note_line or None,
    )
    session.append_checkpoint_answer(entry)
    CONSOLE.print(f"[green]Recorded[/green] -> {session.answers_path}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()

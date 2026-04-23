# Ported from playwright-repo-test/recorder2.js — adapted for agent/
from __future__ import annotations

import asyncio
import os
import select
import sys
from pathlib import Path
from typing import Literal

import typer

from agent.core.logging import get_logger
from agent.recorder.recorder import RecorderArtifact, StepGraphRecorder

app = typer.Typer(help="Record browser actions into a Step Graph.")
LOGGER = get_logger(__name__)

OperatorMode = Literal["auto", "click", "assert_visible", "assert_text"]


def _supports_raw_hotkey() -> bool:
    return os.name == "posix" and sys.stdin.isatty()


def _wait_for_stop_hotkey_blocking(stop_key: str) -> str:
    normalized = stop_key.lower()
    if not normalized:
        normalized = "q"

    if _supports_raw_hotkey():
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while True:
                ready, _, _ = select.select([sys.stdin], [], [], 0.25)
                if not ready:
                    continue
                key = sys.stdin.read(1)
                if key.lower() == normalized:
                    return key
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    # Fallback for non-interactive terminals: require full line input.
    while True:
        line = input().strip().lower()
        if line == normalized:
            return line


async def _record_once(
    *,
    url: str,
    run_id: str | None,
    headless: bool,
    storage_state: str | None,
    mode: OperatorMode,
    stop_key: str,
) -> RecorderArtifact:
    recorder = StepGraphRecorder(
        url=url,
        run_id=run_id,
        headless=headless,
        storage_state=storage_state,
    )
    recorder.set_operator_mode(mode)

    started = False
    try:
        await recorder.start()
        started = True
        LOGGER.info(
            "record_cli_started",
            run_id=recorder.run_id,
            url=url,
            mode=mode,
            stop_key=stop_key,
            headless=headless,
            storage_state=storage_state,
        )
        await asyncio.to_thread(_wait_for_stop_hotkey_blocking, stop_key)
    finally:
        if started:
            return await recorder.stop()

    raise RuntimeError("Recorder did not start successfully.")


@app.command("record")
def record(
    url: str = typer.Option(..., "--url", help="URL to open and record."),
    run_id: str | None = typer.Option(None, "--run-id", help="Optional explicit run id."),
    headless: bool = typer.Option(False, "--headless", help="Run recorder in headless mode."),
    storage_state: Path | None = typer.Option(
        None,
        "--storage-state",
        exists=True,
        dir_okay=False,
        file_okay=True,
        readable=True,
        help="Optional Playwright storage state JSON file for authenticated recording.",
    ),
    mode: OperatorMode = typer.Option(
        "auto",
        "--mode",
        case_sensitive=False,
        help="Initial recorder intent mode.",
    ),
    stop_key: str = typer.Option(
        "q",
        "--stop-key",
        min=1,
        max=1,
        help="Single-key hotkey used to stop recording.",
    ),
) -> None:
    """
    Record user interactions from a live browser and persist a replayable Step Graph.
    """
    storage_state_value = str(storage_state) if storage_state is not None else None
    stop_key_value = stop_key[0]

    typer.echo(f"Starting recorder for: {url}")
    if _supports_raw_hotkey():
        typer.echo(f"Press '{stop_key_value}' to stop recording.")
    else:
        typer.echo(f"Type '{stop_key_value}' then Enter to stop recording.")

    try:
        artifact = asyncio.run(
            _record_once(
                url=url,
                run_id=run_id,
                headless=headless,
                storage_state=storage_state_value,
                mode=mode,
                stop_key=stop_key_value,
            )
        )
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None

    typer.echo("")
    typer.echo(f"Run id: {artifact.run_id}")
    typer.echo(f"Step count: {artifact.step_count}")
    typer.echo(f"Step graph: {artifact.stepgraph_path}")
    typer.echo(f"Manifest: {artifact.manifest_path}")
    typer.echo(f"Next: agent run {artifact.stepgraph_path}")

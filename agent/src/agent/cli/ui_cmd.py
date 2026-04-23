"""Local browser dashboard for recorder (terminal used only to launch)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

import typer

from agent.ui.dashboard import run_dashboard


def ui(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address."),
    port: int = typer.Option(8765, "--port", help="HTTP port for the dashboard."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser tab automatically."),
    url: Optional[str] = typer.Option(
        None,
        "--url",
        help="If set, start recording this URL immediately (same as using the form).",
    ),
    storage_state: Optional[Path] = typer.Option(
        None,
        "--storage-state",
        exists=True,
        dir_okay=False,
        file_okay=True,
        readable=True,
        help="Optional Playwright storage state JSON for the auto-started session.",
    ),
    headless: bool = typer.Option(False, "--headless", help="Headless browser when using --url."),
    record_armed_start: bool = typer.Option(
        False,
        "--record-armed-start",
        help="Start with capture armed when using --url.",
    ),
) -> None:
    """Open http://127.0.0.1:<port> for recorder control (arm, waits, assert URL, save). Ctrl+C to exit."""
    storage = str(storage_state) if storage_state is not None else None
    asyncio.run(
        run_dashboard(
            host=host,
            port=port,
            open_browser=not no_browser,
            auto_url=url,
            storage_state=storage,
            headless=headless,
            record_armed_start=record_armed_start,
        )
    )

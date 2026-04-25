"""Simple HTTP server that serves the panel HTML to the injected iframe."""
from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp import web

_PANEL_HTML = Path(__file__).parent / "web" / "panel.html"


async def run_panel_http_server(port: int = 8767) -> web.AppRunner:
    """Serve the panel HTML at /panel on the given port."""
    app = web.Application()

    async def panel_handler(request: web.Request) -> web.Response:
        html = _PANEL_HTML.read_text(encoding="utf-8")
        return web.Response(
            text=html,
            content_type="text/html",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    async def health(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/panel", panel_handler)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner

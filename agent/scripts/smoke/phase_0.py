from __future__ import annotations

import asyncio
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(SCRIPT_DIR))

from agent.execution.browser import BrowserSession  # noqa: E402
from agent.stepgraph.models import StepGraph  # noqa: E402
from fixtures import running_server  # noqa: E402
from _runner import SmokeRunner  # noqa: E402

_FIXTURE_PAGE_PATHS = (
    "/",
    "/login.html",
    "/dashboard.html",
    "/dialogs.html",
    "/dynamic_list.html",
    "/upload.html",
    "/tabs.html",
    "/iframe_parent.html",
    "/iframe_child.html",
)


def _http_fetch(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=5.0) as response:  # noqa: S310
        status = int(response.getcode())
        body = response.read().decode("utf-8")
    return status, body


def _http_fetch_json(url: str) -> tuple[int, dict[str, Any]]:
    status, body = _http_fetch(url)
    return status, json.loads(body)


async def _resolve_tab_id(session: BrowserSession, page) -> str:
    for _ in range(30):
        tab_id = session.get_tab_id(page)
        if tab_id is not None:
            return tab_id
        await asyncio.sleep(0.01)
    raise RuntimeError("Timed out waiting for BrowserSession tab registration")


async def _dashboard_markers(page) -> dict[str, str | bool]:
    region = await page.get_attribute("#region-orders", "data-region-version")
    route = await page.get_attribute("body", "data-route-variant")
    stale_ref = await page.get_attribute("[data-fixture-action='primary']", "data-testid")
    modal_hidden = await page.get_attribute("#dashboard-modal", "hidden")
    return {
        "regionVersion": region or "",
        "routeVariant": route or "",
        "staleRefVersion": stale_ref or "",
        "modalOpen": modal_hidden is None,
    }


async def main() -> int:
    runner = SmokeRunner(phase="A0", default_task="A0.1")

    with runner.case("a0_1_fixture_server_and_pages_return_200", task="A0.1", feature="fixture_server"):
        with running_server() as fixture:
            for path in _FIXTURE_PAGE_PATHS:
                status, body = _http_fetch(f"{fixture.base_url}{path}")
                runner.check(status == 200, f"Expected 200 for fixture path {path}, got {status}")
                runner.check(bool(body.strip()), f"Expected non-empty response body for {path}")

    with runner.case(
        "a0_2_mutation_endpoints_toggle_known_dom_properties",
        task="A0.2",
        feature="fixture_mutations",
    ):
        with running_server() as fixture:
            session = BrowserSession(headless=True)
            try:
                await session.start()
                _, context = await session.new_context()
                page = await context.new_page()
                await _resolve_tab_id(session, page)
                await page.goto(f"{fixture.base_url}/dashboard.html")
                baseline = await _dashboard_markers(page)

                mutation_specs = (
                    ("/mutate/region", "regionVersion"),
                    ("/mutate/route", "routeVariant"),
                    ("/mutate/modal", "modalOpen"),
                    ("/mutate/stale-ref", "staleRefVersion"),
                )

                for endpoint, marker_key in mutation_specs:
                    status, payload = _http_fetch_json(f"{fixture.base_url}{endpoint}")
                    runner.check(status == 200, f"Expected 200 for {endpoint}, got {status}")
                    runner.check(
                        payload.get("changed", {}).get("key") == marker_key,
                        f"Expected changed key {marker_key} for {endpoint}",
                    )

                    await page.reload(wait_until="domcontentloaded")
                    current = await _dashboard_markers(page)
                    runner.check(
                        current[marker_key] != baseline[marker_key],
                        (
                            "Expected dashboard marker to change after mutation "
                            f"{endpoint}: {marker_key} baseline={baseline[marker_key]!r} "
                            f"current={current[marker_key]!r}"
                        ),
                    )
                    baseline = current
            finally:
                await session.stop()

    with runner.case("a0_3_fixture_graphs_validate_stepgraph_schema", task="A0.3", feature="fixture_graphs"):
        graph_dir = PROJECT_ROOT / "scripts" / "fixtures" / "graphs"
        graph_files = sorted(graph_dir.glob("*.json"))
        runner.check(graph_files, f"Expected fixture graphs under {graph_dir}")
        runner.check(
            len(graph_files) >= 5,
            f"Expected at least 5 committed fixture graphs, got {len(graph_files)}",
        )

        for graph_path in graph_files:
            payload = json.loads(graph_path.read_text(encoding="utf-8"))
            graph = StepGraph.model_validate(payload)
            round_trip = StepGraph.model_validate(graph.model_dump(mode="json", by_alias=True))
            runner.check(bool(graph.steps), f"Expected non-empty steps for {graph_path.name}")
            runner.check(
                round_trip.model_dump(mode="json", by_alias=True)
                == graph.model_dump(mode="json", by_alias=True),
                f"Expected deterministic StepGraph round-trip for {graph_path.name}",
            )

    return runner.finalize()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

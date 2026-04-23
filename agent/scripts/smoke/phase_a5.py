from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from copy import deepcopy
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(SCRIPT_DIR))

from agent.execution.browser import BrowserSession  # noqa: E402
from agent.execution.runner import StepGraphRunner  # noqa: E402
from agent.execution.snapshot import SnapshotEngine  # noqa: E402
from agent.execution.tools import ToolRuntime  # noqa: E402
from agent.recorder.recorder import StepGraphRecorder  # noqa: E402
from agent.stepgraph.models import StepGraph  # noqa: E402
from fixtures import running_server  # noqa: E402
from _runner import SmokeRunner  # noqa: E402

FIXTURE_EMAIL = "fixture@example.test"
FIXTURE_PASSWORD = "A5FixtureSecret!pwd"
GOLDEN_PATH = PROJECT_ROOT / "scripts" / "fixtures" / "goldens" / "a5_login_recording.normalized.json"


def _normalize_recorded_stepgraph(data: dict) -> dict:
    d = deepcopy(data)
    d["runId"] = "__RUN_ID__"
    for index, step in enumerate(d.get("steps", [])):
        step["stepId"] = f"__STEP_{index}__"
        target = step.get("target")
        if isinstance(target, dict):
            target.pop("reasoningHint", None)
            score = target.get("confidenceScore")
            if score is not None:
                target["confidenceScore"] = round(float(score), 3)
        meta = step.get("metadata", {})
        for volatile in ("capturedAt", "capturedSeq", "tabId"):
            meta.pop(volatile, None)
        frame_url = meta.get("frameUrl")
        if isinstance(frame_url, str):
            meta["frameUrl"] = re.sub(
                r"http://127\.0\.0\.1:\d+",
                "http://fixture.test",
                frame_url,
            )
    edges = d.get("edges", [])
    for edge_index, edge in enumerate(edges):
        edge["fromStepId"] = f"__STEP_{edge_index}__"
        edge["toStepId"] = f"__STEP_{edge_index + 1}__"
    return d


async def _resolve_tab_id(session: BrowserSession, page) -> str:
    for _ in range(30):
        tab_id = session.get_tab_id(page)
        if tab_id is not None:
            return tab_id
        await asyncio.sleep(0.01)
    raise RuntimeError("Timed out waiting for BrowserSession tab registration")


async def _record_fixture_login(*, login_url: str) -> tuple[Path, Path]:
    recorder = StepGraphRecorder(url=login_url, headless=True, poll_interval_ms=50)
    await recorder.start()
    try:
        page = recorder._page
        if page is None:
            raise RuntimeError("Recorder page not initialized")

        await page.fill("#email", FIXTURE_EMAIL)
        await asyncio.sleep(0.05)
        await page.fill("#password", FIXTURE_PASSWORD)
        await asyncio.sleep(0.05)
        await page.click("#submit-login")
        await page.wait_for_url("**/dashboard.html", timeout=15_000)
        await page.wait_for_selector("[data-testid='dashboard-title']", timeout=15_000)
        await asyncio.sleep(0.35)

        artifact = await recorder.stop()
        return Path(artifact.stepgraph_path), Path(artifact.manifest_path)
    except Exception:
        await recorder.stop()
        raise


async def main() -> int:
    runner = SmokeRunner(phase="A5", default_task="A5.1")
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    with runner.case(
        "a5_1_recorded_stepgraph_matches_golden",
        task="A5.1",
        feature="recorder_fixture",
    ):
        with running_server() as fixture:
            login_url = f"{fixture.base_url}/login.html"
            stepgraph_path, _manifest_path = await _record_fixture_login(login_url=login_url)
            observed = json.loads(stepgraph_path.read_text(encoding="utf-8"))
            normalized = _normalize_recorded_stepgraph(observed)
            runner.check(
                normalized == golden,
                "Normalized stepgraph must match committed golden "
                f"(see {GOLDEN_PATH.relative_to(PROJECT_ROOT)})",
            )

    with runner.case(
        "a5_2_password_redacted_in_stepgraph_and_manifest",
        task="A5.2",
        feature="recorder_redaction",
    ):
        with running_server() as fixture:
            login_url = f"{fixture.base_url}/login.html"
            stepgraph_path, manifest_path = await _record_fixture_login(login_url=login_url)
            combined = stepgraph_path.read_text(encoding="utf-8") + manifest_path.read_text(
                encoding="utf-8"
            )
            runner.check(
                FIXTURE_PASSWORD not in combined,
                "Password literal must not appear in stepgraph.json or manifest.json",
            )
            runner.check(
                FIXTURE_EMAIL in stepgraph_path.read_text(encoding="utf-8"),
                "Expected email field to remain stored as plain metadata.text",
            )

    with runner.case("a5_3_replay_recorded_graph_on_fixture", task="A5.3", feature="recorder_replay"):
        with running_server() as fixture:
            login_url = f"{fixture.base_url}/login.html"
            stepgraph_path, _manifest = await _record_fixture_login(login_url=login_url)

            previous = os.environ.get("FLOWHUB_PASSWORD")
            os.environ["FLOWHUB_PASSWORD"] = FIXTURE_PASSWORD
            try:
                graph = StepGraph.model_validate_json(stepgraph_path.read_text(encoding="utf-8"))
                session = BrowserSession(headless=True)
                await session.start()
                try:
                    _, context = await session.new_context()
                    page = await context.new_page()
                    await page.goto(login_url, wait_until="domcontentloaded", timeout=15_000)
                    tab_id = await _resolve_tab_id(session, page)
                    for step in graph.steps:
                        step.metadata["tabId"] = tab_id

                    snapshot_engine = SnapshotEngine(session)
                    runtime = ToolRuntime(session, snapshot_engine=snapshot_engine)
                    graph_runner = StepGraphRunner(runtime, snapshot_engine=snapshot_engine)
                    await graph_runner.run(graph, pause_requested=lambda: False)

                    await page.wait_for_selector("[data-testid='dashboard-title']", timeout=15_000)
                    title = (await page.title()).lower()
                    runner.check("dashboard" in title, f"Expected dashboard title after replay, got {title!r}")
                finally:
                    await session.stop()
            finally:
                if previous is None:
                    os.environ.pop("FLOWHUB_PASSWORD", None)
                else:
                    os.environ["FLOWHUB_PASSWORD"] = previous

    return runner.finalize()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

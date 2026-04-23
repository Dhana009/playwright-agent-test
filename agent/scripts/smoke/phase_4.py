from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

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
from fixtures import live_target  # noqa: E402
from _runner import SmokeRunner  # noqa: E402


EMAIL_SELECTORS = (
    'input[type="email"]',
    'input[name="email"]',
    'input[name*="email" i]',
    'input[id*="email" i]',
)
PASSWORD_SELECTORS = (
    'input[type="password"]',
    'input[name="password"]',
    'input[name*="password" i]',
    'input[id*="password" i]',
)
SUBMIT_SELECTORS = (
    'button[type="submit"]',
    'button:has-text("Login")',
    'button:has-text("Log in")',
    'button:has-text("Sign in")',
    'input[type="submit"]',
)
POST_LOGIN_SELECTORS = (
    '[data-testid*="dashboard" i]',
    '[data-testid*="logout" i]',
    'nav',
    'button:has-text("Logout")',
    'button:has-text("Sign out")',
)


def _headless_mode() -> bool:
    if os.getenv("SMOKE_HEADLESS", "").strip() == "1":
        return True
    # Default to headed mode for Phase T4 so operator can verify recording behavior.
    return False


def _interactive_recording_enabled(*, headless: bool) -> bool:
    raw = os.getenv("SMOKE_INTERACTIVE")
    if raw is None:
        # Default to manual recording when browser is visible.
        return not headless
    return raw.strip() == "1"


def _wait_for_manual_stop(*, login_url: str) -> None:
    prompt = (
        "\n[T4.1 Manual Recording]\n"
        f"1) Complete login flow in the opened browser at {login_url}\n"
        "2) Confirm dashboard is reached\n"
        "3) Return here and press Enter to stop recording and continue replay\n"
        "Press Enter when done: "
    )
    sys.__stdout__.write(prompt)
    sys.__stdout__.flush()
    sys.__stdin__.readline()


async def _fill_first_visible(
    *,
    page: Page,
    selectors: tuple[str, ...],
    value: str,
    field_name: str,
) -> None:
    last_error: Exception | None = None
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=2_500)
            await locator.fill(value, timeout=5_000)
            return
        except Exception as exc:  # pragma: no cover - smoke fallback path
            last_error = exc
    raise AssertionError(
        f"Unable to fill {field_name} on live login page. Tried selectors: {selectors}."
    ) from last_error


async def _click_first_visible(*, page: Page, selectors: tuple[str, ...], action_name: str) -> None:
    last_error: Exception | None = None
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=2_500)
            await locator.click(timeout=5_000)
            return
        except Exception as exc:  # pragma: no cover - smoke fallback path
            last_error = exc
    raise AssertionError(
        f"Unable to click {action_name} on live login page. Tried selectors: {selectors}."
    ) from last_error


def _normalize_path(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    normalized = parsed.path.rstrip("/")
    return normalized or "/"


def _looks_logged_in(*, current_url: str, login_url: str) -> bool:
    login_path = _normalize_path(login_url).lower()
    current_path = _normalize_path(current_url).lower()
    if current_path != login_path:
        return True
    return "/login" not in current_path


async def _wait_for_post_login_signal(*, page: Page, login_url: str) -> None:
    try:
        await page.wait_for_function(
            """
            (loginUrl) => {
              const current = new URL(window.location.href);
              const login = new URL(loginUrl, window.location.href);
              const trim = (value) => {
                const normalized = value.replace(/\\/+$/, "");
                return normalized || "/";
              };
              const currentPath = trim(current.pathname).toLowerCase();
              const loginPath = trim(login.pathname).toLowerCase();
              if (current.origin !== login.origin) {
                return false;
              }
              if (currentPath !== loginPath) {
                return true;
              }
              return !currentPath.endsWith("/login");
            }
            """,
            arg=login_url,
            timeout=15_000,
        )
        return
    except PlaywrightTimeoutError:
        pass

    for selector in POST_LOGIN_SELECTORS:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=1_000)
            return
        except PlaywrightTimeoutError:
            continue

    current_url = page.url
    if not _looks_logged_in(current_url=current_url, login_url=login_url):
        raise AssertionError(
            f"Login success signal not detected; still on login route. current_url={current_url!r}"
        )


def _print_manual_recording_banner(*, login_url: str) -> None:
    message = (
        "\n[T4.1 Manual Recording]\n"
        f"- Browser opened at: {login_url}\n"
        "- Please perform login manually (email, password, submit).\n"
        "- Recording will auto-stop once post-login state is detected.\n"
    )
    sys.__stdout__.write(message)
    sys.__stdout__.flush()


async def _record_live_login(*, headless: bool, interactive: bool) -> tuple[str, Path, Path]:
    target = live_target()
    recorder = StepGraphRecorder(
        url=target.url,
        headless=headless,
        poll_interval_ms=75,
    )
    await recorder.start()
    try:
        page = recorder._page  # intentional for smoke: drive the page deterministically
        if page is None:
            raise RuntimeError("Recorder page not initialized")
        # Fail fast instead of hanging forever if something regresses.
        page.set_default_timeout(15_000)
        page.set_default_navigation_timeout(15_000)

        if interactive:
            await asyncio.to_thread(_print_manual_recording_banner, login_url=target.url)
            await _wait_for_post_login_signal(page=page, login_url=target.url)
            # Let the recorder poller drain user actions after successful navigation.
            await asyncio.sleep(0.75)
            artifact = await recorder.stop()
            return artifact.run_id, Path(artifact.stepgraph_path), Path(artifact.manifest_path)

        await _fill_first_visible(
            page=page,
            selectors=EMAIL_SELECTORS,
            value=target.email,
            field_name="email",
        )
        await asyncio.sleep(0.05)
        await _fill_first_visible(
            page=page,
            selectors=PASSWORD_SELECTORS,
            value=target.password,
            field_name="password",
        )
        await asyncio.sleep(0.05)
        await _click_first_visible(page=page, selectors=SUBMIT_SELECTORS, action_name="login submit")
        await _wait_for_post_login_signal(page=page, login_url=target.url)
        # Give the recorder poll loop time to drain in-page queue.
        await asyncio.sleep(0.35)
        artifact = await recorder.stop()
        return artifact.run_id, Path(artifact.stepgraph_path), Path(artifact.manifest_path)
    except Exception:
        await recorder.stop()
        raise


async def _replay_stepgraph(*, stepgraph_path: Path, headless: bool) -> None:
    target = live_target()
    os.environ.setdefault("FLOWHUB_PASSWORD", target.password)
    graph = StepGraph.model_validate_json(stepgraph_path.read_text(encoding="utf-8"))

    session = BrowserSession(headless=headless)
    await session.start()
    try:
        _, context = await session.new_context()
        page = await context.new_page()
        await page.goto(target.url, wait_until="domcontentloaded", timeout=15_000)
        tab_id = session.get_tab_id(page)
        if tab_id is None:
            raise RuntimeError("Failed to resolve tab id for replay")
        for step in graph.steps:
            step.metadata["tabId"] = tab_id

        snapshot_engine = SnapshotEngine(session)
        runtime = ToolRuntime(session, snapshot_engine=snapshot_engine)
        runner = StepGraphRunner(runtime, snapshot_engine=snapshot_engine)
        await runner.run(graph, pause_requested=lambda: False)
        await _wait_for_post_login_signal(page=page, login_url=target.url)
    finally:
        await session.stop()


async def main() -> int:
    headless = _headless_mode()
    interactive = _interactive_recording_enabled(headless=headless)
    runner = SmokeRunner(phase="T4", default_task="T4.1")
    recorded_stepgraph_path: Path | None = None

    with runner.case(
        "t4_1_record_round_trip_live",
        task="T4.1",
        feature="recorder_live",
        error_class="config",
    ):
        target = live_target()
        run_id, stepgraph_path, manifest_path = await _record_live_login(
            headless=headless,
            interactive=interactive,
        )
        recorded_stepgraph_path = stepgraph_path
        runner.check(stepgraph_path.exists(), f"Expected stepgraph.json at {stepgraph_path}")
        runner.check(manifest_path.exists(), f"Expected manifest.json at {manifest_path}")

        stepgraph_text = stepgraph_path.read_text(encoding="utf-8")
        manifest_text = manifest_path.read_text(encoding="utf-8")
        runner.check(
            "\"steps\": []" not in stepgraph_text,
            "Expected recorder to capture at least one step.",
        )
        runner.check(
            target.password not in stepgraph_text,
            "Expected FLOWHUB password to be redacted from stepgraph.json.",
        )
        runner.check(
            target.password not in manifest_text,
            "Expected FLOWHUB password to be absent from manifest.json.",
        )
        runner.check(
            "\"valueRef\": \"redacted\"" in stepgraph_text,
            "Expected sensitive input to be represented via metadata.valueRef=redacted.",
        )
        runner.check(run_id in stepgraph_text, "Expected runId to appear in stepgraph.json.")
        runner.check(run_id in manifest_text, "Expected runId to appear in manifest.json.")

    with runner.case(
        "t4_2_replay_recorded_graph_live",
        task="T4.2",
        feature="recorder_replay_live",
        error_class="config",
    ):
        runner.check(
            recorded_stepgraph_path is not None,
            "T4.2 requires a successful T4.1 recording artifact.",
        )
        stepgraph_path = recorded_stepgraph_path
        runner.check(
            stepgraph_path.exists(),
            f"Expected recorded stepgraph to exist at {stepgraph_path}",
        )
        runner.check(
            "\"steps\": []" not in stepgraph_path.read_text(encoding="utf-8"),
            "Expected recorder to capture steps before replay.",
        )
        await _replay_stepgraph(stepgraph_path=stepgraph_path, headless=headless)

    with runner.case("t4_3_porting_notes_recorder_findings", task="T4.3", feature="porting_notes"):
        notes_path = PROJECT_ROOT / "PORTING_NOTES.md"
        runner.check(notes_path.exists(), f"Expected {notes_path} to exist")
        notes = notes_path.read_text(encoding="utf-8")
        runner.check(
            "## Phase 6.0 - Recorder Feasibility Spike" in notes,
            "Expected PORTING_NOTES recorder spike findings section to exist",
        )
        runner.check(
            "## Phase 6.1 - Headless Recorder Implementation" in notes,
            "Expected PORTING_NOTES recorder implementation section to exist",
        )
        runner.check(
            "binding race gap" in notes or "in-page durable queue" in notes,
            "Expected PORTING_NOTES to capture recorder queue/binding race assumptions",
        )

    return runner.finalize()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

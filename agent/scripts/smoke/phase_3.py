from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import urllib.request
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(SCRIPT_DIR))

from agent.execution.browser import BrowserSession  # noqa: E402
from agent.execution.snapshot import SnapshotEngine  # noqa: E402
from agent.execution.tools import (  # noqa: E402
    AssertionResult,
    DialogResult,
    FrameContextResult,
    InteractionResult,
    NavigateResult,
    ToolCallEvent,
    ToolResult,
    ToolRuntime,
    WaitResult,
)
from fixtures import running_server  # noqa: E402
from _runner import SmokeRunner  # noqa: E402


async def _resolve_tab_id(session: BrowserSession, page) -> str:
    for _ in range(30):
        tab_id = session.get_tab_id(page)
        if tab_id is not None:
            return tab_id
        await asyncio.sleep(0.01)
    raise RuntimeError("Timed out waiting for BrowserSession tab registration")


def _http_get(url: str) -> None:
    with urllib.request.urlopen(url, timeout=5.0) as response:  # noqa: S310
        response.read()


def _expect_succeeded_tool_counts(
    runner: SmokeRunner,
    tool_events: list[ToolCallEvent],
    expected_sequence: list[str],
) -> None:
    succeeded = [event.tool for event in tool_events if event.status == "succeeded"]
    expected = Counter(expected_sequence)
    actual = Counter(succeeded)
    runner.check(
        actual == expected,
        f"Expected per-tool succeeded event counts {dict(expected)}, got {dict(actual)}",
    )
    failed = [event for event in tool_events if event.status == "failed"]
    runner.check(not failed, f"Unexpected failed tool events: {failed!r}")


async def main() -> int:
    runner = SmokeRunner(phase="A3", default_task="A3.1")

    with runner.case("a3_1_browser_session_lifecycle", task="A3.1", feature="browser_session"):
        with running_server() as fixture:
            session = BrowserSession(headless=True)
            try:
                browser_session_id = await session.start()
                runner.check(bool(browser_session_id), "Expected browser_session_id from start()")

                context_id, context = await session.new_context()
                page = await context.new_page()
                tab_id = await _resolve_tab_id(session, page)
                runner.check(bool(context_id), "Expected a tracked context id")
                runner.check(bool(tab_id), "Expected a tracked tab id")

                await page.goto(f"{fixture.base_url}/dashboard.html")

                with tempfile.TemporaryDirectory(prefix="a3-storage-state-") as tmp:
                    state_path = Path(tmp) / "storage_state.json"
                    await session.save_storage_state(context_id=context_id, path=state_path)
                    runner.check(state_path.exists(), f"Expected storage state file at {state_path}")
                    state_payload = state_path.read_text(encoding="utf-8")
                    parsed = json.loads(state_payload)
                    runner.check(isinstance(parsed, dict), "Expected storage state to be a JSON object")
                    runner.check("cookies" in parsed, "Expected storage state to contain cookies key")

                    reuse_id, reuse_context = await session.new_context(storage_state=state_path)
                    runner.check(bool(reuse_id), "Expected second context from storage_state")
                    reuse_page = await reuse_context.new_page()
                    await reuse_page.goto(f"{fixture.base_url}/dashboard.html", wait_until="domcontentloaded")
                    runner.check(
                        "dashboard" in (await reuse_page.title()).lower(),
                        "Expected dashboard title after loading with reused storage state",
                    )
            finally:
                await session.stop()

            runner.check(not session.is_started, "Expected BrowserSession.stop() to close browser")

    with runner.case("a3_2_snapshot_refs_and_region_mutation_fingerprint", task="A3.2", feature="snapshot_engine"):
        with running_server() as fixture:
            session = BrowserSession(headless=True)
            try:
                await session.start()
                _, context = await session.new_context()
                page = await context.new_page()
                tab_id = await _resolve_tab_id(session, page)

                # Match fixture_state.js initial URL shape so polling does not replaceState
                # between snapshots (that would change frame URLs and frameHash spuriously).
                await page.goto(
                    f"{fixture.base_url}/dashboard.html?fixtureRv=1",
                    wait_until="domcontentloaded",
                )

                snapshot_engine = SnapshotEngine(session)
                before = await snapshot_engine.capture_snapshot(tab_id)
                runner.check(bool(before.elements), "Expected snapshot to contain elements")
                runner.check(bool(before.aria_yaml), "Expected aria snapshot to be captured")

                for element in before.elements:
                    handle = await snapshot_engine.resolve_ref(element.ref)
                    runner.check(handle is not None, f"Expected resolve_ref to succeed for {element.ref!r}")

                fp0 = before.fingerprint
                _http_get(f"{fixture.base_url}/mutate/region")
                await page.wait_for_timeout(400)
                after = await snapshot_engine.capture_snapshot(tab_id)
                fp1 = after.fingerprint

                runner.check(
                    fp0.route_template == fp1.route_template,
                    "Expected routeTemplate unchanged after /mutate/region",
                )
                runner.check(
                    fp0.frame_hash == fp1.frame_hash,
                    "Expected frameHash unchanged after /mutate/region",
                )
                runner.check(
                    fp0.modal_state == fp1.modal_state,
                    "Expected modalState unchanged after /mutate/region",
                )
                runner.check(
                    fp0.dom_hash != fp1.dom_hash,
                    "Expected domHash to change after /mutate/region (region DOM updated)",
                )
            finally:
                await session.stop()

    with runner.case("a3_3_core_tools_events_types_idempotency", task="A3.3", feature="core_tools"):
        with running_server() as fixture:
            tool_events: list[ToolCallEvent] = []

            async def _emit(event: ToolCallEvent) -> None:
                tool_events.append(event)

            session = BrowserSession(headless=True)
            try:
                await session.start()
                _, context = await session.new_context()
                page = await context.new_page()
                tab_id = await _resolve_tab_id(session, page)
                snapshot_engine = SnapshotEngine(session)
                runtime = ToolRuntime(session, snapshot_engine=snapshot_engine, event_emitter=_emit)

                nav_login = await runtime.navigate(tab_id=tab_id, url=f"{fixture.base_url}/login.html")
                runner.check(isinstance(nav_login, NavigateResult), "navigate should return NavigateResult")
                fill_r = await runtime.fill(tab_id=tab_id, target="#email", text="admin1@test.com")
                runner.check(isinstance(fill_r, InteractionResult), "fill should return InteractionResult")
                type_r = await runtime.type(tab_id=tab_id, target="#password", text="Test123!@#")
                runner.check(isinstance(type_r, InteractionResult), "type should return InteractionResult")
                press_r = await runtime.press(tab_id=tab_id, target="#password", key="End")
                runner.check(isinstance(press_r, InteractionResult), "press should return InteractionResult")
                click_r = await runtime.click(tab_id=tab_id, target="#submit-login")
                runner.check(isinstance(click_r, InteractionResult), "click should return InteractionResult")
                wait_r = await runtime.wait_for(
                    tab_id=tab_id, target="[data-testid='dashboard-title']", state="visible"
                )
                runner.check(isinstance(wait_r, WaitResult), "wait_for should return WaitResult")
                av_r = await runtime.assert_visible(tab_id=tab_id, target="[data-testid='dashboard-title']")
                runner.check(isinstance(av_r, AssertionResult), "assert_visible should return AssertionResult")
                at_r = await runtime.assert_text(
                    tab_id=tab_id,
                    target="[data-testid='dashboard-title']",
                    expected="Dashboard",
                )
                runner.check(isinstance(at_r, AssertionResult), "assert_text should return AssertionResult")
                au_r = await runtime.assert_url(tab_id=tab_id, expected="/dashboard.html")
                runner.check(isinstance(au_r, AssertionResult), "assert_url should return AssertionResult")
                title_r = await runtime.assert_title(tab_id=tab_id, expected="Fixture Dashboard")
                runner.check(isinstance(title_r, AssertionResult), "assert_title should return AssertionResult")

                await runtime.assert_visible(tab_id=tab_id, target="[data-testid='dashboard-title']")
                await runtime.assert_url(tab_id=tab_id, expected="/dashboard.html")

                await runtime.navigate(tab_id=tab_id, url=f"{fixture.base_url}/dialogs.html")
                confirm_task = asyncio.create_task(runtime.dialog_handle(tab_id=tab_id, accept=False))
                await asyncio.sleep(0)
                await runtime.click(tab_id=tab_id, target="#show-confirm")
                confirm_result = await confirm_task
                runner.check(isinstance(confirm_result, DialogResult), "dialog_handle should return DialogResult")
                runner.check(not confirm_result.accepted, "Expected confirm dialog to be dismissed")
                await runtime.assert_text(tab_id=tab_id, target="#dialog-result", expected="confirm dismissed")

                prompt_task = asyncio.create_task(
                    runtime.dialog_handle(tab_id=tab_id, accept=True, prompt_text="from-smoke")
                )
                await asyncio.sleep(0)
                await runtime.click(tab_id=tab_id, target="#show-prompt")
                prompt_result = await prompt_task
                runner.check(prompt_result.accepted, "Expected prompt dialog to be accepted")
                await runtime.assert_text(tab_id=tab_id, target="#dialog-result", expected="from-smoke")

                await runtime.navigate(tab_id=tab_id, url=f"{fixture.base_url}/iframe_parent.html")
                frame_enter = await runtime.frame_enter(tab_id=tab_id, target="#child-frame")
                runner.check(isinstance(frame_enter, FrameContextResult), "frame_enter should return FrameContextResult")
                runner.check(bool(frame_enter.frame_id), "Expected frame_enter to return frame id")
                await runtime.assert_text(
                    tab_id=tab_id,
                    target="[data-testid='iframe-child-title']",
                    expected="Iframe Child",
                )
                await runtime.click(tab_id=tab_id, target="#iframe-action")
                await runtime.assert_text(tab_id=tab_id, target="#iframe-status", expected="iframe button clicked")
                frame_exit = await runtime.frame_exit(tab_id=tab_id)
                runner.check(isinstance(frame_exit, FrameContextResult), "frame_exit should return FrameContextResult")
                await runtime.assert_visible(tab_id=tab_id, target="#child-frame")

                core_sequence = [
                    "navigate",
                    "fill",
                    "type",
                    "press",
                    "click",
                    "wait_for",
                    "assert_visible",
                    "assert_text",
                    "assert_url",
                    "assert_title",
                    "assert_visible",
                    "assert_url",
                    "navigate",
                    "dialog_handle",
                    "click",
                    "assert_text",
                    "dialog_handle",
                    "click",
                    "assert_text",
                    "navigate",
                    "frame_enter",
                    "assert_text",
                    "click",
                    "assert_text",
                    "frame_exit",
                    "assert_visible",
                ]
                _expect_succeeded_tool_counts(runner, tool_events, core_sequence)

                core_tools = set(core_sequence)
                succeeded_tools = {event.tool for event in tool_events if event.status == "succeeded"}
                runner.check(
                    core_tools == succeeded_tools,
                    "Expected succeeded tool names to match the scripted core tool set exactly",
                )
            finally:
                await session.stop()

    with runner.case("a3_4_extended_tools_events_types_idempotency", task="A3.4", feature="extended_tools"):
        with running_server() as fixture:
            tool_events: list[ToolCallEvent] = []

            async def _emit(event: ToolCallEvent) -> None:
                tool_events.append(event)

            session = BrowserSession(headless=True)
            try:
                await session.start()
                _, context = await session.new_context()
                page = await context.new_page()
                tab_id = await _resolve_tab_id(session, page)
                runtime = ToolRuntime(session, snapshot_engine=SnapshotEngine(session), event_emitter=_emit)

                await runtime.navigate(tab_id=tab_id, url=f"{fixture.base_url}/dashboard.html")
                await page.evaluate(
                    """
                    () => {
                      const host = document.createElement('section');
                      host.id = 'extended-fixture';
                      host.innerHTML = `
                        <input id="value-input" value="" />
                        <input id="accept-box" type="checkbox" />
                        <select id="plan-select">
                          <option value="free">Free</option>
                          <option value="pro">Pro</option>
                        </select>
                        <button id="hover-target">Hover me</button>
                        <button id="focus-target">Focus me</button>
                        <div id="hidden-target" style="display:none">Hidden block</div>
                        <ul id="count-list"><li>A</li><li>B</li><li>C</li></ul>
                        <div id="drag-source" draggable="true" style="width:120px;height:30px;background:#ddd;">Drag Source</div>
                        <div id="drag-target" style="width:150px;height:60px;border:1px solid #222;">Drop Zone</div>
                      `;
                      document.body.appendChild(host);
                      const dragSource = document.getElementById('drag-source');
                      const dragTarget = document.getElementById('drag-target');
                      dragSource.addEventListener('dragstart', (event) => {
                        event.dataTransfer.setData('text/plain', 'drag-source');
                      });
                      dragTarget.addEventListener('dragover', (event) => event.preventDefault());
                      dragTarget.addEventListener('drop', (event) => {
                        event.preventDefault();
                        dragTarget.textContent = 'dropped';
                      });
                    }
                    """
                )

                await runtime.fill(tab_id=tab_id, target="#value-input", text="hello")
                await runtime.type(tab_id=tab_id, target="#value-input", text=" world")
                val_a = await runtime.assert_value(tab_id=tab_id, target="#value-input", expected="hello world")
                runner.check(isinstance(val_a, AssertionResult), "assert_value should return AssertionResult")
                val_b = await runtime.assert_value(tab_id=tab_id, target="#value-input", expected="hello world")
                runner.check(isinstance(val_b, AssertionResult), "repeat assert_value should return AssertionResult")

                await runtime.check(tab_id=tab_id, target="#accept-box")
                await runtime.assert_checked(tab_id=tab_id, target="#accept-box", expected=True)
                await runtime.uncheck(tab_id=tab_id, target="#accept-box")
                await runtime.assert_checked(tab_id=tab_id, target="#accept-box", expected=False)

                await runtime.select(tab_id=tab_id, target="#plan-select", value="pro")
                await runtime.assert_value(tab_id=tab_id, target="#plan-select", expected="pro")
                await runtime.assert_enabled(tab_id=tab_id, target="#focus-target", expected=True)
                await runtime.assert_hidden(tab_id=tab_id, target="#hidden-target")
                await runtime.assert_count(tab_id=tab_id, target="#count-list li", expected_count=3)
                await runtime.assert_in_viewport(tab_id=tab_id, target="#hover-target")
                await runtime.hover(tab_id=tab_id, target="#hover-target")
                await runtime.focus(tab_id=tab_id, target="#focus-target")
                await runtime.drag(tab_id=tab_id, start_target="#drag-source", end_target="#drag-target")
                await runtime.assert_text(tab_id=tab_id, target="#drag-target", expected="dropped")

                await page.evaluate("() => console.info('a3-extended-console')")
                await page.evaluate("() => fetch('/dashboard.html?from=extended-tools', { cache: 'no-store' })")
                wait_to = await runtime.wait_timeout(tab_id=tab_id, timeout_ms=200)
                runner.check(isinstance(wait_to, WaitResult), "wait_timeout should return WaitResult")

                console_result = await runtime.console_messages(tab_id=tab_id, min_level="info")
                runner.check(isinstance(console_result, ToolResult), "console_messages should return ToolResult")
                console_texts = [
                    str(message.get("text", ""))
                    for message in console_result.details.get("messages", [])
                ]
                runner.check(
                    any("a3-extended-console" in text for text in console_texts),
                    "Expected console_messages to include injected console.info payload",
                )

                network_result = await runtime.network_requests(tab_id=tab_id)
                runner.check(isinstance(network_result, ToolResult), "network_requests should return ToolResult")
                network_urls = [
                    str(entry.get("url", ""))
                    for entry in network_result.details.get("requests", [])
                ]
                runner.check(
                    any("from=extended-tools" in url for url in network_urls),
                    "Expected network_requests to include fixture fetch call",
                )

                with tempfile.TemporaryDirectory(prefix="a3-artifacts-") as tmp:
                    screenshot_path = Path(tmp) / "extended.png"
                    trace_path = Path(tmp) / "extended.trace.zip"
                    screenshot_result = await runtime.screenshot(
                        tab_id=tab_id,
                        path=str(screenshot_path),
                        full_page=True,
                    )
                    trace_result = await runtime.take_trace(tab_id=tab_id, path=str(trace_path))
                    runner.check(isinstance(screenshot_result, ToolResult), "screenshot should return ToolResult")
                    runner.check(isinstance(trace_result, ToolResult), "take_trace should return ToolResult")
                    runner.check(screenshot_path.exists(), "Expected screenshot artifact to be written")
                    runner.check(trace_path.exists(), "Expected trace artifact to be written")
                    runner.check(
                        int(screenshot_result.details.get("sizeBytes", 0)) > 0,
                        "Expected screenshot bytes to be greater than 0",
                    )
                    runner.check(
                        str(trace_result.details.get("path", "")).endswith(".zip"),
                        "Expected trace result path to end with .zip",
                    )

                with tempfile.NamedTemporaryFile(prefix="a3-upload-", suffix=".txt") as temp_upload:
                    temp_upload.write(b"upload smoke")
                    temp_upload.flush()
                    await runtime.navigate(tab_id=tab_id, url=f"{fixture.base_url}/upload.html")
                    up_r = await runtime.upload(tab_id=tab_id, target="#file-input", file_paths=temp_upload.name)
                    runner.check(isinstance(up_r, InteractionResult), "upload should return InteractionResult")
                    await runtime.assert_text(
                        tab_id=tab_id,
                        target="#file-result",
                        expected=Path(temp_upload.name).name,
                    )

                await runtime.navigate(tab_id=tab_id, url=f"{fixture.base_url}/tabs.html")
                extra_page = await context.new_page()
                await extra_page.goto(f"{fixture.base_url}/dashboard.html")
                extra_tab_id = await _resolve_tab_id(session, extra_page)

                tabs_result = await runtime.tabs_list(tab_id=tab_id)
                runner.check(
                    int(tabs_result.details.get("count", 0)) >= 2,
                    "Expected tabs_list to return at least two tabs",
                )
                ts_r = await runtime.tabs_select(tab_id=extra_tab_id)
                runner.check(isinstance(ts_r, ToolResult), "tabs_select should return ToolResult")
                await runtime.assert_title(tab_id=extra_tab_id, expected="Fixture Dashboard")
                close_result = await runtime.tabs_close(tab_id=extra_tab_id)
                runner.check(isinstance(close_result, ToolResult), "tabs_close should return ToolResult")
                runner.check(
                    close_result.details.get("closedTabId") == extra_tab_id,
                    "Expected tabs_close to close selected extra tab",
                )

                extended_sequence = [
                    "navigate",
                    "fill",
                    "type",
                    "assert_value",
                    "assert_value",
                    "check",
                    "assert_checked",
                    "uncheck",
                    "assert_checked",
                    "select",
                    "assert_value",
                    "assert_enabled",
                    "assert_hidden",
                    "assert_count",
                    "assert_in_viewport",
                    "hover",
                    "focus",
                    "drag",
                    "assert_text",
                    "wait_timeout",
                    "console_messages",
                    "network_requests",
                    "screenshot",
                    "take_trace",
                    "navigate",
                    "upload",
                    "assert_text",
                    "navigate",
                    "tabs_list",
                    "tabs_select",
                    "assert_title",
                    "tabs_close",
                ]
                _expect_succeeded_tool_counts(runner, tool_events, extended_sequence)

                extended_tools = set(extended_sequence)
                succeeded_tools = {event.tool for event in tool_events if event.status == "succeeded"}
                runner.check(
                    extended_tools == succeeded_tools,
                    "Expected succeeded tool names to match the scripted extended tool set exactly",
                )
            finally:
                await session.stop()

    return runner.finalize()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

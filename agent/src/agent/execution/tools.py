# Ported from playwright-cli/playwright-cli.js and playwright-repo-test/lib/execute.js — adapted for agent/
from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Awaitable, Callable, Literal
from urllib.parse import unquote

from playwright.async_api import BrowserContext, Dialog, Frame, Locator, Page
from pydantic import BaseModel, ConfigDict, Field

from agent.core.logging import get_logger
from agent.execution.browser import BrowserSession, BrowserSessionError
from agent.execution.snapshot import SnapshotEngine


class ToolCallEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    tool: str
    tab_id: str = Field(alias="tabId")
    status: Literal["started", "succeeded", "failed"]
    actor: str = "tool_layer"
    payload: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    tool: str
    tab_id: str = Field(alias="tabId")
    frame_path: list[str] = Field(default_factory=list, alias="framePath")
    ok: bool = True
    details: dict[str, Any] = Field(default_factory=dict)


class NavigateResult(ToolResult):
    url: str
    status_code: int | None = Field(default=None, alias="statusCode")


class WaitResult(ToolResult):
    waited_ms: int | None = Field(default=None, alias="waitedMs")


class InteractionResult(ToolResult):
    target: str


class AssertionResult(ToolResult):
    assertion: str
    expected: str
    actual: str


class DialogResult(ToolResult):
    accepted: bool
    dialog_type: str = Field(alias="dialogType")
    dialog_message: str = Field(alias="dialogMessage")


class FrameContextResult(ToolResult):
    frame_id: str | None = Field(default=None, alias="frameId")


ToolEventEmitter = Callable[[ToolCallEvent], Awaitable[None] | None]

# Finds the real <input type=file> from a custom upload control (div/button/label), same
# strategy as recorder __agentFindAssociatedFileInput. Returns a Playwright selector string.
_UPLOAD_FILE_SELECTOR_FROM_TRIGGER_JS = """
(el) => {
  function fileIn(root) {
    if (!root || !root.querySelector) return null;
    return root.querySelector('input[type="file"],input[type=file]');
  }
  function fileInSubtreeBfs(start, maxNodes) {
    if (!start || start.nodeType !== 1) return null;
    const q = [start];
    let seen = 0;
    while (q.length && seen < maxNodes) {
      const n = q.shift();
      seen++;
      if (!n || n.nodeType !== 1) continue;
      const tag = n.tagName && n.tagName.toLowerCase();
      const typ = String(n.getAttribute('type') || '').toLowerCase();
      if (tag === 'input' && typ === 'file') return n;
      if (n.shadowRoot) {
        const sh = fileIn(n.shadowRoot);
        if (sh) return sh;
        let c = n.shadowRoot.firstElementChild;
        while (c) { q.push(c); c = c.nextElementSibling; }
      }
      let ch = n.firstElementChild;
      while (ch) { q.push(ch); ch = ch.nextElementSibling; }
    }
    return null;
  }
  function find(node) {
    if (!node || node.nodeType !== 1) return null;
    const tag = node.tagName.toLowerCase();
    const typ = String(node.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && typ === 'file') return node;
    let inner = fileIn(node);
    if (inner) return inner;
    inner = fileInSubtreeBfs(node, 800);
    if (inner) return inner;
    let s = node.previousElementSibling;
    while (s) {
      const tg = s.tagName && s.tagName.toLowerCase();
      const ty = String(s.getAttribute('type') || '').toLowerCase();
      if (tg === 'input' && ty === 'file') return s;
      inner = fileIn(s) || fileInSubtreeBfs(s, 400);
      if (inner) return inner;
      s = s.previousElementSibling;
    }
    s = node.nextElementSibling;
    while (s) {
      const tg = s.tagName && s.tagName.toLowerCase();
      const ty = String(s.getAttribute('type') || '').toLowerCase();
      if (tg === 'input' && ty === 'file') return s;
      inner = fileIn(s) || fileInSubtreeBfs(s, 400);
      if (inner) return inner;
      s = s.nextElementSibling;
    }
    let p = node.parentElement;
    for (let d = 0; d < 28 && p; d++) {
      inner = fileIn(p) || fileInSubtreeBfs(p, 600);
      if (inner) return inner;
      p = p.parentElement;
    }
    return null;
  }
  const fi = find(el);
  if (!fi) return null;
  if (fi.id && typeof fi.id === 'string' && fi.id.trim()) {
    const id = fi.id.trim();
    return '#' + (typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(id) : id);
  }
  const tid = fi.getAttribute('data-testid') || fi.getAttribute('data-test-id') || fi.getAttribute('data-qa');
  if (tid) return '[data-testid=' + JSON.stringify(tid) + ']';
  const nm = fi.getAttribute('name');
  if (nm) return 'input[type=file][name=' + JSON.stringify(nm) + ']';
  const al = fi.getAttribute('aria-label');
  if (al) return 'input[type=file][aria-label=' + JSON.stringify(al) + ']';
  const parts = [];
  let cur = fi;
  while (cur && cur.nodeType === 1) {
    let idx = 1;
    let sb = cur.previousElementSibling;
    while (sb) {
      if (sb.tagName === cur.tagName) idx++;
      sb = sb.previousElementSibling;
    }
    const t = cur.tagName.toLowerCase();
    parts.unshift(idx > 1 ? t + '[' + idx + ']' : t);
    cur = cur.parentElement;
  }
  return 'xpath=/' + parts.join('/');
}
"""


def _upload_playwright_selector_candidates(original: str) -> list[str]:
    """Stable upload targets for flaky recordings (duplicate ids, ``>> nth`` on inputs, inner text nodes)."""
    o = (original or "").strip()
    out: list[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        t = s.strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)

    add(o)
    # ``#gql_get_resume >> nth=0`` — ``#id`` is an <input>, chaining is meaningless; strip chain.
    m_id_chain = re.match(r"^(#[A-Za-z0-9_-]+)\s*>>\s*nth=\d+$", o)
    if m_id_chain:
        add(m_id_chain.group(1))
    # Prefer the dropzone root over ``[data-testid=upload-area] >> p`` / ``>> span``.
    m_area = re.search(r'(\[data-testid=["\']upload-area["\']\])', o)
    if m_area and ">>" in o:
        add(m_area.group(1))
    lo = o.lower()
    # ``upload-file__upload-area`` does not contain the substring ``upload-area``; still treat as dropzone.
    m_tid = re.search(r'data-testid\s*=\s*["\']([^"\']+)["\']', o, flags=re.I)
    uploadish_dropzone = False
    if m_tid:
        tid_l = m_tid.group(1).lower()
        if "upload" in tid_l and ("area" in tid_l or "drop" in tid_l or "zone" in tid_l or "file" in tid_l):
            uploadish_dropzone = True
            token = json.dumps(m_tid.group(1))
            add(f"[data-testid={token}] input[type=file]")
            add(f"[data-testid={token}] >> input[type=file]")
    if (
        "gql_get_resume" in lo
        or "upload-area" in o
        or "upload-file" in lo
        or uploadish_dropzone
        or ("upload" in lo and "data-testid" in lo and ("area" in lo or "file" in lo))
    ):
        add("input[type=file]>>nth=0")
    return out


class ToolRuntime:
    def __init__(
        self,
        browser_session: BrowserSession,
        *,
        snapshot_engine: SnapshotEngine | None = None,
        event_emitter: ToolEventEmitter | None = None,
    ) -> None:
        self._logger = get_logger(__name__)
        self._browser_session = browser_session
        self._snapshot_engine = snapshot_engine
        self._event_emitter = event_emitter
        self._active_frame_paths: dict[str, list[str]] = {}
        self._selected_tab_id: str | None = None
        self._console_events: dict[str, list[dict[str, Any]]] = {}
        self._network_events: dict[str, list[dict[str, Any]]] = {}
        self._instrumented_tabs: set[str] = set()

    async def navigate(
        self,
        *,
        tab_id: str,
        url: str,
        wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = "load",
        timeout_ms: float | None = None,
    ) -> NavigateResult:
        async def _impl() -> NavigateResult:
            page = self._require_page(tab_id)
            response = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            frame_path = self._current_frame_path(tab_id, page)
            return NavigateResult(
                tool="navigate",
                tabId=tab_id,
                framePath=frame_path,
                url=page.url,
                statusCode=response.status if response else None,
            )

        return await self._run_tool("navigate", tab_id, {"url": url}, _impl)

    async def navigate_back(
        self,
        *,
        tab_id: str,
        wait_until: Literal["commit", "domcontentloaded", "load", "networkidle"] = "load",
        timeout_ms: float | None = None,
    ) -> NavigateResult:
        async def _impl() -> NavigateResult:
            page = self._require_page(tab_id)
            response = await page.go_back(wait_until=wait_until, timeout=timeout_ms)
            frame_path = self._current_frame_path(tab_id, page)
            return NavigateResult(
                tool="navigate_back",
                tabId=tab_id,
                framePath=frame_path,
                url=page.url,
                statusCode=response.status if response else None,
            )

        return await self._run_tool("navigate_back", tab_id, {}, _impl)

    async def wait_for(
        self,
        *,
        tab_id: str,
        target: str | None = None,
        state: Literal["attached", "detached", "visible", "hidden"] = "visible",
        timeout_ms: float = 30_000,
    ) -> WaitResult:
        async def _impl() -> WaitResult:
            page = self._require_page(tab_id)
            if target is None:
                await page.wait_for_load_state(state="load", timeout=timeout_ms)
                frame_path = self._current_frame_path(tab_id, page)
                return WaitResult(
                    tool="wait_for",
                    tabId=tab_id,
                    framePath=frame_path,
                    waitedMs=int(timeout_ms),
                    details={"condition": "load_state"},
                )

            locator, frame_path = await self._resolve_target_locator(tab_id, target)
            await locator.wait_for(state=state, timeout=timeout_ms)
            return WaitResult(
                tool="wait_for",
                tabId=tab_id,
                framePath=frame_path,
                waitedMs=int(timeout_ms),
                details={"target": target, "state": state},
            )

        return await self._run_tool(
            "wait_for",
            tab_id,
            {"target": target, "state": state, "timeout_ms": timeout_ms},
            _impl,
        )

    async def wait_timeout(self, *, tab_id: str, timeout_ms: int) -> WaitResult:
        async def _impl() -> WaitResult:
            page = self._require_page(tab_id)
            await page.wait_for_timeout(timeout_ms)
            frame_path = self._current_frame_path(tab_id, page)
            return WaitResult(
                tool="wait_timeout",
                tabId=tab_id,
                framePath=frame_path,
                waitedMs=timeout_ms,
            )

        return await self._run_tool("wait_timeout", tab_id, {"timeout_ms": timeout_ms}, _impl)

    async def click(
        self,
        *,
        tab_id: str,
        target: str,
        button: Literal["left", "middle", "right"] = "left",
        timeout_ms: float = 30_000,
    ) -> InteractionResult:
        async def _impl() -> InteractionResult:
            locator, frame_path = await self._resolve_target_locator(tab_id, target)
            await locator.click(button=button, timeout=timeout_ms)
            return InteractionResult(
                tool="click",
                tabId=tab_id,
                framePath=frame_path,
                target=target,
                details={"button": button},
            )

        return await self._run_tool(
            "click",
            tab_id,
            {"target": target, "button": button},
            _impl,
        )

    async def fill(
        self,
        *,
        tab_id: str,
        target: str,
        text: str,
        timeout_ms: float = 30_000,
    ) -> InteractionResult:
        async def _impl() -> InteractionResult:
            locator, frame_path = await self._resolve_target_locator(tab_id, target)
            await locator.fill(text, timeout=timeout_ms)
            return InteractionResult(
                tool="fill",
                tabId=tab_id,
                framePath=frame_path,
                target=target,
                details={"text_length": len(text)},
            )

        return await self._run_tool("fill", tab_id, {"target": target}, _impl)

    async def type(
        self,  # noqa: A003
        *,
        tab_id: str,
        target: str,
        text: str,
        delay_ms: float = 0,
        timeout_ms: float = 30_000,
    ) -> InteractionResult:
        async def _impl() -> InteractionResult:
            locator, frame_path = await self._resolve_target_locator(tab_id, target)
            await locator.type(text, delay=delay_ms, timeout=timeout_ms)
            return InteractionResult(
                tool="type",
                tabId=tab_id,
                framePath=frame_path,
                target=target,
                details={"text_length": len(text), "delay_ms": delay_ms},
            )

        return await self._run_tool("type", tab_id, {"target": target}, _impl)

    async def press(
        self,
        *,
        tab_id: str,
        key: str,
        target: str | None = None,
        timeout_ms: float = 30_000,
    ) -> InteractionResult:
        async def _impl() -> InteractionResult:
            page = self._require_page(tab_id)
            frame_path = self._current_frame_path(tab_id, page)
            if target is None:
                await page.keyboard.press(key)
                return InteractionResult(
                    tool="press",
                    tabId=tab_id,
                    framePath=frame_path,
                    target="keyboard",
                    details={"key": key},
                )

            locator, frame_path = await self._resolve_target_locator(tab_id, target)
            await locator.press(key, timeout=timeout_ms)
            return InteractionResult(
                tool="press",
                tabId=tab_id,
                framePath=frame_path,
                target=target,
                details={"key": key},
            )

        return await self._run_tool("press", tab_id, {"target": target, "key": key}, _impl)

    async def check(
        self,
        *,
        tab_id: str,
        target: str,
        timeout_ms: float = 30_000,
    ) -> InteractionResult:
        async def _impl() -> InteractionResult:
            locator, frame_path = await self._resolve_target_locator(tab_id, target)
            await locator.check(timeout=timeout_ms)
            return InteractionResult(
                tool="check",
                tabId=tab_id,
                framePath=frame_path,
                target=target,
            )

        return await self._run_tool("check", tab_id, {"target": target}, _impl)

    async def uncheck(
        self,
        *,
        tab_id: str,
        target: str,
        timeout_ms: float = 30_000,
    ) -> InteractionResult:
        async def _impl() -> InteractionResult:
            locator, frame_path = await self._resolve_target_locator(tab_id, target)
            await locator.uncheck(timeout=timeout_ms)
            return InteractionResult(
                tool="uncheck",
                tabId=tab_id,
                framePath=frame_path,
                target=target,
            )

        return await self._run_tool("uncheck", tab_id, {"target": target}, _impl)

    async def select(
        self,
        *,
        tab_id: str,
        target: str,
        value: str | list[str],
        timeout_ms: float = 30_000,
    ) -> InteractionResult:
        async def _impl() -> InteractionResult:
            locator, frame_path = await self._resolve_target_locator(tab_id, target)
            selected = await locator.select_option(value=value, timeout=timeout_ms)
            return InteractionResult(
                tool="select",
                tabId=tab_id,
                framePath=frame_path,
                target=target,
                details={"selected": selected},
            )

        return await self._run_tool("select", tab_id, {"target": target, "value": value}, _impl)

    async def upload(
        self,
        *,
        tab_id: str,
        target: str,
        file_paths: str | list[str],
        timeout_ms: float = 30_000,
    ) -> InteractionResult:
        async def _impl() -> InteractionResult:
            raw_paths = file_paths if isinstance(file_paths, list) else [file_paths]
            normalized_paths = [
                str(Path(unquote(str(p).strip())).expanduser()) for p in raw_paths if str(p).strip()
            ]
            missing = [p for p in normalized_paths if not Path(p).is_file()]
            if missing:
                raise BrowserSessionError(
                    "upload: file path is not an existing file (fix path or spaces vs literal %20 in the filename): "
                    + "; ".join(missing[:4])
                )
            page = self._require_page(tab_id)

            async def _try_upload_with_resolved_locator(
                use_target: str,
            ) -> InteractionResult:
                locator, frame_path = await self._resolve_target_locator(tab_id, use_target)
                frame = self._browser_session.resolve_frame_path(frame_path)
                if frame is None:
                    frame = page.main_frame

                upload_method = "setInputFiles"
                base_details: dict[str, Any] = {
                    "file_count": len(normalized_paths),
                    "selectorAttempt": use_target,
                }

                # Custom upload UIs: map trigger → real <input type=file> (in-page).
                try:
                    if await locator.count() > 0:
                        sel_js = await locator.first.evaluate(_UPLOAD_FILE_SELECTOR_FROM_TRIGGER_JS)
                        if isinstance(sel_js, str) and sel_js.strip():
                            cand = sel_js.strip()
                            if cand != use_target.strip():
                                file_loc = frame.locator(cand).first
                                if await file_loc.count() > 0:
                                    await file_loc.set_input_files(normalized_paths, timeout=timeout_ms)
                                    return InteractionResult(
                                        tool="upload",
                                        tabId=tab_id,
                                        framePath=frame_path,
                                        target=target,
                                        details={
                                            **base_details,
                                            "uploadMethod": "js_resolved_file_input",
                                            "resolvedSelector": cand,
                                        },
                                    )
                except Exception:  # noqa: BLE001
                    pass

                try:
                    await locator.set_input_files(normalized_paths, timeout=timeout_ms)
                except Exception:
                    inner = locator.locator('input[type="file"],input[type=file]').first
                    try:
                        if await inner.count() > 0:
                            await inner.set_input_files(normalized_paths, timeout=timeout_ms)
                            upload_method = "nested_file_input"
                        else:
                            raise LookupError("no nested file input under locator")
                    except Exception:
                        assoc = await self._locator_associated_file_input(locator)
                        if assoc is not None:
                            await assoc.set_input_files(normalized_paths, timeout=timeout_ms)
                            upload_method = "ancestor_file_input"
                        else:
                            fc_timeout = min(float(timeout_ms), 20_000.0)
                            click_timeout = min(float(timeout_ms), 15_000.0)
                            last_fc_exc: Exception | None = None
                            for force in (False, True):
                                try:
                                    async with page.expect_file_chooser(timeout=fc_timeout) as fc_info:
                                        await locator.scroll_into_view_if_needed(timeout=click_timeout)
                                        await locator.click(timeout=click_timeout, force=force)
                                    chooser = await fc_info.value
                                    await chooser.set_files(normalized_paths)
                                    upload_method = "fileChooser" if not force else "fileChooser_force"
                                    break
                                except Exception as exc:  # noqa: BLE001
                                    last_fc_exc = exc
                            else:
                                if last_fc_exc is not None:
                                    raise last_fc_exc
                                raise BrowserSessionError("upload: file chooser path failed")
                return InteractionResult(
                    tool="upload",
                    tabId=tab_id,
                    framePath=frame_path,
                    target=target,
                    details={**base_details, "uploadMethod": upload_method},
                )

            variants = _upload_playwright_selector_candidates(target)
            last_exc: Exception | None = None
            for use in variants:
                try:
                    return await _try_upload_with_resolved_locator(use)
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
            if last_exc is not None:
                raise last_exc
            raise BrowserSessionError("upload: no selector candidates")

        return await self._run_tool(
            "upload",
            tab_id,
            {"target": target, "file_paths": file_paths},
            _impl,
        )

    async def drag(
        self,
        *,
        tab_id: str,
        start_target: str,
        end_target: str,
        timeout_ms: float = 30_000,
    ) -> InteractionResult:
        async def _impl() -> InteractionResult:
            start_locator, frame_path = await self._resolve_target_locator(tab_id, start_target)
            end_locator, _ = await self._resolve_target_locator(tab_id, end_target)
            await start_locator.drag_to(end_locator, timeout=timeout_ms)
            return InteractionResult(
                tool="drag",
                tabId=tab_id,
                framePath=frame_path,
                target=start_target,
                details={"end_target": end_target},
            )

        return await self._run_tool(
            "drag",
            tab_id,
            {"start_target": start_target, "end_target": end_target},
            _impl,
        )

    async def hover(
        self,
        *,
        tab_id: str,
        target: str,
        timeout_ms: float = 30_000,
    ) -> InteractionResult:
        async def _impl() -> InteractionResult:
            locator, frame_path = await self._resolve_target_locator(tab_id, target)
            await locator.hover(timeout=timeout_ms)
            return InteractionResult(
                tool="hover",
                tabId=tab_id,
                framePath=frame_path,
                target=target,
            )

        return await self._run_tool("hover", tab_id, {"target": target}, _impl)

    async def focus(
        self,
        *,
        tab_id: str,
        target: str,
        timeout_ms: float = 30_000,
    ) -> InteractionResult:
        async def _impl() -> InteractionResult:
            locator, frame_path = await self._resolve_target_locator(tab_id, target)
            await locator.focus(timeout=timeout_ms)
            return InteractionResult(
                tool="focus",
                tabId=tab_id,
                framePath=frame_path,
                target=target,
            )

        return await self._run_tool("focus", tab_id, {"target": target}, _impl)

    async def tabs_list(self, *, tab_id: str) -> ToolResult:
        async def _impl() -> ToolResult:
            tab_ids = self._browser_session.list_tab_ids()
            items: list[dict[str, Any]] = []
            for current_tab_id in tab_ids:
                page = self._browser_session.get_tab(current_tab_id)
                if page is None:
                    continue
                title = ""
                try:
                    title = await page.title()
                except Exception:  # noqa: BLE001
                    title = ""
                items.append({"tabId": current_tab_id, "url": page.url, "title": title})

            return ToolResult(
                tool="tabs_list",
                tabId=tab_id,
                framePath=self._active_frame_paths.get(tab_id, []),
                details={
                    "tabs": items,
                    "selectedTabId": self._selected_tab_id,
                    "count": len(items),
                },
            )

        return await self._run_tool("tabs_list", tab_id, {}, _impl)

    async def tabs_select(self, *, tab_id: str) -> ToolResult:
        async def _impl() -> ToolResult:
            page = self._require_page(tab_id)
            self._selected_tab_id = tab_id
            frame_path = self._browser_session.get_frame_path(page.main_frame)
            self._active_frame_paths[tab_id] = frame_path
            return ToolResult(
                tool="tabs_select",
                tabId=tab_id,
                framePath=frame_path,
                details={"selectedTabId": tab_id, "url": page.url},
            )

        return await self._run_tool("tabs_select", tab_id, {}, _impl)

    async def tabs_close(
        self,
        *,
        tab_id: str,
        target_tab_id: str | None = None,
    ) -> ToolResult:
        async def _impl() -> ToolResult:
            close_tab_id = target_tab_id or tab_id
            page = self._browser_session.get_tab(close_tab_id)
            if page is None:
                msg = f"Unknown tab id: {close_tab_id}"
                raise BrowserSessionError(msg)

            await page.close()
            remaining_tabs = self._browser_session.list_tab_ids()
            if self._selected_tab_id == close_tab_id:
                self._selected_tab_id = remaining_tabs[0] if remaining_tabs else None

            self._console_events.pop(close_tab_id, None)
            self._network_events.pop(close_tab_id, None)
            self._instrumented_tabs.discard(close_tab_id)
            self._active_frame_paths.pop(close_tab_id, None)

            return ToolResult(
                tool="tabs_close",
                tabId=tab_id,
                framePath=self._active_frame_paths.get(tab_id, []),
                details={
                    "closedTabId": close_tab_id,
                    "remainingTabIds": remaining_tabs,
                    "selectedTabId": self._selected_tab_id,
                },
            )

        return await self._run_tool(
            "tabs_close",
            tab_id,
            {"target_tab_id": target_tab_id},
            _impl,
        )

    async def console_messages(
        self,
        *,
        tab_id: str,
        min_level: Literal["verbose", "log", "info", "warning", "error"] = "verbose",
        clear: bool = False,
        limit: int = 200,
    ) -> ToolResult:
        async def _impl() -> ToolResult:
            self._require_page(tab_id)
            all_entries = self._console_events.get(tab_id, [])
            filtered = _filter_console_messages(all_entries, min_level=min_level)
            if limit >= 0:
                filtered = filtered[-limit:]
            if clear:
                self._console_events[tab_id] = []
            return ToolResult(
                tool="console_messages",
                tabId=tab_id,
                framePath=self._active_frame_paths.get(tab_id, []),
                details={"messages": filtered, "count": len(filtered), "cleared": clear},
            )

        return await self._run_tool(
            "console_messages",
            tab_id,
            {"min_level": min_level, "clear": clear, "limit": limit},
            _impl,
        )

    async def network_requests(
        self,
        *,
        tab_id: str,
        clear: bool = False,
        limit: int = 200,
    ) -> ToolResult:
        async def _impl() -> ToolResult:
            self._require_page(tab_id)
            entries = self._network_events.get(tab_id, [])
            if limit >= 0:
                entries = entries[-limit:]
            if clear:
                self._network_events[tab_id] = []
            return ToolResult(
                tool="network_requests",
                tabId=tab_id,
                framePath=self._active_frame_paths.get(tab_id, []),
                details={"requests": entries, "count": len(entries), "cleared": clear},
            )

        return await self._run_tool(
            "network_requests",
            tab_id,
            {"clear": clear, "limit": limit},
            _impl,
        )

    async def screenshot(
        self,
        *,
        tab_id: str,
        path: str | None = None,
        full_page: bool = False,
        target: str | None = None,
    ) -> ToolResult:
        async def _impl() -> ToolResult:
            page = self._require_page(tab_id)
            frame_path = self._current_frame_path(tab_id, page)

            capture_target: Locator | Page
            if isinstance(target, str) and target.strip():
                locator, frame_path = await self._resolve_target_locator(tab_id, target)
                capture_target = locator
            else:
                capture_target = page

            output_path = None
            if path is not None:
                output_path = Path(path)
                output_path.parent.mkdir(parents=True, exist_ok=True)

            if isinstance(capture_target, Locator):
                image_bytes = await capture_target.screenshot(path=str(output_path) if output_path else None)
            else:
                image_bytes = await capture_target.screenshot(
                    path=str(output_path) if output_path else None,
                    full_page=full_page,
                )

            return ToolResult(
                tool="screenshot",
                tabId=tab_id,
                framePath=frame_path,
                details={
                    "path": str(output_path) if output_path else None,
                    "sizeBytes": len(image_bytes),
                    "fullPage": full_page,
                    "target": target,
                },
            )

        return await self._run_tool(
            "screenshot",
            tab_id,
            {"path": path, "full_page": full_page, "target": target},
            _impl,
        )

    async def take_trace(
        self,
        *,
        tab_id: str,
        path: str | None = None,
        title: str | None = None,
    ) -> ToolResult:
        async def _impl() -> ToolResult:
            context = self._resolve_tab_context(tab_id)
            output_path = Path(path) if path else _temp_trace_path()
            output_path.parent.mkdir(parents=True, exist_ok=True)

            await context.tracing.start(
                title=title or f"trace-{tab_id}",
                screenshots=True,
                snapshots=True,
            )
            await context.tracing.stop(path=str(output_path))
            return ToolResult(
                tool="take_trace",
                tabId=tab_id,
                framePath=self._active_frame_paths.get(tab_id, []),
                details={"path": str(output_path), "title": title or f"trace-{tab_id}"},
            )

        return await self._run_tool(
            "take_trace",
            tab_id,
            {"path": path, "title": title},
            _impl,
        )

    async def assert_visible(
        self,
        *,
        tab_id: str,
        target: str,
        timeout_ms: float = 30_000,
    ) -> AssertionResult:
        async def _impl() -> AssertionResult:
            locator, frame_path = await self._resolve_target_locator(tab_id, target)
            await locator.wait_for(state="visible", timeout=timeout_ms)
            actual_visible = await locator.is_visible()
            if not actual_visible:
                msg = f"Expected target '{target}' to be visible."
                raise AssertionError(msg)
            return AssertionResult(
                tool="assert_visible",
                tabId=tab_id,
                framePath=frame_path,
                assertion="visible",
                expected="true",
                actual=str(actual_visible).lower(),
            )

        return await self._run_tool("assert_visible", tab_id, {"target": target}, _impl)

    async def assert_text(
        self,
        *,
        tab_id: str,
        target: str,
        expected: str,
        contains: bool = True,
        timeout_ms: float = 30_000,
    ) -> AssertionResult:
        async def _impl() -> AssertionResult:
            locator, frame_path = await self._resolve_target_locator(tab_id, target)
            await locator.wait_for(state="visible", timeout=timeout_ms)
            actual = (await locator.inner_text()).strip()
            is_valid = expected in actual if contains else actual == expected
            if not is_valid:
                msg = f"Text assertion failed for target '{target}'. Expected '{expected}', got '{actual}'."
                raise AssertionError(msg)
            return AssertionResult(
                tool="assert_text",
                tabId=tab_id,
                framePath=frame_path,
                assertion="text_contains" if contains else "text_equals",
                expected=expected,
                actual=actual,
            )

        return await self._run_tool(
            "assert_text",
            tab_id,
            {"target": target, "expected": expected, "contains": contains},
            _impl,
        )

    async def assert_url(
        self,
        *,
        tab_id: str,
        expected: str,
        contains: bool = True,
    ) -> AssertionResult:
        async def _impl() -> AssertionResult:
            page = self._require_page(tab_id)
            frame_path = self._current_frame_path(tab_id, page)
            actual = page.url
            is_valid = expected in actual if contains else actual == expected
            if not is_valid:
                msg = f"URL assertion failed. Expected '{expected}', got '{actual}'."
                raise AssertionError(msg)
            return AssertionResult(
                tool="assert_url",
                tabId=tab_id,
                framePath=frame_path,
                assertion="url_contains" if contains else "url_equals",
                expected=expected,
                actual=actual,
            )

        return await self._run_tool(
            "assert_url",
            tab_id,
            {"expected": expected, "contains": contains},
            _impl,
        )

    async def assert_title(
        self,
        *,
        tab_id: str,
        expected: str,
        contains: bool = True,
    ) -> AssertionResult:
        async def _impl() -> AssertionResult:
            page = self._require_page(tab_id)
            frame_path = self._current_frame_path(tab_id, page)
            actual = await page.title()
            is_valid = expected in actual if contains else actual == expected
            if not is_valid:
                msg = f"Title assertion failed. Expected '{expected}', got '{actual}'."
                raise AssertionError(msg)
            return AssertionResult(
                tool="assert_title",
                tabId=tab_id,
                framePath=frame_path,
                assertion="title_contains" if contains else "title_equals",
                expected=expected,
                actual=actual,
            )

        return await self._run_tool(
            "assert_title",
            tab_id,
            {"expected": expected, "contains": contains},
            _impl,
        )

    async def assert_value(
        self,
        *,
        tab_id: str,
        target: str,
        expected: str,
        contains: bool = False,
        timeout_ms: float = 30_000,
    ) -> AssertionResult:
        async def _impl() -> AssertionResult:
            locator, frame_path = await self._resolve_target_locator(tab_id, target)
            await locator.wait_for(state="attached", timeout=timeout_ms)
            actual = await locator.input_value(timeout=timeout_ms)
            is_valid = expected in actual if contains else actual == expected
            if not is_valid:
                msg = f"Value assertion failed for target '{target}'. Expected '{expected}', got '{actual}'."
                raise AssertionError(msg)
            return AssertionResult(
                tool="assert_value",
                tabId=tab_id,
                framePath=frame_path,
                assertion="value_contains" if contains else "value_equals",
                expected=expected,
                actual=actual,
            )

        return await self._run_tool(
            "assert_value",
            tab_id,
            {"target": target, "expected": expected, "contains": contains},
            _impl,
        )

    async def assert_checked(
        self,
        *,
        tab_id: str,
        target: str,
        expected: bool = True,
        timeout_ms: float = 30_000,
    ) -> AssertionResult:
        async def _impl() -> AssertionResult:
            locator, frame_path = await self._resolve_target_locator(tab_id, target)
            await locator.wait_for(state="attached", timeout=timeout_ms)
            actual_checked = await locator.is_checked()
            if actual_checked != expected:
                msg = (
                    f"Checked assertion failed for target '{target}'. "
                    f"Expected '{str(expected).lower()}', got '{str(actual_checked).lower()}'."
                )
                raise AssertionError(msg)
            return AssertionResult(
                tool="assert_checked",
                tabId=tab_id,
                framePath=frame_path,
                assertion="checked_equals",
                expected=str(expected).lower(),
                actual=str(actual_checked).lower(),
            )

        return await self._run_tool(
            "assert_checked",
            tab_id,
            {"target": target, "expected": expected},
            _impl,
        )

    async def assert_enabled(
        self,
        *,
        tab_id: str,
        target: str,
        expected: bool = True,
        timeout_ms: float = 30_000,
    ) -> AssertionResult:
        async def _impl() -> AssertionResult:
            locator, frame_path = await self._resolve_target_locator(tab_id, target)
            await locator.wait_for(state="attached", timeout=timeout_ms)
            actual_enabled = await locator.is_enabled()
            if actual_enabled != expected:
                msg = (
                    f"Enabled assertion failed for target '{target}'. "
                    f"Expected '{str(expected).lower()}', got '{str(actual_enabled).lower()}'."
                )
                raise AssertionError(msg)
            return AssertionResult(
                tool="assert_enabled",
                tabId=tab_id,
                framePath=frame_path,
                assertion="enabled_equals",
                expected=str(expected).lower(),
                actual=str(actual_enabled).lower(),
            )

        return await self._run_tool(
            "assert_enabled",
            tab_id,
            {"target": target, "expected": expected},
            _impl,
        )

    async def assert_hidden(
        self,
        *,
        tab_id: str,
        target: str,
        timeout_ms: float = 30_000,
    ) -> AssertionResult:
        async def _impl() -> AssertionResult:
            locator, frame_path = await self._resolve_target_locator(tab_id, target)
            await locator.wait_for(state="hidden", timeout=timeout_ms)
            actual_visible = await locator.is_visible()
            if actual_visible:
                msg = f"Hidden assertion failed for target '{target}'; target is still visible."
                raise AssertionError(msg)
            return AssertionResult(
                tool="assert_hidden",
                tabId=tab_id,
                framePath=frame_path,
                assertion="hidden",
                expected="true",
                actual=str(not actual_visible).lower(),
            )

        return await self._run_tool("assert_hidden", tab_id, {"target": target}, _impl)

    async def assert_count(
        self,
        *,
        tab_id: str,
        target: str,
        expected_count: int,
    ) -> AssertionResult:
        async def _impl() -> AssertionResult:
            locator, frame_path = await self._resolve_target_locator(tab_id, target, first=False)
            actual_count = await locator.count()
            if actual_count != expected_count:
                msg = f"Count assertion failed for target '{target}'. Expected {expected_count}, got {actual_count}."
                raise AssertionError(msg)
            return AssertionResult(
                tool="assert_count",
                tabId=tab_id,
                framePath=frame_path,
                assertion="count_equals",
                expected=str(expected_count),
                actual=str(actual_count),
            )

        return await self._run_tool(
            "assert_count",
            tab_id,
            {"target": target, "expected_count": expected_count},
            _impl,
        )

    async def assert_in_viewport(
        self,
        *,
        tab_id: str,
        target: str,
        timeout_ms: float = 30_000,
    ) -> AssertionResult:
        async def _impl() -> AssertionResult:
            locator, frame_path = await self._resolve_target_locator(tab_id, target)
            await locator.wait_for(state="attached", timeout=timeout_ms)
            in_viewport = await locator.evaluate(
                """
                (element) => {
                    const rect = element.getBoundingClientRect();
                    const vw = window.innerWidth || document.documentElement.clientWidth;
                    const vh = window.innerHeight || document.documentElement.clientHeight;
                    return (
                        rect.width > 0 &&
                        rect.height > 0 &&
                        rect.bottom > 0 &&
                        rect.right > 0 &&
                        rect.top < vh &&
                        rect.left < vw
                    );
                }
                """
            )
            if not bool(in_viewport):
                msg = f"In-viewport assertion failed for target '{target}'."
                raise AssertionError(msg)
            return AssertionResult(
                tool="assert_in_viewport",
                tabId=tab_id,
                framePath=frame_path,
                assertion="in_viewport",
                expected="true",
                actual=str(bool(in_viewport)).lower(),
            )

        return await self._run_tool("assert_in_viewport", tab_id, {"target": target}, _impl)

    async def dialog_handle(
        self,
        *,
        tab_id: str,
        accept: bool = True,
        prompt_text: str | None = None,
        timeout_ms: int = 10_000,
    ) -> DialogResult:
        async def _impl() -> DialogResult:
            page = self._require_page(tab_id)
            frame_path = self._current_frame_path(tab_id, page)

            dialog = await self._wait_for_dialog(page, timeout_ms=timeout_ms)
            if accept:
                await dialog.accept(prompt_text=prompt_text)
            else:
                await dialog.dismiss()

            return DialogResult(
                tool="dialog_handle",
                tabId=tab_id,
                framePath=frame_path,
                accepted=accept,
                dialogType=dialog.type,
                dialogMessage=dialog.message,
            )

        return await self._run_tool(
            "dialog_handle",
            tab_id,
            {"accept": accept, "has_prompt_text": prompt_text is not None},
            _impl,
        )

    async def frame_enter(self, *, tab_id: str, target: str) -> FrameContextResult:
        async def _impl() -> FrameContextResult:
            page = self._require_page(tab_id)
            if target.startswith("frame:"):
                frame_id = target.split(":", 1)[1]
                frame = self._browser_session.get_frame(frame_id)
                if frame is None:
                    msg = f"Unknown frame id: {frame_id}"
                    raise BrowserSessionError(msg)
            else:
                locator, _ = await self._resolve_target_locator(tab_id, target)
                handle = await locator.element_handle()
                if handle is None:
                    msg = f"Could not resolve frame host element for target '{target}'."
                    raise BrowserSessionError(msg)
                frame = await handle.content_frame()
                if frame is None:
                    msg = f"Target '{target}' is not an iframe/frame element."
                    raise BrowserSessionError(msg)

            frame_id = self._browser_session.get_frame_id(frame)
            if frame_id is None:
                msg = "Resolved frame has no tracked id in BrowserSession."
                raise BrowserSessionError(msg)

            frame_path = self._browser_session.get_frame_path(frame)
            if not frame_path:
                frame_path = self._current_frame_path(tab_id, page)
            self._active_frame_paths[tab_id] = frame_path
            return FrameContextResult(
                tool="frame_enter",
                tabId=tab_id,
                framePath=frame_path,
                frameId=frame_id,
            )

        return await self._run_tool("frame_enter", tab_id, {"target": target}, _impl)

    async def frame_exit(self, *, tab_id: str) -> FrameContextResult:
        async def _impl() -> FrameContextResult:
            page = self._require_page(tab_id)
            current_path = self._active_frame_paths.get(tab_id)
            if not current_path or len(current_path) <= 1:
                main_path = self._browser_session.get_frame_path(page.main_frame)
                self._active_frame_paths[tab_id] = main_path
                return FrameContextResult(
                    tool="frame_exit",
                    tabId=tab_id,
                    framePath=main_path,
                    frameId=main_path[-1] if main_path else None,
                )

            parent_path = current_path[:-1]
            parent_frame = self._browser_session.resolve_frame_path(parent_path)
            if parent_frame is None:
                main_path = self._browser_session.get_frame_path(page.main_frame)
                self._active_frame_paths[tab_id] = main_path
                return FrameContextResult(
                    tool="frame_exit",
                    tabId=tab_id,
                    framePath=main_path,
                    frameId=main_path[-1] if main_path else None,
                    details={"fallback": "main_frame"},
                )

            self._active_frame_paths[tab_id] = parent_path
            frame_id = self._browser_session.get_frame_id(parent_frame)
            return FrameContextResult(
                tool="frame_exit",
                tabId=tab_id,
                framePath=parent_path,
                frameId=frame_id,
            )

        return await self._run_tool("frame_exit", tab_id, {}, _impl)

    def _require_page(self, tab_id: str) -> Page:
        page = self._browser_session.get_tab(tab_id)
        if page is None:
            msg = f"Unknown tab id: {tab_id}"
            raise BrowserSessionError(msg)
        self._instrument_tab_observability(tab_id, page)
        return page

    def _resolve_tab_context(self, tab_id: str) -> BrowserContext:
        context_id = self._browser_session.get_tab_context_id(tab_id)
        if context_id is None:
            msg = f"No browser context tracked for tab id: {tab_id}"
            raise BrowserSessionError(msg)
        context = self._browser_session.get_context(context_id)
        if context is None:
            msg = f"Context '{context_id}' for tab '{tab_id}' is not available."
            raise BrowserSessionError(msg)
        return context

    def _current_frame_path(self, tab_id: str, page: Page) -> list[str]:
        active_path = self._active_frame_paths.get(tab_id)
        if active_path:
            frame = self._browser_session.resolve_frame_path(active_path)
            if frame is not None:
                return list(active_path)

        main_path = self._browser_session.get_frame_path(page.main_frame)
        self._active_frame_paths[tab_id] = main_path
        return list(main_path)

    def _current_frame(self, tab_id: str, page: Page) -> tuple[Frame, list[str]]:
        frame_path = self._current_frame_path(tab_id, page)
        frame = self._browser_session.resolve_frame_path(frame_path)
        if frame is None:
            return page.main_frame, self._browser_session.get_frame_path(page.main_frame)
        return frame, frame_path

    async def _resolve_target_locator(
        self,
        tab_id: str,
        target: str,
        *,
        first: bool = True,
    ) -> tuple[Locator, list[str]]:
        page = self._require_page(tab_id)
        if self._snapshot_engine:
            ref = target.split(":", 1)[1] if target.startswith("ref:") else target
            binding = self._snapshot_engine.get_ref_binding(ref)
            if binding is not None:
                ref_tab_id, frame_path, selector = binding
                if ref_tab_id != tab_id:
                    msg = f"Ref '{ref}' belongs to a different tab: {ref_tab_id}"
                    raise BrowserSessionError(msg)

                frame = self._browser_session.resolve_frame_path(frame_path)
                if frame is None:
                    msg = f"Ref '{ref}' points to a detached frame path."
                    raise BrowserSessionError(msg)
                locator = frame.locator(selector)
                return (locator.first if first else locator), frame_path

        frame, frame_path = self._current_frame(tab_id, page)
        locator = frame.locator(target)
        return (locator.first if first else locator), frame_path

    async def _locator_associated_file_input(self, start: Locator) -> Locator | None:
        """Find ``input[type=file]`` under ``start``, adjacent siblings, or any ancestor subtree."""
        for rel in (
            'xpath=preceding-sibling::input[@type="file"][1]',
            'xpath=following-sibling::input[@type="file"][1]',
        ):
            try:
                sib = start.locator(rel)
                if await sib.count() > 0:
                    return sib.first
            except Exception:  # noqa: BLE001
                continue
        node: Locator = start
        for _ in range(14):
            bucket = node.locator('input[type="file"],input[type=file]')
            if await bucket.count() > 0:
                return bucket.first
            parent = node.locator("xpath=..").first
            if await parent.count() == 0:
                break
            node = parent
        return None

    async def probe_selector(
        self,
        *,
        tab_id: str,
        selector: str,
        state: Literal["attached", "detached", "visible", "hidden"] = "visible",
        timeout_ms: float = 2500,
    ) -> tuple[bool, str | None]:
        """Resolve one selector and wait for ``state`` without emitting tool_call audit events."""
        try:
            locator, _ = await self._resolve_target_locator(tab_id, selector)
            await locator.wait_for(state=state, timeout=timeout_ms)
            return True, None
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def _run_tool(
        self,
        tool_name: str,
        tab_id: str,
        input_payload: dict[str, Any],
        fn: Callable[[], Awaitable[ToolResult]],
    ) -> ToolResult:
        await self._emit_tool_call(tool_name, tab_id, "started", input_payload)
        try:
            result = await fn()
        except Exception as exc:
            await self._emit_tool_call(
                tool_name,
                tab_id,
                "failed",
                {"error": str(exc), **input_payload},
            )
            raise

        await self._emit_tool_call(
            tool_name,
            tab_id,
            "succeeded",
            {"result": result.model_dump(by_alias=True), **input_payload},
        )
        return result

    async def _emit_tool_call(
        self,
        tool_name: str,
        tab_id: str,
        status: Literal["started", "succeeded", "failed"],
        payload: dict[str, Any],
    ) -> None:
        event = ToolCallEvent(
            tool=tool_name,
            tabId=tab_id,
            status=status,
            payload=payload,
        )
        self._logger.info(
            "tool_call",
            tool=tool_name,
            tab_id=tab_id,
            status=status,
            payload=payload,
        )
        if self._event_emitter is None:
            return

        maybe_awaitable = self._event_emitter(event)
        if asyncio.iscoroutine(maybe_awaitable):
            await maybe_awaitable

    async def _wait_for_dialog(self, page: Page, *, timeout_ms: int) -> Dialog:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Dialog] = loop.create_future()

        def _on_dialog(dialog: Dialog) -> None:
            if not future.done():
                future.set_result(dialog)

        page.once("dialog", _on_dialog)
        return await asyncio.wait_for(future, timeout=timeout_ms / 1000)

    def _instrument_tab_observability(self, tab_id: str, page: Page) -> None:
        if tab_id in self._instrumented_tabs:
            return
        self._instrumented_tabs.add(tab_id)
        self._console_events.setdefault(tab_id, [])
        self._network_events.setdefault(tab_id, [])

        page.on(
            "console",
            lambda message: self._console_events[tab_id].append(
                {
                    "type": message.type,
                    "text": message.text,
                    "location": message.location,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            ),
        )
        page.on(
            "request",
            lambda request: self._network_events[tab_id].append(
                {
                    "phase": "request",
                    "url": request.url,
                    "method": request.method,
                    "resourceType": request.resource_type,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            ),
        )
        page.on(
            "response",
            lambda response: self._network_events[tab_id].append(
                {
                    "phase": "response",
                    "url": response.url,
                    "status": response.status,
                    "ok": response.ok,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            ),
        )
        page.on(
            "requestfailed",
            lambda request: self._network_events[tab_id].append(
                {
                    "phase": "failed",
                    "url": request.url,
                    "method": request.method,
                    "failure": request.failure,
                    "timestamp": datetime.now(UTC).isoformat(),
                }
            ),
        )


async def navigate(runtime: ToolRuntime, **kwargs: Any) -> NavigateResult:
    return await runtime.navigate(**kwargs)


async def navigate_back(runtime: ToolRuntime, **kwargs: Any) -> NavigateResult:
    return await runtime.navigate_back(**kwargs)


async def wait_for(runtime: ToolRuntime, **kwargs: Any) -> WaitResult:
    return await runtime.wait_for(**kwargs)


async def wait_timeout(runtime: ToolRuntime, **kwargs: Any) -> WaitResult:
    return await runtime.wait_timeout(**kwargs)


async def click(runtime: ToolRuntime, **kwargs: Any) -> InteractionResult:
    return await runtime.click(**kwargs)


async def fill(runtime: ToolRuntime, **kwargs: Any) -> InteractionResult:
    return await runtime.fill(**kwargs)


async def type(runtime: ToolRuntime, **kwargs: Any) -> InteractionResult:  # noqa: A001
    return await runtime.type(**kwargs)


async def press(runtime: ToolRuntime, **kwargs: Any) -> InteractionResult:
    return await runtime.press(**kwargs)


async def check(runtime: ToolRuntime, **kwargs: Any) -> InteractionResult:
    return await runtime.check(**kwargs)


async def uncheck(runtime: ToolRuntime, **kwargs: Any) -> InteractionResult:
    return await runtime.uncheck(**kwargs)


async def select(runtime: ToolRuntime, **kwargs: Any) -> InteractionResult:
    return await runtime.select(**kwargs)


async def upload(runtime: ToolRuntime, **kwargs: Any) -> InteractionResult:
    return await runtime.upload(**kwargs)


async def drag(runtime: ToolRuntime, **kwargs: Any) -> InteractionResult:
    return await runtime.drag(**kwargs)


async def hover(runtime: ToolRuntime, **kwargs: Any) -> InteractionResult:
    return await runtime.hover(**kwargs)


async def focus(runtime: ToolRuntime, **kwargs: Any) -> InteractionResult:
    return await runtime.focus(**kwargs)


async def tabs_list(runtime: ToolRuntime, **kwargs: Any) -> ToolResult:
    return await runtime.tabs_list(**kwargs)


async def tabs_select(runtime: ToolRuntime, **kwargs: Any) -> ToolResult:
    return await runtime.tabs_select(**kwargs)


async def tabs_close(runtime: ToolRuntime, **kwargs: Any) -> ToolResult:
    return await runtime.tabs_close(**kwargs)


async def console_messages(runtime: ToolRuntime, **kwargs: Any) -> ToolResult:
    return await runtime.console_messages(**kwargs)


async def network_requests(runtime: ToolRuntime, **kwargs: Any) -> ToolResult:
    return await runtime.network_requests(**kwargs)


async def screenshot(runtime: ToolRuntime, **kwargs: Any) -> ToolResult:
    return await runtime.screenshot(**kwargs)


async def take_trace(runtime: ToolRuntime, **kwargs: Any) -> ToolResult:
    return await runtime.take_trace(**kwargs)


async def assert_visible(runtime: ToolRuntime, **kwargs: Any) -> AssertionResult:
    return await runtime.assert_visible(**kwargs)


async def assert_text(runtime: ToolRuntime, **kwargs: Any) -> AssertionResult:
    return await runtime.assert_text(**kwargs)


async def assert_url(runtime: ToolRuntime, **kwargs: Any) -> AssertionResult:
    return await runtime.assert_url(**kwargs)


async def assert_title(runtime: ToolRuntime, **kwargs: Any) -> AssertionResult:
    return await runtime.assert_title(**kwargs)


async def assert_value(runtime: ToolRuntime, **kwargs: Any) -> AssertionResult:
    return await runtime.assert_value(**kwargs)


async def assert_checked(runtime: ToolRuntime, **kwargs: Any) -> AssertionResult:
    return await runtime.assert_checked(**kwargs)


async def assert_enabled(runtime: ToolRuntime, **kwargs: Any) -> AssertionResult:
    return await runtime.assert_enabled(**kwargs)


async def assert_hidden(runtime: ToolRuntime, **kwargs: Any) -> AssertionResult:
    return await runtime.assert_hidden(**kwargs)


async def assert_count(runtime: ToolRuntime, **kwargs: Any) -> AssertionResult:
    return await runtime.assert_count(**kwargs)


async def assert_in_viewport(runtime: ToolRuntime, **kwargs: Any) -> AssertionResult:
    return await runtime.assert_in_viewport(**kwargs)


async def dialog_handle(runtime: ToolRuntime, **kwargs: Any) -> DialogResult:
    return await runtime.dialog_handle(**kwargs)


async def frame_enter(runtime: ToolRuntime, **kwargs: Any) -> FrameContextResult:
    return await runtime.frame_enter(**kwargs)


async def frame_exit(runtime: ToolRuntime, **kwargs: Any) -> FrameContextResult:
    return await runtime.frame_exit(**kwargs)


def _filter_console_messages(
    messages: list[dict[str, Any]],
    *,
    min_level: Literal["verbose", "log", "info", "warning", "error"],
) -> list[dict[str, Any]]:
    severity_order = {"verbose": 0, "log": 1, "info": 2, "warning": 3, "error": 4}
    min_value = severity_order[min_level]

    mapped = {"debug": "verbose", "log": "log", "info": "info", "warning": "warning", "error": "error"}
    filtered: list[dict[str, Any]] = []
    for item in messages:
        item_type = str(item.get("type", "log"))
        normalized = mapped.get(item_type, "log")
        if severity_order[normalized] >= min_value:
            filtered.append(item)
    return filtered


def _temp_trace_path() -> Path:
    with NamedTemporaryFile(prefix="agent-trace-", suffix=".zip", delete=False) as temp_file:
        return Path(temp_file.name)

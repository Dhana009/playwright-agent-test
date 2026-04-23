# Ported from playwright-repo-test/lib/browser/inject.js and lib/record.js — adapted for agent/
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from playwright.async_api import Frame, Page
from pydantic import BaseModel, ConfigDict, Field

from agent.core.ids import generate_run_id
from agent.core.logging import configure_logging, get_logger
from agent.execution.browser import BrowserSession, StorageStateInput
from agent.locator.engine import LocatorEngine
from agent.stepgraph.models import LocatorBundle, Step, StepEdge, StepGraph, StepMode
from agent.storage.files import get_run_layout

_CAPTURE_QUEUE_INIT_SCRIPT = """
(() => {
  if (window.__agentRecorderInstalled) return;
  window.__agentRecorderInstalled = true;
  window.__agentRecorderSeq = 0;
  window.__agentRecorderQueue = [];

  window.__agentRecorderDrain = () => {
    const snapshot = window.__agentRecorderQueue.slice();
    window.__agentRecorderQueue = [];
    return snapshot;
  };

  function toXPath(node) {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return '';
    if (node.id) return `//*[@id="${node.id}"]`;
    const parts = [];
    let current = node;
    while (current && current.nodeType === Node.ELEMENT_NODE) {
      let index = 1;
      let sibling = current.previousElementSibling;
      while (sibling) {
        if (sibling.tagName === current.tagName) index += 1;
        sibling = sibling.previousElementSibling;
      }
      const tag = current.tagName.toLowerCase();
      parts.unshift(index > 1 ? `${tag}[${index}]` : tag);
      current = current.parentElement;
    }
    return '/' + parts.join('/');
  }

  function parentTrail(node) {
    const out = [];
    let current = node && node.parentElement;
    let depth = 0;
    while (current && current.tagName !== 'BODY' && depth < 4) {
      out.push({
        tag: current.tagName.toLowerCase(),
        id: current.id || '',
        className: typeof current.className === 'string' ? current.className : '',
        testid:
          current.getAttribute('data-testid') ||
          current.getAttribute('data-test-id') ||
          current.getAttribute('data-qa') ||
          '',
      });
      current = current.parentElement;
      depth += 1;
    }
    return out;
  }

  function dataAttrs(node) {
    const attrs = {};
    if (!node || !node.attributes) return attrs;
    for (const attr of node.attributes) {
      if (attr.name.startsWith('data-')) attrs[attr.name] = attr.value;
    }
    return attrs;
  }

  function collectTarget(node) {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return null;
    const text = (node.innerText || node.textContent || '').trim().slice(0, 120);
    const testid =
      node.getAttribute('data-testid') ||
      node.getAttribute('data-test-id') ||
      node.getAttribute('data-qa') ||
      '';

    return {
      tag: node.tagName.toLowerCase(),
      id: node.id || '',
      className: typeof node.className === 'string' ? node.className : '',
      testid,
      text,
      placeholder: node.getAttribute('placeholder') || '',
      ariaLabel: node.getAttribute('aria-label') || '',
      role: node.getAttribute('role') || '',
      inputType: node.getAttribute('type') || '',
      name: node.getAttribute('name') || '',
      dataAttrs: dataAttrs(node),
      siblingIndex: node.parentElement ? Array.from(node.parentElement.children).indexOf(node) : -1,
      parents: parentTrail(node),
      absoluteXPath: toXPath(node),
      frameContext: [],
      targetSemanticKey: [node.tagName.toLowerCase(), testid || node.id || node.getAttribute('name') || text]
        .filter(Boolean)
        .join(':'),
    };
  }

  function emitCapture(raw) {
    const entry = {
      ...raw,
      seq: ++window.__agentRecorderSeq,
      capturedAt: new Date().toISOString(),
      frameUrl: window.location.href,
      pageUrl: window.location.href,
    };
    window.__agentRecorderQueue.push(entry);
    if (typeof window.__agentRecordEmit === 'function') {
      window.__agentRecordEmit(entry);
    }
  }

  document.addEventListener(
    'click',
    (event) => {
      const node = event.target instanceof Element ? event.target.closest('*') : null;
      const target = collectTarget(node);
      if (!target) return;
      emitCapture({
        eventType: 'click',
        target,
        modifiers: {
          altKey: !!event.altKey,
          ctrlKey: !!event.ctrlKey,
          metaKey: !!event.metaKey,
          shiftKey: !!event.shiftKey,
        },
      });
    },
    true
  );

  document.addEventListener(
    'input',
    (event) => {
      const node = event.target instanceof Element ? event.target : null;
      const target = collectTarget(node);
      if (!target) return;
      const value = node && 'value' in node ? node.value : (node?.textContent || '').trim();
      emitCapture({
        eventType: 'input',
        target,
        value,
      });
    },
    true
  );

  document.addEventListener(
    'keydown',
    (event) => {
      const node = event.target instanceof Element ? event.target : null;
      const target = collectTarget(node);
      if (!target) return;
      emitCapture({
        eventType: 'keydown',
        target,
        key: event.key,
        code: event.code,
      });
    },
    true
  );
})();
""".strip()


class CapturedTarget(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    tag: str = "div"
    id: str | None = None
    class_name: str | None = Field(default=None, alias="className")
    testid: str | None = None
    text: str | None = None
    placeholder: str | None = None
    aria_label: str | None = Field(default=None, alias="ariaLabel")
    role: str | None = None
    input_type: str | None = Field(default=None, alias="inputType")
    name: str | None = None
    data_attrs: dict[str, str] = Field(default_factory=dict, alias="dataAttrs")
    sibling_index: int = Field(default=-1, alias="siblingIndex")
    parents: list[dict[str, Any]] = Field(default_factory=list)
    absolute_xpath: str | None = Field(default=None, alias="absoluteXPath")
    frame_context: list[str] = Field(default_factory=list, alias="frameContext")
    target_semantic_key: str | None = Field(default=None, alias="targetSemanticKey")


class RecorderCaptureEvent(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    seq: int = Field(ge=1)
    event_type: Literal["click", "input", "keydown"] = Field(alias="eventType")
    captured_at: str | None = Field(default=None, alias="capturedAt")
    frame_url: str | None = Field(default=None, alias="frameUrl")
    page_url: str | None = Field(default=None, alias="pageUrl")
    key: str | None = None
    code: str | None = None
    value: str | None = None
    target: CapturedTarget
    modifiers: dict[str, bool] = Field(default_factory=dict)


class RecorderArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    run_id: str = Field(alias="runId")
    stepgraph_path: str = Field(alias="stepgraphPath")
    manifest_path: str = Field(alias="manifestPath")
    step_count: int = Field(alias="stepCount")
    source_url: str = Field(alias="sourceUrl")


@dataclass
class RecorderModeState:
    selected_mode: str = "auto"


class StepGraphRecorder:
    def __init__(
        self,
        *,
        url: str,
        run_id: str | None = None,
        headless: bool = False,
        storage_state: StorageStateInput | None = None,
        poll_interval_ms: int = 250,
    ) -> None:
        self._run_id = run_id or generate_run_id()
        configure_logging(self._run_id)
        self._logger = get_logger(__name__)

        self._url = url
        self._storage_state = storage_state
        self._poll_interval_ms = max(50, poll_interval_ms)

        self._session = BrowserSession(headless=headless)
        self._locator_engine = LocatorEngine()
        self._mode_state = RecorderModeState()

        self._context_id: str | None = None
        self._tab_id: str | None = None
        self._page: Page | None = None
        self._started_at = datetime.now(UTC)
        self._stopped = False

        self._captures_by_seq: dict[int, RecorderCaptureEvent] = {}
        self._seen_sequences: set[int] = set()
        self._poll_task: asyncio.Task[None] | None = None

        self._graph = StepGraph(runId=self._run_id, steps=[], edges=[], version="1.0")

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def mode_state(self) -> RecorderModeState:
        return self._mode_state

    @property
    def step_graph(self) -> StepGraph:
        return self._graph

    def set_operator_mode(self, mode: str) -> None:
        self._mode_state.selected_mode = mode.strip() or "auto"
        self._logger.info("recorder_mode_updated", run_id=self._run_id, mode=self._mode_state.selected_mode)

    async def start(self) -> None:
        await self._session.start()
        self._context_id, context = await self._session.new_context(storage_state=self._storage_state)
        page = await context.new_page()
        self._page = page
        self._tab_id = self._session.get_tab_id(page)
        if self._tab_id is None:
            raise RuntimeError("Failed to resolve tab id for recorder page.")

        await context.expose_binding("__agentRecordEmit", self._on_capture_binding)
        await context.add_init_script(_CAPTURE_QUEUE_INIT_SCRIPT)

        page.on("framenavigated", self._on_frame_navigated)
        page.on("console", self._on_console)

        await page.goto(self._url, wait_until="domcontentloaded", timeout=15_000)
        await page.wait_for_function("window.__agentRecorderInstalled === true", timeout=15_000)
        self._poll_task = asyncio.create_task(self._poll_inpage_queue(), name=f"recorder-poll-{self._run_id}")

        self._logger.info(
            "recorder_started",
            run_id=self._run_id,
            url=self._url,
            context_id=self._context_id,
            tab_id=self._tab_id,
        )

    def _on_frame_navigated(self, frame: Frame) -> None:
        self._logger.info(
            "recorder_frame_navigated",
            run_id=self._run_id,
            is_main_frame=bool(self._page and frame == self._page.main_frame),
            frame_url=frame.url,
        )

    def _on_console(self, message: Any) -> None:
        self._logger.info(
            "recorder_console_message",
            run_id=self._run_id,
            message_type=getattr(message, "type", "unknown"),
            text=getattr(message, "text", ""),
        )

    async def stop(self) -> RecorderArtifact:
        if self._stopped:
            return self._build_artifact()
        self._stopped = True

        try:
            await self._flush_inpage_queue()
            await self._process_new_sequences()

            if self._poll_task is not None:
                self._poll_task.cancel()
                try:
                    await self._poll_task
                except asyncio.CancelledError:
                    pass
                self._poll_task = None

            return self._write_artifacts()
        finally:
            await self._session.stop()
            self._logger.info("recorder_stopped", run_id=self._run_id, step_count=len(self._graph.steps))

    async def _poll_inpage_queue(self) -> None:
        while True:
            await self._flush_inpage_queue()
            await self._process_new_sequences()
            await asyncio.sleep(self._poll_interval_ms / 1000)

    async def _on_capture_binding(self, source: Any, payload: dict[str, Any]) -> None:
        enriched = dict(payload)
        enriched.setdefault("frameUrl", source.frame.url)
        await self._ingest_capture(enriched)
        await self._process_new_sequences()

    async def _flush_inpage_queue(self) -> None:
        page = self._page
        if page is None:
            return
        for attempt in range(2):
            try:
                entries = await asyncio.wait_for(
                    page.evaluate("window.__agentRecorderDrain ? window.__agentRecorderDrain() : []"),
                    timeout=2.0,
                )
                break
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                if attempt == 0 and "Execution context was destroyed" in message:
                    await asyncio.sleep(0.1)
                    continue
                self._logger.warning(
                    "recorder_queue_flush_failed",
                    run_id=self._run_id,
                    error=message,
                )
                return

        if not isinstance(entries, list):
            return
        for entry in entries:
            if isinstance(entry, dict):
                await self._ingest_capture(entry)

    async def _ingest_capture(self, payload: dict[str, Any]) -> None:
        try:
            capture = RecorderCaptureEvent.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("recorder_capture_invalid", run_id=self._run_id, error=str(exc))
            return

        if capture.seq in self._seen_sequences:
            return
        self._seen_sequences.add(capture.seq)
        self._captures_by_seq[capture.seq] = capture

    async def _process_new_sequences(self) -> None:
        pending = sorted(self._captures_by_seq.items(), key=lambda item: item[0])
        if not pending:
            return
        self._captures_by_seq.clear()

        for _, capture in pending:
            await self._capture_to_step(capture)

    async def _capture_to_step(self, capture: RecorderCaptureEvent) -> None:
        intent = self._resolve_intent(capture)
        if intent is None:
            return

        page = self._page
        tab_id = self._tab_id
        if page is None or tab_id is None:
            return

        locator_bundle = await self._build_locator_bundle(page, capture)

        metadata: dict[str, Any] = {
            "tabId": tab_id,
            "capturedSeq": capture.seq,
            "capturedAt": capture.captured_at,
            "sourceEventType": capture.event_type,
            "semanticIntent": intent["semantic_intent"],
            "intentConfidence": intent["confidence"],
            "operatorMode": self._mode_state.selected_mode,
            "frameUrl": capture.frame_url,
            **intent["metadata"],
        }

        if intent["action"] in {"click", "fill", "type", "press", "assert_visible", "assert_text"}:
            step = Step(
                mode=StepMode.ACTION if not intent["action"].startswith("assert") else StepMode.ASSERTION,
                action=intent["action"],
                target=locator_bundle,
                metadata=metadata,
            )
        else:
            return

        if self._should_coalesce_fill(step):
            self._coalesce_fill(step)
            return

        self._append_step(step)
        self._logger.info(
            "recorder_step_captured",
            run_id=self._run_id,
            step_id=step.step_id,
            action=step.action,
            sequence=capture.seq,
        )

    async def _build_locator_bundle(self, page: Page, capture: RecorderCaptureEvent) -> LocatorBundle | None:
        descriptor = capture.target.model_dump(by_alias=True, exclude_none=True)
        scope: Page | Frame = self._resolve_scope(page, capture.frame_url)
        try:
            return await self._locator_engine.build(scope, descriptor, force=True)
        except Exception as exc:  # noqa: BLE001
            xpath = capture.target.absolute_xpath
            if isinstance(xpath, str) and xpath.strip():
                return LocatorBundle(
                    primarySelector=f"xpath={xpath}",
                    fallbackSelectors=[],
                    confidenceScore=0.2,
                    reasoningHint=f"fallback absolute xpath due to locator build error: {exc}",
                    frameContext=list(capture.target.frame_context),
                )
            self._logger.warning(
                "recorder_locator_bundle_failed",
                run_id=self._run_id,
                sequence=capture.seq,
                error=str(exc),
            )
            return None

    def _resolve_scope(self, page: Page, frame_url: str | None) -> Page | Frame:
        if not frame_url:
            return page
        for frame in page.frames:
            if frame.url == frame_url:
                return frame
        return page

    def _resolve_intent(self, capture: RecorderCaptureEvent) -> dict[str, Any] | None:
        mode = self._mode_state.selected_mode
        target = capture.target

        if mode == "assert_visible" and capture.event_type == "click":
            return {
                "action": "assert_visible",
                "semantic_intent": "operator_assert_visible",
                "confidence": 0.95,
                "metadata": {},
            }
        if mode == "assert_text" and capture.event_type == "click":
            expected = (target.text or "").strip()
            if expected:
                return {
                    "action": "assert_text",
                    "semantic_intent": "operator_assert_text",
                    "confidence": 0.9,
                    "metadata": {"expected": expected, "contains": True},
                }

        if capture.event_type == "input":
            value = capture.value or ""
            if self._is_sensitive_input(capture.target):
                return {
                    "action": "fill",
                    "semantic_intent": "field_input_redacted",
                    "confidence": 0.9,
                    "metadata": {"valueRef": "redacted"},
                }
            return {
                "action": "fill",
                "semantic_intent": "field_input",
                "confidence": 0.9,
                "metadata": {"text": value},
            }

        if capture.event_type == "keydown" and (capture.key or "").lower() == "enter":
            semantic_intent = "submit_form"
            if self._looks_like_search_target(target):
                semantic_intent = "search_submit"
            return {
                "action": "press",
                "semantic_intent": semantic_intent,
                "confidence": 0.75,
                "metadata": {"key": "Enter"},
            }

        if capture.event_type == "click":
            semantic_intent = "click"
            lowered_text = (target.text or "").strip().lower()
            if target.tag == "button" and any(token in lowered_text for token in ("submit", "save", "search")):
                semantic_intent = "submit_button_click"
            return {
                "action": "click",
                "semantic_intent": semantic_intent,
                "confidence": 0.8,
                "metadata": {},
            }

        return None

    def _is_sensitive_input(self, target: CapturedTarget) -> bool:
        input_type = (target.input_type or "").lower().strip()
        if input_type == "password":
            return True
        combined = " ".join(
            token
            for token in [
                target.id or "",
                target.name or "",
                target.placeholder or "",
                target.aria_label or "",
                target.target_semantic_key or "",
            ]
            if token
        ).lower()
        return "password" in combined or "passcode" in combined

    def _looks_like_search_target(self, target: CapturedTarget) -> bool:
        input_type = (target.input_type or "").lower()
        if input_type == "search":
            return True
        search_hints = " ".join(filter(None, [target.placeholder, target.aria_label, target.name, target.text])).lower()
        return "search" in search_hints

    def _append_step(self, step: Step) -> None:
        previous_step = self._graph.steps[-1] if self._graph.steps else None
        self._graph.steps.append(step)
        if previous_step is not None:
            self._graph.edges.append(
                StepEdge(
                    fromStepId=previous_step.step_id,
                    toStepId=step.step_id,
                    condition="on_success",
                )
            )

    def _should_coalesce_fill(self, incoming: Step) -> bool:
        if incoming.action != "fill" or not self._graph.steps:
            return False
        latest = self._graph.steps[-1]
        if latest.action != "fill":
            return False
        if latest.target is None or incoming.target is None:
            return False
        return latest.target.primary_selector == incoming.target.primary_selector

    def _coalesce_fill(self, incoming: Step) -> None:
        latest = self._graph.steps[-1]
        latest.metadata.update(incoming.metadata)
        self._logger.info(
            "recorder_fill_coalesced",
            run_id=self._run_id,
            step_id=latest.step_id,
            target=latest.target.primary_selector if latest.target else None,
        )

    def _write_artifacts(self) -> RecorderArtifact:
        layout = get_run_layout(self._run_id)
        stepgraph_path = layout.run_dir / "stepgraph.json"
        stepgraph_path.write_text(self._graph.model_dump_json(indent=2, by_alias=True), encoding="utf-8")

        manifest_payload = {
            "runId": self._run_id,
            "sourceUrl": self._url,
            "recordedAt": datetime.now(UTC).isoformat(),
            "startedAt": self._started_at.isoformat(),
            "stepCount": len(self._graph.steps),
            "edgeCount": len(self._graph.edges),
            "tabId": self._tab_id,
            "contextId": self._context_id,
            "artifacts": {
                "stepgraph": str(stepgraph_path),
                "log": str(layout.log_jsonl),
                "manifest": str(layout.manifest_json),
            },
            "notes": {
                "captureModel": "in-page queue + binding fast-path",
                "intentModel": "heuristic + operator mode state",
            },
        }
        layout.manifest_json.write_text(json.dumps(manifest_payload, indent=2), encoding="utf-8")

        self._logger.info(
            "recorder_artifacts_written",
            run_id=self._run_id,
            stepgraph_path=str(stepgraph_path),
            manifest_path=str(layout.manifest_json),
            step_count=len(self._graph.steps),
        )
        return self._build_artifact()

    def _build_artifact(self) -> RecorderArtifact:
        layout = get_run_layout(self._run_id)
        return RecorderArtifact(
            runId=self._run_id,
            stepgraphPath=str(layout.run_dir / "stepgraph.json"),
            manifestPath=str(layout.manifest_json),
            stepCount=len(self._graph.steps),
            sourceUrl=self._url,
        )

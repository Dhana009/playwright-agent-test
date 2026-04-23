"""Single-browser interactive replay for the dashboard (step-by-step, mutable graph)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from dotenv import load_dotenv
from playwright.async_api import Frame, Page

from agent.cache.engine import CacheEngine
from agent.core.config import Settings
from agent.core.logging import get_logger
from agent.execution.browser import BrowserSession
from agent.execution.checkpoint_writer import CheckpointWriter, RunnerEventSink
from agent.execution.events import EventType
from agent.execution.runner import StepGraphRunner
from agent.execution.snapshot import SnapshotEngine
from agent.execution.tools import ToolRuntime
from agent.policy.approval import ApprovalClassifier, HardApprovalRequest
from agent.policy.audit import AuditLogger
from agent.policy.restrictions import RestrictionViolation, RestrictionsPolicy
from agent.locator.engine import LocatorEngine
from agent.recorder.recorder import RecorderCaptureEvent, _CAPTURE_QUEUE_INIT_SCRIPT, _playwright_binding_frame_url
from agent.stepgraph.models import LocatorBundle, Step, StepEdge, StepGraph, StepMode, TimeoutPolicy
from agent.storage.files import get_run_layout
from agent.storage.repos.step_graph import StepGraphRepository

_LOGGER = get_logger(__name__)

FORCE_FIX_UI_CAVEAT = (
    "The dashboard first checks the interactive browser tab: at least one selector must match "
    "(same frame stack as when you run steps). Then Force fix reorders primary/fallback locator text on the step. "
    "For upload steps, if `[data-testid=\"upload-area\"]` is attached on that tab, it also appends canonical "
    "upload fallbacks (`upload-area`, first `input[type=file]`) before reordering. "
    "This is not an LLM: it does not invent selectors from failure messages or read the DOM beyond that probe. "
    "It does not log in or navigate for you — fix the page first if nothing matches."
)


def _selector_chain(bundle: LocatorBundle) -> tuple[str, ...]:
    parts = [bundle.primary_selector, *bundle.fallback_selectors]
    return tuple(s.strip() for s in parts if isinstance(s, str) and s.strip())


def _selectors_from_bundle_for_probe(bundle: LocatorBundle) -> list[str]:
    parts = [bundle.primary_selector, *bundle.fallback_selectors]
    return [s.strip() for s in parts if isinstance(s, str) and s.strip()]


def _playwright_wait_state_for_probe(step: Step) -> str:
    """``state`` for :meth:`Locator.wait_for` when probing before Force fix."""
    action = (step.action or "").strip().lower()
    if action == "wait_for" and isinstance(step.metadata, dict):
        st = step.metadata.get("state")
        if isinstance(st, str) and st.strip() in ("attached", "detached", "visible", "hidden"):
            return st.strip()
    if action in ("upload",):
        return "attached"
    return "visible"


@dataclass(frozen=True)
class ForceFixOutcome:
    """Result of applying CLI-style locator relaxation to one step (graph-only, no browser I/O)."""

    changed: bool
    primary_selector: str
    fallback_count: int
    message: str


def _force_fix_locator_bundle(bundle: LocatorBundle) -> LocatorBundle:
    """Deterministic relax: try primary then every fallback (same idea as ``agent.cli.fix``)."""
    selectors = [bundle.primary_selector, *bundle.fallback_selectors]
    selectors = [s for s in selectors if isinstance(s, str) and s.strip()]
    if not selectors:
        return bundle
    primary = selectors[0]
    fallbacks = selectors[1:]
    return LocatorBundle(
        primarySelector=primary,
        fallbackSelectors=fallbacks,
        confidenceScore=bundle.confidence_score,
        reasoningHint=bundle.reasoning_hint,
        frameContext=bundle.frame_context,
    )


_UPLOAD_FORCE_FIX_EXTRA_FALLBACKS: tuple[str, ...] = (
    '[data-testid="upload-area"]',
    'input[type=file]>>nth=0',
)


def _enrich_upload_bundle_with_known_fallbacks(bundle: LocatorBundle) -> tuple[LocatorBundle, bool]:
    """Append canonical resume-upload fallbacks when the page exposes ``upload-area`` (see PORTING_NOTES)."""
    chain = [bundle.primary_selector, *bundle.fallback_selectors]
    chain = [s.strip() for s in chain if isinstance(s, str) and s.strip()]
    merged = list(dict.fromkeys(chain))
    before = tuple(merged)
    for fb in _UPLOAD_FORCE_FIX_EXTRA_FALLBACKS:
        if fb not in merged:
            merged.append(fb)
    if tuple(merged) == before:
        return bundle, False
    return (
        LocatorBundle(
            primarySelector=merged[0],
            fallbackSelectors=merged[1:],
            confidenceScore=bundle.confidence_score,
            reasoningHint=bundle.reasoning_hint,
            frameContext=bundle.frame_context,
        ),
        True,
    )


def _load_optional_env_test() -> None:
    candidate = Path(__file__).resolve().parents[3] / ".env.test"
    if candidate.is_file():
        load_dotenv(candidate, override=False)


def _derive_initial_url_for_blank_page(graph: StepGraph) -> str | None:
    if not graph.steps:
        return None
    if graph.steps[0].action.strip().lower() == "navigate":
        return None
    for step in graph.steps:
        meta = step.metadata or {}
        for key in ("frameUrl", "frame_url", "pageUrl", "page_url", "url"):
            val = meta.get(key)
            if isinstance(val, str) and val.strip().startswith(("http://", "https://")):
                return val.strip()
    return None


def _propagate_upload_page_hints(graph: StepGraph) -> StepGraph:
    """If an upload step has no http ``pageUrl``/``frameUrl``, copy the last http URL from earlier steps.

    Covers graphs where a ``navigate`` step stores ``metadata.url`` but a later dashboard-created upload
    omitted page hints, so interactive / CLI replay would otherwise stay on ``about:blank``.
    """
    last_http: str | None = None
    new_steps: list[Step] = []
    changed = False
    for s in graph.steps:
        md = dict(s.metadata) if isinstance(s.metadata, dict) else {}
        for key in ("pageUrl", "page_url", "frameUrl", "frame_url", "url"):
            v = md.get(key)
            if isinstance(v, str) and v.strip().startswith(("http://", "https://")):
                last_http = v.strip()
                break
        if (s.action or "").strip().lower() == "upload" and last_http:
            has = any(
                isinstance(md.get(k), str) and str(md.get(k)).strip().startswith(("http://", "https://"))
                for k in ("pageUrl", "page_url", "frameUrl", "frame_url")
            )
            if not has:
                md.setdefault("pageUrl", last_http)
                md.setdefault("frameUrl", last_http)
                changed = True
                new_steps.append(s.model_copy(update={"metadata": md}))
                continue
        new_steps.append(s)
    return graph.model_copy(update={"steps": new_steps}) if changed else graph


def _bundle_from_selector(selector: str) -> LocatorBundle:
    sel = selector.strip()
    return LocatorBundle(
        primarySelector=sel,
        fallbackSelectors=[],
        confidenceScore=0.85,
        reasoningHint="manual insert from interactive dashboard",
        frameContext=[],
    )


def build_step_from_interactive_insert_body(body: dict[str, Any]) -> Step:
    """Build a :class:`Step` from dashboard JSON (same shapes as recorder control actions). ``tabId`` is added on insert."""
    raw_kind = body.get("kind") or body.get("action")
    kind = str(raw_kind or "").strip().lower()
    if not kind:
        raise ValueError("kind is required")

    def _int(name: str, default: int) -> int:
        v = body.get(name)
        if v is None:
            return default
        return int(v)

    if kind == "navigate":
        url = str(body.get("url") or "").strip()
        if not url:
            raise ValueError("url is required for navigate")
        return Step(
            mode=StepMode.NAVIGATION,
            action="navigate",
            target=None,
            metadata={"url": url},
            timeout_policy=TimeoutPolicy(timeoutMs=max(1000, _int("timeoutMs", 30_000))),
        )

    if kind == "wait_for":
        selector = str(body.get("selector") or "").strip()
        if not selector:
            raise ValueError("selector is required for wait_for")
        state = str(body.get("state") or "visible").strip() or "visible"
        bundle = _bundle_from_selector(selector)
        return Step(
            mode=StepMode.ACTION,
            action="wait_for",
            target=bundle,
            metadata={"state": state, "target": selector},
            timeout_policy=TimeoutPolicy(timeoutMs=max(1000, _int("timeoutMs", 30_000))),
        )

    if kind == "assert_visible":
        selector = str(body.get("selector") or "").strip()
        if not selector:
            raise ValueError("selector is required for assert_visible")
        bundle = _bundle_from_selector(selector)
        return Step(
            mode=StepMode.ASSERTION,
            action="assert_visible",
            target=bundle,
            metadata={},
            timeout_policy=TimeoutPolicy(timeoutMs=max(1000, _int("timeoutMs", 30_000))),
        )

    if kind == "assert_text":
        selector = str(body.get("selector") or "").strip()
        if not selector:
            raise ValueError("selector is required for assert_text")
        expected = str(body.get("expected") or "").strip()
        if not expected:
            raise ValueError("expected is required for assert_text")
        contains = bool(body.get("contains", True))
        bundle = _bundle_from_selector(selector)
        return Step(
            mode=StepMode.ASSERTION,
            action="assert_text",
            target=bundle,
            metadata={"expected": expected, "contains": contains},
            timeout_policy=TimeoutPolicy(timeoutMs=max(1000, _int("timeoutMs", 30_000))),
        )

    if kind == "assert_url":
        expected = str(body.get("expected") or "").strip()
        if not expected:
            raise ValueError("expected is required for assert_url")
        contains = bool(body.get("contains", True))
        return Step(
            mode=StepMode.ASSERTION,
            action="assert_url",
            target=None,
            metadata={"expected": expected, "contains": contains},
            timeout_policy=TimeoutPolicy(timeoutMs=max(1000, _int("timeoutMs", 15_000))),
        )

    if kind == "assert_title":
        expected = str(body.get("expected") or "").strip()
        if not expected:
            raise ValueError("expected is required for assert_title")
        contains = bool(body.get("contains", True))
        return Step(
            mode=StepMode.ASSERTION,
            action="assert_title",
            target=None,
            metadata={"expected": expected, "contains": contains},
            timeout_policy=TimeoutPolicy(timeoutMs=15_000),
        )

    if kind == "wait_timeout":
        timeout_ms = max(1, _int("timeoutMs", 1000))
        return Step(
            mode=StepMode.ACTION,
            action="wait_timeout",
            target=None,
            metadata={"timeoutMs": timeout_ms},
            timeout_policy=TimeoutPolicy(timeoutMs=timeout_ms),
        )

    if kind == "frame_enter":
        selector = str(body.get("selector") or "").strip()
        if not selector:
            raise ValueError("selector is required for frame_enter")
        bundle = _bundle_from_selector(selector)
        return Step(
            mode=StepMode.ACTION,
            action="frame_enter",
            target=bundle,
            metadata={"target": selector},
            timeout_policy=TimeoutPolicy(timeoutMs=max(1000, _int("timeoutMs", 30_000))),
        )

    if kind == "frame_exit":
        return Step(
            mode=StepMode.ACTION,
            action="frame_exit",
            target=None,
            metadata={},
            timeout_policy=TimeoutPolicy(timeoutMs=15_000),
        )

    if kind == "upload":
        selector = str(body.get("selector") or "").strip()
        raw_paths = body.get("filePaths") or body.get("file_paths") or body.get("paths")
        paths: str | list[str]
        if isinstance(raw_paths, list) and raw_paths:
            paths = [str(p).strip() for p in raw_paths if isinstance(p, str) and p.strip()]
        elif isinstance(raw_paths, str) and raw_paths.strip():
            lines = [ln.strip() for ln in raw_paths.splitlines() if ln.strip()]
            paths = lines if len(lines) > 1 else raw_paths.strip()
        else:
            paths = []
        if not selector or not paths:
            raise ValueError("selector and filePaths are required for upload")
        bundle = _bundle_from_selector(selector)
        return Step(
            mode=StepMode.ACTION,
            action="upload",
            target=bundle,
            metadata={"filePaths": paths},
            timeout_policy=TimeoutPolicy(timeoutMs=max(1000, _int("timeoutMs", 30_000))),
        )

    if kind == "dialog_handle":
        accept = bool(body.get("accept", True))
        prompt_raw = body.get("promptText") or body.get("prompt_text")
        prompt_text = str(prompt_raw).strip() if isinstance(prompt_raw, str) else None
        meta: dict[str, Any] = {"accept": accept}
        if prompt_text:
            meta["promptText"] = prompt_text
        body_bundle = LocatorBundle(
            primarySelector="body",
            fallbackSelectors=[],
            confidenceScore=0.5,
            reasoningHint="placeholder target for dialog_handle (page-level)",
            frameContext=[],
        )
        return Step(
            mode=StepMode.ACTION,
            action="dialog_handle",
            target=body_bundle,
            metadata=meta,
            timeout_policy=TimeoutPolicy(timeoutMs=max(1000, _int("timeoutMs", 15_000))),
        )

    if kind == "click":
        selector = str(body.get("selector") or "").strip()
        if not selector:
            raise ValueError("selector is required for click")
        bundle = _bundle_from_selector(selector)
        meta: dict[str, Any] = {}
        btn = body.get("button")
        if isinstance(btn, str) and btn.strip():
            meta["button"] = btn.strip()
        return Step(
            mode=StepMode.ACTION,
            action="click",
            target=bundle,
            metadata=meta,
            timeout_policy=TimeoutPolicy(timeoutMs=max(1000, _int("timeoutMs", 30_000))),
        )

    if kind == "fill":
        selector = str(body.get("selector") or "").strip()
        if not selector:
            raise ValueError("selector is required for fill")
        text = body.get("text")
        if not isinstance(text, str):
            raise ValueError("text (string) is required for fill")
        bundle = _bundle_from_selector(selector)
        return Step(
            mode=StepMode.ACTION,
            action="fill",
            target=bundle,
            metadata={"text": text},
            timeout_policy=TimeoutPolicy(timeoutMs=max(1000, _int("timeoutMs", 30_000))),
        )

    if kind == "type":
        selector = str(body.get("selector") or "").strip()
        if not selector:
            raise ValueError("selector is required for type")
        text = body.get("text")
        if not isinstance(text, str):
            raise ValueError("text (string) is required for type")
        delay_ms = float(body.get("delayMs") or body.get("delay_ms") or 0)
        bundle = _bundle_from_selector(selector)
        return Step(
            mode=StepMode.ACTION,
            action="type",
            target=bundle,
            metadata={"text": text, "delayMs": delay_ms},
            timeout_policy=TimeoutPolicy(timeoutMs=max(1000, _int("timeoutMs", 30_000))),
        )

    if kind == "press":
        selector = str(body.get("selector") or "").strip()
        if not selector:
            raise ValueError("selector is required for press")
        key = str(body.get("key") or "").strip()
        if not key:
            raise ValueError("key is required for press (e.g. Enter)")
        bundle = _bundle_from_selector(selector)
        return Step(
            mode=StepMode.ACTION,
            action="press",
            target=bundle,
            metadata={"key": key},
            timeout_policy=TimeoutPolicy(timeoutMs=max(1000, _int("timeoutMs", 30_000))),
        )

    raise ValueError(f"Unsupported kind for insert: {kind}")


def _rebuild_linear_edges(graph: StepGraph) -> StepGraph:
    edges: list[StepEdge] = []
    prev = None
    for step in graph.steps:
        if prev is not None:
            edges.append(
                StepEdge(
                    fromStepId=prev.step_id,
                    toStepId=step.step_id,
                    condition="on_success",
                )
            )
        prev = step
    return graph.model_copy(update={"edges": edges})


class InteractiveReplaySession:
    """Keeps one browser context alive; run steps on demand against a mutable :class:`StepGraph`."""

    def __init__(self) -> None:
        self.graph: StepGraph | None = None
        self._session: BrowserSession | None = None
        self._runner: StepGraphRunner | None = None
        self._tab_id: str | None = None
        self._folder_run_id: str | None = None
        self.last_error: str | None = None
        self._upload_last_errors: dict[str, str] = {}
        self._locator_engine: LocatorEngine | None = None
        self._pick_result: dict[str, Any] | None = None
        self._run_activity: dict[str, Any] | None = None
        self._tool_runtime: ToolRuntime | None = None

    @property
    def active(self) -> bool:
        return self._session is not None and self.graph is not None

    @property
    def folder_run_id(self) -> str | None:
        return self._folder_run_id

    def run_activity_snapshot(self) -> dict[str, Any] | None:
        """Latest runner progress for dashboard polling (copy for JSON)."""
        if self._run_activity is None:
            return None
        return dict(self._run_activity)

    def last_upload_error_for_step(self, step_id: str) -> str | None:
        """Last runner failure message for an ``upload`` step (dashboard step list)."""
        return self._upload_last_errors.get(step_id)

    async def _runner_ui_event(self, event: Any) -> None:
        """Drive ``_run_activity`` from :class:`StepGraphRunner` events (same loop as replay)."""
        et = getattr(event, "type", None)
        step_id = getattr(event, "step_id", None)
        if not isinstance(step_id, str) or not step_id:
            return
        pl = getattr(event, "payload", None)
        if not isinstance(pl, dict):
            pl = {}
        if et == EventType.STEP_STARTED:
            action = str(pl.get("action") or "step")
            att0 = int(pl.get("attempt", 0))
            mx = max(1, int(pl.get("max_attempts", 1)))
            att = att0 + 1
            label = f"Running {action} (attempt {att}/{mx})…"
            self._run_activity = {
                "phase": "running",
                "stepId": step_id,
                "action": action,
                "attempt": att,
                "maxAttempts": mx,
                "label": label,
            }
        elif et == EventType.STEP_RETRIED:
            prev = self._run_activity or {}
            action = str(prev.get("action") or "step")
            err = str(pl.get("error", ""))[:240]
            finished_attempt = int(pl.get("attempt", 0)) + 1
            tail = err if len(err) <= 100 else err[:97] + "…"
            label = f"Retrying {action} after attempt {finished_attempt} failed: {tail}"
            self._run_activity = {
                "phase": "retrying",
                "stepId": step_id,
                "action": action,
                "attempt": finished_attempt,
                "label": label,
            }

    async def start(self, run_id: str) -> StepGraph:
        _load_optional_env_test()
        layout = get_run_layout(run_id)
        sg = layout.run_dir / "stepgraph.json"
        if not sg.is_file():
            raise FileNotFoundError(str(sg))
        graph = StepGraph.model_validate_json(sg.read_text(encoding="utf-8"))
        graph = _propagate_upload_page_hints(graph)

        settings = Settings.load()
        step_graph_repo = StepGraphRepository(sqlite_path=settings.storage.sqlite_path)
        await step_graph_repo.save(graph)

        writer = CheckpointWriter.for_run(run_id=graph.run_id, sqlite_path=settings.storage.sqlite_path)
        sink = RunnerEventSink(writer)
        audit_logger = AuditLogger.for_run(run_id=graph.run_id)
        restrictions_policy = RestrictionsPolicy.from_settings(settings.policy)

        self._session = BrowserSession(headless=False)
        await self._session.start()
        _, context = await self._session.new_context()
        await context.add_init_script(_CAPTURE_QUEUE_INIT_SCRIPT)
        page = await context.new_page()
        tab_id = self._session.get_tab_id(page)
        if tab_id is None:
            await self._session.stop()
            self._session = None
            raise RuntimeError("Failed to acquire tab_id for interactive replay.")

        await context.expose_binding("__agentPickEmit", self._on_interactive_pick_binding)
        try:
            await page.wait_for_function("() => window.__agentRecorderInstalled === true", timeout=15_000)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "interactive_replay_init_script_wait_failed",
                run_id=graph.run_id,
                error=str(exc),
            )
            self.last_error = f"Recorder init script not detected on blank page: {exc}"
        try:
            await page.evaluate("() => { window.__agentRecorderArmed = false; }")
        except Exception:  # noqa: BLE001
            pass
        self._locator_engine = LocatorEngine()
        self._pick_result = None

        for step in graph.steps:
            step.metadata["tabId"] = tab_id

        # Expose graph + tab as soon as the page exists so the dashboard never shows "inactive"
        # while this Chromium window is already up (init script / runner / navigate can still fail).
        self.graph = graph
        self._tab_id = tab_id
        self._folder_run_id = run_id

        snapshot_engine = SnapshotEngine(self._session)
        cache_engine = CacheEngine(self._session)

        def _emit_tool_audit(event: object) -> None:
            audit_logger.record_tool_call(event)

        runtime = ToolRuntime(
            self._session,
            snapshot_engine=snapshot_engine,
            event_emitter=_emit_tool_audit,
        )

        def _approve_all(_request: HardApprovalRequest) -> bool:
            return True

        self._runner = StepGraphRunner(
            runtime,
            event_sink=sink,
            event_emitter=self._runner_ui_event,
            cache_engine=cache_engine,
            snapshot_engine=snapshot_engine,
            approval_classifier=ApprovalClassifier(),
            hard_approval_resolver=_approve_all,
            restrictions_policy=restrictions_policy,
            audit_logger=audit_logger,
        )
        self._tool_runtime = runtime

        initial_url = _derive_initial_url_for_blank_page(graph)
        if initial_url is not None:
            try:
                restrictions_policy.enforce_navigation_url(initial_url)
            except RestrictionViolation as exc:
                await self.stop()
                raise RuntimeError(f"Initial navigation blocked by policy: {exc}") from exc
            _LOGGER.info("interactive_replay_initial_nav", run_id=graph.run_id, url=initial_url)
            try:
                await runtime.navigate(
                    tab_id=tab_id,
                    url=initial_url,
                    # Interactive dashboard should open quickly; full "load" can wait on ads/analytics.
                    wait_until="domcontentloaded",
                    timeout_ms=60_000.0,
                )
            except Exception as exc:  # noqa: BLE001
                msg = f"Initial navigation to {initial_url[:200]!r} failed: {exc}"
                self.last_error = msg
                _LOGGER.warning("interactive_replay_initial_nav_failed", run_id=graph.run_id, error=str(exc))
            else:
                try:
                    await page.wait_for_function("() => window.__agentRecorderInstalled === true", timeout=15_000)
                    await page.evaluate("() => { window.__agentRecorderArmed = false; }")
                except Exception:  # noqa: BLE001
                    pass

        return graph

    async def stop(self) -> None:
        """Close the browser if present and always clear in-memory graph/runner state."""
        if self._session is not None:
            try:
                await self._session.stop()
            except Exception:  # noqa: BLE001
                pass
        self._session = None
        self._runner = None
        self._tab_id = None
        self.graph = None
        self._folder_run_id = None
        self._locator_engine = None
        self._pick_result = None
        self._run_activity = None
        self._tool_runtime = None

    @staticmethod
    def _resolve_pick_scope(page: Page, frame_url: str | None) -> Page | Frame:
        if not frame_url:
            return page
        for frame in page.frames:
            if frame.url == frame_url:
                return frame
        return page

    async def _on_interactive_pick_binding(self, source: Any, payload: Any) -> None:
        """Handle in-page pick commit: build locator, store for dashboard (no graph append)."""
        if not isinstance(payload, dict):
            return
        pi = payload.get("pickIntent") if isinstance(payload.get("pickIntent"), dict) else {}
        kind = str(pi.get("kind") or "").strip()
        if kind not in ("wait_for", "assert_visible", "assert_text", "upload"):
            return
        enriched = dict(payload)
        enriched.setdefault("frameUrl", _playwright_binding_frame_url(source))
        try:
            capture = RecorderCaptureEvent.model_validate(enriched)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("interactive_pick_invalid", error=str(exc))
            return
        session = self._session
        tid = self._tab_id
        engine = self._locator_engine
        if session is None or tid is None or engine is None:
            return
        page = session.get_tab(tid)
        if page is None:
            return
        scope = self._resolve_pick_scope(page, capture.frame_url)
        descriptor = capture.target.model_dump(by_alias=True, exclude_none=True)
        try:
            bundle = await engine.build(scope, descriptor, force=True)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("interactive_pick_bundle_failed", error=str(exc))
            return
        out: dict[str, Any] = {"kind": kind, "primarySelector": bundle.primary_selector}
        if kind == "upload":
            self._pick_result = out
            _LOGGER.info("interactive_pick_captured", kind=kind, selector=bundle.primary_selector[:80])
            return
        if kind == "assert_text":
            expected = (capture.target.text or "").strip()
            if not expected:
                _LOGGER.warning("interactive_pick_assert_text_empty")
                return
            out["expectedText"] = expected
            out["contains"] = bool(pi.get("contains", True))
        self._pick_result = out
        _LOGGER.info("interactive_pick_captured", kind=kind, selector=bundle.primary_selector[:80])

    async def begin_pick(
        self,
        *,
        kind: str,
        state: str = "visible",
        timeout_ms: int = 30_000,
        contains: bool = True,
    ) -> None:
        if not self._session or not self._tab_id:
            raise RuntimeError("Interactive replay not started.")
        page = self._session.get_tab(self._tab_id)
        if page is None:
            raise RuntimeError("No page for interactive pick.")
        self._pick_result = None
        await page.evaluate(
            """(intent) => { window.__agentPickIntent = intent; }""",
            {"kind": kind, "state": state, "timeoutMs": timeout_ms, "contains": contains},
        )

    async def read_interactive_pick_state(self) -> dict[str, Any]:
        if not self._session or not self._tab_id:
            return {"pickPending": False, "pickIntent": None, "pickResult": self._pick_result}
        page = self._session.get_tab(self._tab_id)
        if page is None:
            return {"pickPending": False, "pickIntent": None, "pickResult": self._pick_result}
        try:
            intent = await page.evaluate(
                """() => {
                  const p = window.__agentPickIntent;
                  if (!p || !p.kind) return null;
                  return {
                    kind: p.kind,
                    state: p.state || 'visible',
                    timeoutMs: p.timeoutMs || 30000,
                    contains: p.contains !== false,
                  };
                }"""
            )
        except Exception:  # noqa: BLE001
            return {"pickPending": False, "pickIntent": None, "pickResult": self._pick_result}
        if intent is None:
            return {"pickPending": False, "pickIntent": None, "pickResult": self._pick_result}
        return {"pickPending": True, "pickIntent": intent, "pickResult": self._pick_result}

    async def cancel_pick(self) -> None:
        if not self._session or not self._tab_id:
            return
        page = self._session.get_tab(self._tab_id)
        if page is None:
            return
        try:
            await page.evaluate(
                """() => {
                  if (typeof window.__agentPickCancelUi === 'function') window.__agentPickCancelUi();
                  else { window.__agentPickIntent = null; }
                }"""
            )
        except Exception:  # noqa: BLE001
            pass

    def consume_pick_result(self) -> dict[str, Any] | None:
        r = self._pick_result
        self._pick_result = None
        return r

    async def run_step_id(self, step_id: str) -> None:
        if not self.graph or not self._runner:
            raise RuntimeError("Interactive replay not started.")
        step = next((s for s in self.graph.steps if s.step_id == step_id), None)
        if step is None:
            raise ValueError(f"Unknown stepId: {step_id}")
        if (step.action or "").strip().lower() == "upload":
            self._upload_last_errors.pop(step_id, None)
        self._run_activity = {
            "phase": "running",
            "stepId": step_id,
            "action": step.action,
            "attempt": 1,
            "maxAttempts": 1,
            "label": f"Starting {step.action}…",
        }
        try:
            await self._runner.run_one_step(self.graph, step)
        except Exception as exc:  # noqa: BLE001
            if (step.action or "").strip().lower() == "upload":
                self._upload_last_errors[step_id] = str(exc)[:480]
            raise
        else:
            if (step.action or "").strip().lower() == "upload":
                self._upload_last_errors.pop(step_id, None)
        finally:
            self._run_activity = None

    async def run_range(self, from_step_id: str, to_step_id: str) -> None:
        if not self.graph or not self._runner:
            raise RuntimeError("Interactive replay not started.")
        ids = [s.step_id for s in self.graph.steps]
        if from_step_id not in ids or to_step_id not in ids:
            raise ValueError("fromStepId and toStepId must exist in the current graph.")
        i0, i1 = ids.index(from_step_id), ids.index(to_step_id)
        if i0 > i1:
            i0, i1 = i1, i0
        self._run_activity = {
            "phase": "running",
            "stepId": from_step_id,
            "action": "run_range",
            "label": f"Run range: steps {i0 + 1}–{i1 + 1}…",
        }
        try:
            for step in self.graph.steps[i0 : i1 + 1]:
                sid = step.step_id
                if (step.action or "").strip().lower() == "upload":
                    self._upload_last_errors.pop(sid, None)
                try:
                    await self._runner.run_one_step(self.graph, step)
                except Exception as exc:  # noqa: BLE001
                    if (step.action or "").strip().lower() == "upload":
                        self._upload_last_errors[sid] = str(exc)[:480]
                    raise
        finally:
            self._run_activity = None

    def delete_step(self, step_id: str) -> bool:
        if not self.graph:
            return False
        steps = list(self.graph.steps)
        idx = next((i for i, s in enumerate(steps) if s.step_id == step_id), None)
        if idx is None:
            return False
        new_steps = [s for i, s in enumerate(steps) if i != idx]
        self.graph = _rebuild_linear_edges(self.graph.model_copy(update={"steps": new_steps}))
        self._upload_last_errors.pop(step_id, None)
        return True

    def insert_step(self, after_step_id: str, step: Step) -> str:
        """Insert *step* after ``after_step_id``, or prepend/append using sentinels.

        ``after_step_id`` values:
        - ``__prepend__``: insert at index 0
        - ``""``, ``__append__``, or unknown sentinel handling: append at end
        - otherwise: insert immediately after the step with that id
        """
        if not self.graph:
            raise RuntimeError("Interactive replay not started.")
        tab_id = self._tab_id
        if not tab_id:
            raise RuntimeError("No tab_id for interactive session.")

        md = dict(step.metadata)
        md["tabId"] = tab_id
        if (step.action or "").strip().lower() == "upload" and self._session is not None:
            page = self._session.get_tab(tab_id)
            if page is not None:
                u = (page.url or "").strip()
                if u.startswith(("http://", "https://")):
                    has = any(
                        isinstance(md.get(k), str)
                        and str(md.get(k)).strip().startswith(("http://", "https://"))
                        for k in ("pageUrl", "page_url", "frameUrl", "frame_url")
                    )
                    if not has:
                        md.setdefault("pageUrl", u)
                        md.setdefault("frameUrl", u)
        step_copy = step.model_copy(update={"metadata": md})

        steps = list(self.graph.steps)
        anchor = (after_step_id or "").strip()
        if anchor == "__prepend__":
            new_steps = [step_copy] + steps
        elif anchor in ("", "__append__"):
            new_steps = steps + [step_copy]
        else:
            idx = next((i for i, s in enumerate(steps) if s.step_id == anchor), None)
            if idx is None:
                raise ValueError(f"Unknown afterStepId: {anchor}")
            new_steps = steps[: idx + 1] + [step_copy] + steps[idx + 1 :]

        self.graph = _rebuild_linear_edges(self.graph.model_copy(update={"steps": new_steps}))
        self.last_error = None
        return step_copy.step_id

    async def probe_step_target_for_force_fix(self, step_id: str) -> dict[str, Any]:
        """Check the interactive tab for any selector on this step before applying Force fix.

        Returns a dict with ``ok`` bool. If ``ok`` is false, ``reason`` is ``not_on_page``,
        ``inactive``, or ``no_locator``, and ``message`` is user-facing.
        """
        if self.graph is None or self._session is None or self._tab_id is None:
            return {
                "ok": False,
                "reason": "inactive",
                "message": "Interactive replay is not active.",
            }
        rt = self._tool_runtime
        if rt is None:
            return {
                "ok": False,
                "reason": "inactive",
                "message": "Browser tooling is not ready yet.",
            }
        step = next((s for s in self.graph.steps if s.step_id == step_id), None)
        if step is None or step.target is None:
            return {
                "ok": False,
                "reason": "no_locator",
                "message": "This step has no selector target to check.",
            }
        selectors = _selectors_from_bundle_for_probe(step.target)
        if not selectors:
            return {
                "ok": False,
                "reason": "no_locator",
                "message": "This step has no non-empty selectors.",
            }
        tab_id = self._tab_id
        page = self._session.get_tab(tab_id)
        current_url = page.url if page is not None else ""
        raw_state = _playwright_wait_state_for_probe(step)
        state = cast(
            Literal["attached", "detached", "visible", "hidden"],
            raw_state if raw_state in ("attached", "detached", "visible", "hidden") else "visible",
        )
        n = len(selectors)
        per_timeout = min(4000.0, max(700.0, 14_000.0 / float(n)))
        last_err: str | None = None
        for i, sel in enumerate(selectors, start=1):
            ok, err = await rt.probe_selector(
                tab_id=tab_id,
                selector=sel,
                state=state,
                timeout_ms=per_timeout,
            )
            if ok:
                return {
                    "ok": True,
                    "matchedSelector": sel,
                    "state": state,
                    "currentUrl": current_url,
                    "triedCount": i,
                }
            last_err = err
        msg = (
            "None of this step's selectors match the interactive browser tab right now "
            f"(checked for '{state}'). The element is not present on this page — log in, navigate, "
            "or wait until the UI shows the control, then try Force fix again (or use Pick / re-record)."
        )
        _LOGGER.info(
            "interactive_force_fix_probe_failed",
            step_id=step_id,
            action=step.action,
            tried=len(selectors),
            url=current_url[:200] if current_url else "",
        )
        return {
            "ok": False,
            "reason": "not_on_page",
            "message": msg,
            "currentUrl": current_url,
            "lastError": last_err,
            "triedCount": len(selectors),
            "state": state,
        }

    def force_fix_step_target(self, step_id: str, *, upload_area_on_page: bool = False) -> ForceFixOutcome | None:
        """Merge primary + fallback selectors so the runner tries each in order (graph only).

        For ``upload`` steps when ``upload_area_on_page`` is true, appends canonical fallbacks first
        (see ``_enrich_upload_bundle_with_known_fallbacks``), then applies the usual reorder.

        Returns ``None`` if the step is missing or has no locator bundle. When the ordered
        selector list is already in the relaxed shape, returns ``ForceFixOutcome`` with
        ``changed`` false and does not mutate the graph.
        """
        if not self.graph:
            return None
        step = next((s for s in self.graph.steps if s.step_id == step_id), None)
        if step is None or step.target is None:
            return None
        orig = step.target
        chain_before = _selector_chain(orig)
        tgt = orig
        upload_enriched = False
        if upload_area_on_page and (step.action or "").strip().lower() == "upload":
            tgt, upload_enriched = _enrich_upload_bundle_with_known_fallbacks(tgt)
        new_target = _force_fix_locator_bundle(tgt)
        chain_after = _selector_chain(new_target)
        changed = chain_before != chain_after or upload_enriched
        fb_ct = len([s for s in new_target.fallback_selectors if isinstance(s, str) and s.strip()])
        if not changed:
            msg = (
                "No locator change: this step already uses one ordered primary + fallback list "
                f"({len(chain_after)} non-empty selector(s)), and upload fallbacks were already present "
                "when applicable. Force fix does not call an LLM or invent new selectors."
            )
            return ForceFixOutcome(
                changed=False,
                primary_selector=orig.primary_selector,
                fallback_count=fb_ct,
                message=msg,
            )
        idx = next(i for i, s in enumerate(self.graph.steps) if s.step_id == step_id)
        new_steps = list(self.graph.steps)
        new_steps[idx] = step.model_copy(update={"target": new_target})
        self.graph = self.graph.model_copy(update={"steps": new_steps})
        self.last_error = None
        extra = ""
        if upload_enriched:
            extra = " Added canonical upload fallbacks (upload-area + first file input)."
        msg = (
            f"Locators updated: primary kept first, then {fb_ct} fallback(s) "
            f"({len(chain_after)} strings in run order).{extra} Run this step again to test on the current page."
        )
        return ForceFixOutcome(
            changed=True,
            primary_selector=new_target.primary_selector,
            fallback_count=fb_ct,
            message=msg,
        )

    def save_graph_to_disk(self) -> Path:
        if not self.graph or not self._folder_run_id:
            raise RuntimeError("Interactive replay not started.")
        layout = get_run_layout(self._folder_run_id)
        path = layout.run_dir / "stepgraph.json"
        path.write_text(self.graph.model_dump_json(indent=2, by_alias=True), encoding="utf-8")
        return path.resolve()

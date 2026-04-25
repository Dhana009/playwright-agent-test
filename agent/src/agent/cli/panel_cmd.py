"""agent panel — launch Chromium with the recorder panel injected."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

import typer

from agent.core.ids import generate_run_id
from agent.core.logging import configure_logging, get_logger
from agent.execution.browser import BrowserSession
from agent.execution.tools import ToolRuntime
from agent.locator.engine import LocatorEngine
from agent.panel.bridge import (
    MSG_ACCEPT_LLM_REPAIR,
    MSG_APPEND_STEP,
    MSG_DELETE_STEP,
    MSG_DELETE_VERSION,
    MSG_DUPLICATE_STEP,
    MSG_FORCE_FIX,
    MSG_LIST_VERSIONS,
    MSG_LOAD_VERSION,
    MSG_PAUSE_REQUEST,
    MSG_PICK_CANCEL,
    MSG_PICK_START,
    MSG_REPLAY,
    MSG_RESUME,
    MSG_SAVE_VERSION,
    MSG_SET_LLM_MODE,
    MSG_START_RECORDING,
    MSG_STOP_REPLAY,
    MSG_STOP_RECORDING,
    MSG_VALIDATE_STEP,
    PanelBridge,
)
from agent.recorder.recorder import _CAPTURE_QUEUE_INIT_SCRIPT
from agent.stepgraph.models import (
    LocatorBundle,
    RecoveryPolicy,
    Step,
    StepGraph,
    StepMode,
    TimeoutPolicy,
)
from agent.stepgraph.versions import RecordingVersions

logger = get_logger(__name__)

_DB_ENV_KEY = "AGENT_SQLITE_PATH"
_DEFAULT_DB = Path.home() / ".agent" / "panel.db"

WS_PORT = 8766
HTTP_PORT = 8767


def panel(
    url: Optional[str] = typer.Option(None, "--url", "-u", help="URL to navigate to on launch."),
    headless: bool = typer.Option(False, "--headless", help="Run browser headless (no window)."),
    ws_port: int = typer.Option(WS_PORT, "--ws-port", help="WebSocket bridge port (also serves panel HTML)."),
    storage_state: Optional[Path] = typer.Option(
        None,
        "--storage-state",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Playwright storage state JSON for auth.",
    ),
    db_path: Optional[Path] = typer.Option(
        None,
        "--db",
        help="SQLite database path for version storage.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
) -> None:
    """Launch Chromium with the Playwright Agent panel injected.

    Pick elements, validate actions, record steps, replay — all from the panel.
    Panel docks to the right side of the browser like DevTools.
    """
    asyncio.run(
        _run_panel(
            start_url=url,
            headless=headless,
            ws_port=ws_port,
            storage_state_path=str(storage_state) if storage_state else None,
            db_path=db_path or _DEFAULT_DB,
            verbose=verbose,
        )
    )


async def _run_panel(
    start_url: str | None,
    headless: bool,
    ws_port: int,
    storage_state_path: str | None,
    db_path: Path,
    verbose: bool,
) -> None:
    run_id = generate_run_id()
    try:
        configure_logging(run_id=run_id)
    except Exception:
        pass
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    logger.info("panel_run_starting", run_id=run_id)

    # ── Storage ──────────────────────────────────────────────────────────
    db_path.parent.mkdir(parents=True, exist_ok=True)
    versions_store = RecordingVersions(db_path)

    # ── Browser session ───────────────────────────────────────────────────
    browser_session = BrowserSession(browser_name="chromium", headless=headless)
    await browser_session.start()

    context_opts: dict[str, Any] = {}
    if storage_state_path:
        context_opts["storage_state"] = storage_state_path

    _, context = await browser_session.new_context(**context_opts)
    page = await context.new_page()
    tab_id = browser_session.get_tab_id(page)
    if tab_id is None:
        raise RuntimeError("Failed to get tab_id for the new page")

    tool_runtime = ToolRuntime(browser_session=browser_session)

    # ── Detect LLM availability ───────────────────────────────────────────
    llm_provider = _try_load_llm_provider()

    # ── Bridge ────────────────────────────────────────────────────────────
    bridge = PanelBridge(page, ws_port=ws_port, llm_available=llm_provider is not None)

    # ── In-memory state ───────────────────────────────────────────────────
    step_graph = StepGraph(runId=run_id, steps=[], edges=[])
    locator_engine = LocatorEngine()
    state = _PanelState(
        run_id=run_id,
        tab_id=tab_id,
        step_graph=step_graph,
        tool_runtime=tool_runtime,
        bridge=bridge,
        locator_engine=locator_engine,
        versions_store=versions_store,
        llm_provider=llm_provider,
        page=page,
    )

    # ── Register message handlers ─────────────────────────────────────────
    bridge.on(MSG_PICK_START, state.handle_pick_start)
    bridge.on(MSG_PICK_CANCEL, state.handle_pick_cancel)
    bridge.on(MSG_VALIDATE_STEP, state.handle_validate_step)
    bridge.on(MSG_APPEND_STEP, state.handle_append_step)
    bridge.on(MSG_DELETE_STEP, state.handle_delete_step)
    bridge.on(MSG_DUPLICATE_STEP, state.handle_duplicate_step)
    bridge.on(MSG_REPLAY, state.handle_replay)
    bridge.on(MSG_STOP_REPLAY, state.handle_stop_replay)
    bridge.on(MSG_PAUSE_REQUEST, state.handle_pause_request)
    bridge.on(MSG_RESUME, state.handle_resume)
    bridge.on(MSG_FORCE_FIX, state.handle_force_fix)
    bridge.on(MSG_ACCEPT_LLM_REPAIR, state.handle_accept_llm_repair)
    bridge.on(MSG_SAVE_VERSION, state.handle_save_version)
    bridge.on(MSG_LIST_VERSIONS, state.handle_list_versions)
    bridge.on(MSG_LOAD_VERSION, state.handle_load_version)
    bridge.on(MSG_DELETE_VERSION, state.handle_delete_version)
    bridge.on(MSG_SET_LLM_MODE, state.handle_set_llm_mode)
    bridge.on(MSG_START_RECORDING, state.handle_start_recording)
    bridge.on(MSG_STOP_RECORDING, state.handle_stop_recording)
    bridge.on("set_llm_config", state.handle_set_llm_config)

    # Send full state when panel tab reconnects (close/reopen)
    bridge.on_connect(state.handle_connect)

    # ── Start bridge (WS server + panel injection) ────────────────────────
    await bridge.start()

    # ── Navigate to start URL ─────────────────────────────────────────────
    if start_url:
        await page.goto(start_url, wait_until="domcontentloaded", timeout=30_000)
        logger.info("panel_navigated", url=start_url)

    actual_port = bridge.ws_port
    typer.echo("\n  Playwright Agent panel running.")
    typer.echo(f"  Browser:   {'headless' if headless else 'visible'}")
    typer.echo(f"  WS bridge: ws://127.0.0.1:{actual_port}/ws")
    typer.echo(f"  Panel URL: http://127.0.0.1:{actual_port}/panel")
    if start_url:
        typer.echo(f"  Opened:    {start_url}")
    typer.echo("\n  Press Ctrl+C to stop.\n")

    try:
        # Keep running until Ctrl+C or browser close
        while True:
            await asyncio.sleep(1)
            # Check if browser is still open
            if browser_session._browser is None or not browser_session._browser.is_connected():
                logger.info("browser_closed")
                break
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        typer.echo("\nStopping panel session…")
        await bridge.stop()
        await browser_session.stop()
        logger.info("panel_run_stopped", run_id=run_id)


class _PanelState:
    """Holds mutable state for one panel session and wires bridge messages to actions."""

    def __init__(
        self,
        *,
        run_id: str,
        tab_id: str,
        step_graph: StepGraph,
        tool_runtime: ToolRuntime,
        bridge: PanelBridge,
        locator_engine: LocatorEngine,
        versions_store: RecordingVersions,
        llm_provider: Any | None,
        page: Any,
    ) -> None:
        self.run_id = run_id
        self.tab_id = tab_id
        self.step_graph = step_graph
        self.tool_runtime = tool_runtime
        self.bridge = bridge
        self.locator_engine = locator_engine
        self.versions_store = versions_store
        self.llm_provider = llm_provider
        self.page = page
        self.llm_enabled = llm_provider is not None

        self._replay_task: asyncio.Task[None] | None = None
        self._pause_requested = False
        self._paused_step_id: str | None = None
        self._failing_step_id: str | None = None
        self._failing_step_override: Step | None = None
        self._original_steps: list[Step] = []  # Snapshot taken before first version save
        self._repair_history_by_step: dict[str, list[dict[str, Any]]] = {}
        self._llm_verified: bool | None = None
        self._llm_verify_message: str = ""
        self._llm_verify_task: asyncio.Task[None] | None = None

        # Auto-recording state
        self._recording_active = False
        self._record_poll_task: asyncio.Task[None] | None = None
        self._recorder_injected = False
        # Debounce: track pending fill events — key = semantic_key, value = (value, target)
        self._pending_fills: dict[str, tuple[str, dict[str, Any]]] = {}
        self._last_click_key: str | None = None  # prevent duplicate click for same element
        self._capture_suspend_depth = 0

    # ── Pick ─────────────────────────────────────────────────────────────

    async def handle_pick_start(self, msg: dict[str, Any]) -> None:
        """Activate pick mode in the recorder JS and wait for a pick result."""
        # The recorder already has JS for hover-outline pick — trigger it
        try:
            await self.page.evaluate("""
            (() => {
                window.__agentPickIntent = {kind: 'panel_pick', resolve: null};
                if (typeof __agentEnsurePickUi === 'function') __agentEnsurePickUi();
            })();
            """)
        except Exception as exc:
            logger.debug("pick_start_eval_error", error=str(exc))

        # Listen for pick-click result via panel callback binding
        async def on_pick_result(descriptor_json: str) -> None:
            try:
                descriptor = json.loads(descriptor_json)
            except Exception:
                return
            await self._process_pick_result(descriptor)

        try:
            await self.page.expose_function("__agentPanelPickResult", on_pick_result)
        except Exception:
            pass  # already exposed

        # If recorder script owns pick-click interception, bridge its pick payload to panel flow.
        async def on_pick_emit(payload: Any) -> None:
            if not isinstance(payload, dict):
                return
            target = payload.get("target")
            if isinstance(target, dict):
                await self._process_pick_result(target)

        try:
            await self.page.expose_function("__agentPickEmit", on_pick_emit)
        except Exception:
            pass  # already exposed

        # Inject click capture for pick mode when recorder script is not installed.
        # Recorder-installed pages already intercept pick clicks and call __agentPickEmit.
        await self.page.evaluate("""
        (() => {
            if (window.__agentPickListenerInstalled) return;
            window.__agentPickListenerInstalled = true;
            document.addEventListener('click', function(e) {
                if (window.__agentRecorderInstalled) return;
                if (!window.__agentPickIntent || !window.__agentPickIntent.kind) return;
                const panel = document.getElementById('__agent_panel_host');
                if (panel && e.target === panel) return;
                if (e.target.closest && e.target.closest('[data-agent-recorder-hud]')) return;

                e.preventDefault();
                e.stopPropagation();

                window.__agentPickIntent = null;

                const el = e.target;
                const desc = window.__agentCollectTarget ? window.__agentCollectTarget(el) : null;

                if (desc && typeof window.__agentPanelPickResult === 'function') {
                    window.__agentPanelPickResult(JSON.stringify(desc));
                }
            }, true);
        })();
        """)

    async def handle_pick_cancel(self, _msg: dict[str, Any]) -> None:
        try:
            await self.page.evaluate("window.__agentPickIntent = null;")
        except Exception:
            pass

    async def _process_pick_result(self, descriptor: dict[str, Any]) -> None:
        try:
            ranked = await self.locator_engine.rank_candidates(self.page, descriptor)
            candidates = [
                {
                    "selector": c.selector,
                    "strategy": c.strategy,
                    "label": c.label,
                    "confidenceScore": c.confidence_score,
                    "totalCount": c.total_count,
                    "visibleCount": c.visible_count,
                    "actionable": c.actionable,
                    "frameContext": [],
                    "tag": descriptor.get("tag", ""),
                    "inputType": descriptor.get("inputType", ""),
                }
                for c in ranked[:6]
            ]
            await self.bridge.broadcast_pick_result(descriptor, candidates)
        except Exception as exc:
            logger.error("pick_result_error", error=str(exc))
            await self.bridge.broadcast_pick_result(descriptor, [])

    # ── Validate ──────────────────────────────────────────────────────────

    async def handle_validate_step(self, msg: dict[str, Any]) -> None:
        payload = msg.get("payload", {})
        action = payload.get("action", "")
        locator = payload.get("locator")
        params = payload.get("params") or {}
        repair_context = payload.get("repairContext") or {}
        repair_step_id = repair_context.get("stepId")

        start_ms = int(time.time() * 1000)
        try:
            await self._suspend_auto_capture()
            try:
                await self._execute_action(action, locator, params)
            finally:
                await self._resume_auto_capture()
            duration_ms = int(time.time() * 1000) - start_ms
            if repair_step_id:
                self._append_repair_history(
                    repair_step_id,
                    {
                        "kind": "post_apply_test",
                        "locator": locator or "",
                        "passed": True,
                        "durationMs": duration_ms,
                    },
                )
            await self.bridge.broadcast_validate_result(passed=True, duration_ms=duration_ms)
        except Exception as exc:
            duration_ms = int(time.time() * 1000) - start_ms
            if repair_step_id:
                self._append_repair_history(
                    repair_step_id,
                    {
                        "kind": "post_apply_test",
                        "locator": locator or "",
                        "passed": False,
                        "error": _friendly_error(str(exc)),
                        "durationMs": duration_ms,
                    },
                )
            await self.bridge.broadcast_validate_result(
                passed=False, error=_friendly_error(str(exc)), duration_ms=duration_ms
            )

    # ── Append step ───────────────────────────────────────────────────────

    async def handle_append_step(self, msg: dict[str, Any]) -> None:
        payload = msg.get("payload", {})
        action = payload.get("action", "")
        locator = payload.get("locator")
        params = payload.get("params") or {}
        insert_after_step_id = payload.get("insertAfterStepId")

        step = _build_step(
            action=action,
            locator=locator,
            params=params,
            tab_id=self.tab_id,
        )

        if insert_after_step_id:
            idx = next(
                (i for i, s in enumerate(self.step_graph.steps) if s.step_id == insert_after_step_id),
                None,
            )
            if idx is not None:
                self.step_graph.steps.insert(idx + 1, step)
            else:
                self.step_graph.steps.append(step)
        else:
            self.step_graph.steps.append(step)

        step_dict = _step_to_dict(step, locator, params)
        await self.bridge.broadcast_step_appended(step_dict)
        logger.info("step_appended", step_id=step.step_id, action=action)

    async def handle_delete_step(self, msg: dict[str, Any]) -> None:
        step_id = msg.get("payload", {}).get("stepId")
        self.step_graph.steps = [s for s in self.step_graph.steps if s.step_id != step_id]

    async def handle_duplicate_step(self, msg: dict[str, Any]) -> None:
        step_id = msg.get("payload", {}).get("stepId")
        original = next((s for s in self.step_graph.steps if s.step_id == step_id), None)
        if not original:
            return
        from agent.core.ids import generate_step_id
        new_step = original.model_copy(update={"step_id": generate_step_id()})
        idx = next(i for i, s in enumerate(self.step_graph.steps) if s.step_id == step_id)
        self.step_graph.steps.insert(idx + 1, new_step)
        locator = original.metadata.get("locator")
        params = original.metadata.get("params") or {}
        step_dict = _step_to_dict(new_step, locator, params)
        await self.bridge.broadcast_step_appended(step_dict)

    # ── Replay ────────────────────────────────────────────────────────────

    async def handle_replay(self, msg: dict[str, Any]) -> None:
        if self._recording_active:
            await self.handle_stop_recording({})
        if self._replay_task and not self._replay_task.done():
            self._replay_task.cancel()
        self._pause_requested = False
        self._paused_step_id = None
        self._failing_step_id = None
        start_step_id = msg.get("payload", {}).get("fromStepId")
        self._replay_task = asyncio.create_task(
            self._run_replay(start_step_id=start_step_id)
        )

    async def handle_stop_replay(self, _msg: dict[str, Any]) -> None:
        should_broadcast_abort = False
        if self._replay_task and not self._replay_task.done():
            self._replay_task.cancel()
            should_broadcast_abort = True
        elif self._paused_step_id is not None or self._failing_step_id is not None:
            should_broadcast_abort = True
        self._pause_requested = False
        self._paused_step_id = None
        self._failing_step_id = None
        if should_broadcast_abort:
            await self.bridge.send({"type": "run_aborted", "payload": {}})

    async def _run_replay(self, start_step_id: str | None = None) -> None:
        await self._suspend_auto_capture()
        try:
            started = start_step_id is None
            for step in self.step_graph.steps:
                if not started:
                    if step.step_id == start_step_id:
                        started = True
                    else:
                        continue

                if self._pause_requested:
                    self._pause_requested = False
                    self._paused_step_id = step.step_id
                    await self.bridge.broadcast_pause(step.step_id, "Pause requested")
                    return

                await self.bridge.broadcast_step_status(step.step_id, "running")
                try:
                    locator = step.metadata.get("locator")
                    params = step.metadata.get("params") or {}
                    await self._execute_action(step.action, locator, params)
                    await self.bridge.broadcast_step_status(step.step_id, "passed")
                except Exception as exc:
                    self._failing_step_id = step.step_id
                    await self.bridge.broadcast_step_status(step.step_id, "failed", error=_friendly_error(str(exc)))
                    await self.bridge.broadcast_pause(step.step_id, str(exc))
                    return

            await self.bridge.send({"type": "run_completed", "payload": {}})
        finally:
            await self._resume_auto_capture()

    async def handle_pause_request(self, _msg: dict[str, Any]) -> None:
        self._pause_requested = True

    async def handle_resume(self, msg: dict[str, Any]) -> None:
        payload = msg.get("payload", {})
        override = payload.get("overrideStep")
        applied_step_id: str | None = None
        applied_locator: str | None = None
        applied_params: dict[str, Any] | None = None

        if override:
            # Apply the override to the step graph
            override_step_id = (
                override.get("stepId")
                or self._failing_step_id
                or self._paused_step_id
            )
            new_locator = override.get("locator")
            new_params = override.get("params")
            if override_step_id:
                for s in self.step_graph.steps:
                    if s.step_id == override_step_id:
                        if new_locator:
                            s.metadata["locator"] = new_locator
                            # Update the LocatorBundle primary selector
                            if s.target:
                                bundle = s.target.model_copy(update={"primary_selector": new_locator})
                                object.__setattr__(s, "target", bundle)
                        if new_params:
                            s.metadata["params"] = new_params
                        applied_step_id = override_step_id
                        applied_locator = new_locator
                        applied_params = new_params if isinstance(new_params, dict) else None
                        logger.info(
                            "resume_override_applied step_id=%s locator=%s has_params=%s",
                            applied_step_id,
                            bool(applied_locator),
                            bool(applied_params),
                        )
                        break

        if applied_step_id:
            await self.bridge.broadcast_repair_applied(
                step_id=applied_step_id,
                locator=applied_locator,
                params=applied_params,
            )

        resume_from = self._failing_step_id or self._paused_step_id or applied_step_id
        self._pause_requested = False
        self._paused_step_id = None
        self._failing_step_id = None

        if self._replay_task and not self._replay_task.done():
            self._replay_task.cancel()

        self._replay_task = asyncio.create_task(
            self._run_replay(start_step_id=resume_from)
        )

    # ── Force-fix ─────────────────────────────────────────────────────────

    async def handle_force_fix(self, msg: dict[str, Any]) -> None:
        try:
            payload = msg.get("payload", {})
            step_id = payload.get("stepId") or self._failing_step_id
            if not step_id:
                await self.bridge.broadcast_force_fix_progress(
                    stage=4,
                    status="fail",
                    repaired=False,
                    explanation="Auto-fix could not start because no failing step is selected.",
                    meta={"failureCode": "no_failing_step"},
                )
                return

            step = next((s for s in self.step_graph.steps if s.step_id == step_id), None)
            if not step:
                await self.bridge.broadcast_force_fix_progress(
                    stage=4,
                    status="fail",
                    repaired=False,
                    explanation=f"Auto-fix could not start because step {step_id} was not found.",
                    meta={"failureCode": "step_not_found", "stepId": step_id},
                )
                return

            from agent.healing.force_fix import run_force_fix_cascade

            logger.info("force_fix_requested", step_id=step_id, has_override=bool(payload.get("primarySelector")))
            primary = payload.get("primarySelector") or (step.target.primary_selector if step.target else "")
            fallbacks = step.target.fallback_selectors if step.target else []
            descriptor = step.metadata.get("descriptor") or {}
            params = payload.get("params") or step.metadata.get("params") or {}
            user_hint = (payload.get("userHint") or "").strip() or None
            history_hint = self._build_repair_history_hint(step_id)
            if history_hint:
                user_hint = f"{(user_hint or '').strip()}\n\n{history_hint}".strip()

            async def on_progress(
                stage: int,
                status: str,
                repaired: bool,
                locator: str | None,
                explanation: str | None,
                meta: dict[str, Any] | None,
            ) -> None:
                await self.bridge.broadcast_force_fix_progress(
                    stage=stage,
                    status=status,
                    repaired=repaired,
                    locator=locator,
                    explanation=explanation,
                    meta=meta,
                )

            result = await run_force_fix_cascade(
                self.page,
                step_id=step_id,
                action=step.action,
                primary_selector=primary,
                fallback_selectors=fallbacks,
                target_descriptor=descriptor,
                params=params,
                llm_provider=self.llm_provider if self.llm_enabled else None,
                on_progress=on_progress,
                user_hint=user_hint,
            )

            if result.repaired:
                self._failing_step_id = step_id  # Keep for resume
            self._append_repair_history(
                step_id,
                {
                    "kind": "autofix_attempt",
                    "stage": result.stage,
                    "repaired": result.repaired,
                    "locator": result.locator or "",
                    "explanation": result.explanation or "",
                    "candidatesTried": result.candidates_tried[-8:],
                },
            )
            logger.info("force_fix_done", repaired=result.repaired, stage=result.stage)
        except Exception as exc:
            logger.error("force_fix_unhandled_error error=%s", str(exc))
            await self.bridge.broadcast_force_fix_progress(
                stage=4,
                status="fail",
                repaired=False,
                explanation=f"Auto-fix internal error: {str(exc)[:220]}",
                meta={"failureCode": "internal_error"},
            )

    def _append_repair_history(self, step_id: str, entry: dict[str, Any]) -> None:
        if not step_id:
            return
        bucket = self._repair_history_by_step.setdefault(step_id, [])
        bucket.append(entry)
        # Keep recent entries only to limit hint/token growth.
        if len(bucket) > 12:
            del bucket[:-12]

    def _build_repair_history_hint(self, step_id: str) -> str:
        entries = self._repair_history_by_step.get(step_id) or []
        if not entries:
            return ""
        lines: list[str] = ["## Previous repair attempts (most recent first)"]
        for entry in reversed(entries[-5:]):
            if entry.get("kind") == "post_apply_test":
                locator = str(entry.get("locator") or "")[:140]
                if entry.get("passed"):
                    lines.append(f"- Post-apply test passed for locator: {locator}")
                else:
                    err = str(entry.get("error") or "validation failed")[:160]
                    lines.append(f"- Post-apply test failed for locator: {locator} · error: {err}")
                continue
            stage = entry.get("stage")
            repaired = bool(entry.get("repaired"))
            locator = str(entry.get("locator") or "")[:140]
            expl = str(entry.get("explanation") or "")[:180]
            lines.append(
                f"- Auto-fix stage {stage} {'succeeded' if repaired else 'failed'}"
                + (f" · locator: {locator}" if locator else "")
                + (f" · note: {expl}" if expl else "")
            )
        return "\n".join(lines)

    async def handle_accept_llm_repair(self, msg: dict[str, Any]) -> None:
        payload = msg.get("payload", {})
        step_id = payload.get("stepId") or self._failing_step_id
        locator = payload.get("locator", "")
        if not step_id or not locator:
            return
        for s in self.step_graph.steps:
            if s.step_id == step_id:
                s.metadata["locator"] = locator
                if s.target:
                    bundle = LocatorBundle(
                        primarySelector=locator,
                        fallbackSelectors=s.target.fallback_selectors,
                        confidenceScore=0.75,
                    )
                    object.__setattr__(s, "target", bundle)
                break

    # ── Versions ──────────────────────────────────────────────────────────

    async def handle_save_version(self, msg: dict[str, Any]) -> None:
        payload = msg.get("payload", {})
        name = payload.get("name", "")
        step_ids = payload.get("stepIds", [])
        if not name or not step_ids:
            return
        # Take a snapshot of "main" before first version save
        if not self._original_steps:
            self._original_steps = list(self.step_graph.steps)
        all_steps = [_step_to_dict(s, s.metadata.get("locator"), s.metadata.get("params")) for s in self.step_graph.steps]
        await self.versions_store.save_version(self.run_id, name, step_ids, all_steps)
        await self._send_versions_list()

    async def handle_list_versions(self, _msg: dict[str, Any]) -> None:
        await self._send_versions_list()

    async def handle_load_version(self, msg: dict[str, Any]) -> None:
        name = msg.get("payload", {}).get("name", "main")
        if name == "main":
            # Restore original steps (pre-save snapshot, or current full list)
            restore = self._original_steps if self._original_steps else self.step_graph.steps
            steps_dicts = [
                _step_to_dict(s, s.metadata.get("locator"), s.metadata.get("params"))
                for s in restore
            ]
            self.step_graph.steps = list(restore)
            await self.bridge.send({
                "type": "version_loaded",
                "payload": {"name": "main", "steps": steps_dicts},
            })
            await self._send_versions_list()
            return
        steps = await self.versions_store.load_version(self.run_id, name)
        if steps is not None:
            self.step_graph.steps = [
                _step_from_dict(step_dict, self.tab_id)
                for step_dict in steps
            ]
            await self.bridge.send({
                "type": "version_loaded",
                "payload": {"name": name, "steps": steps},
            })

    async def handle_delete_version(self, msg: dict[str, Any]) -> None:
        name = msg.get("payload", {}).get("name", "")
        if not name or name == "main":
            return
        await self.versions_store.delete_version(self.run_id, name)
        await self._send_versions_list()

    async def _send_versions_list(self) -> None:
        versions = await self.versions_store.list_versions(self.run_id)
        step_count = len(self.step_graph.steps)
        all_versions = [{"name": "main", "stepCount": step_count}] + versions
        await self.bridge.broadcast_versions(all_versions)

    # ── LLM mode ─────────────────────────────────────────────────────────

    async def handle_set_llm_mode(self, msg: dict[str, Any]) -> None:
        self.llm_enabled = bool(msg.get("payload", {}).get("enabled", False))
        logger.info("llm_mode_changed", enabled=self.llm_enabled)

    async def _verify_and_broadcast_llm(self) -> None:
        """Background task: probe the LLM and send verified status to the panel."""
        if self.llm_provider is None:
            return
        try:
            from agent.healing.force_fix import verify_llm_connection
            verified, verify_msg = await verify_llm_connection(self.llm_provider)
            self._llm_verified = verified
            self._llm_verify_message = verify_msg
            logger.info("llm_verify", verified=verified, message=verify_msg)
            await self.bridge.send({
                "type": "llm_status",
                "payload": {
                    "available": True,
                    "verified": verified,
                    "verify_message": verify_msg,
                },
            })
        except Exception as exc:
            logger.debug("llm_verify_error error=%s", str(exc))
        finally:
            self._llm_verify_task = None

    def _schedule_llm_verify(self) -> None:
        if self.llm_provider is None:
            return
        if self._llm_verify_task and not self._llm_verify_task.done():
            return
        self._llm_verify_task = asyncio.create_task(self._verify_and_broadcast_llm())

    # ── Auto-recording ────────────────────────────────────────────────────────

    async def handle_start_recording(self, _msg: dict[str, Any]) -> None:
        if self._recording_active:
            return
        self._recording_active = True
        logger.info("auto_recording_started")

        # Inject capture queue script if not already done
        if not self._recorder_injected:
            self._recorder_injected = True
            try:
                await self.page.add_init_script(_CAPTURE_QUEUE_INIT_SCRIPT)
                await self.page.evaluate(_CAPTURE_QUEUE_INIT_SCRIPT)
            except Exception as exc:
                logger.debug("recorder_inject_error error=%s", str(exc))

        # Arm the recorder
        try:
            await self.page.evaluate("window.__agentRecorderArmed = true;")
        except Exception:
            pass

        # Start polling for captured events
        self._record_poll_task = asyncio.create_task(self._auto_record_poll())

    async def handle_stop_recording(self, _msg: dict[str, Any]) -> None:
        self._recording_active = False
        if self._record_poll_task:
            self._record_poll_task.cancel()
            self._record_poll_task = None
        # Flush any buffered fill events before stopping
        await self._flush_pending_fills()
        try:
            await self.page.evaluate("window.__agentRecorderArmed = false;")
        except Exception:
            pass
        logger.info("auto_recording_stopped")

    async def _auto_record_poll(self) -> None:
        """Poll the in-page capture queue and convert events to steps."""
        while self._recording_active:
            try:
                await asyncio.sleep(0.15)
                events = await self.page.evaluate("""
                    (() => {
                        if (!window.__agentRecorderDrain) return [];
                        return window.__agentRecorderDrain();
                    })()
                """)
                if events:
                    for evt in events:
                        await self._process_capture_event(evt)
                # After processing the batch, flush any pending fills that haven't been
                # superseded (i.e. no more input events for that element in this batch)
                await self._flush_pending_fills()
                # Deduplicate only within one poll cycle.
                self._last_click_key = None
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("auto_record_poll_error error=%s", str(exc))

    async def _process_capture_event(self, evt: dict[str, Any]) -> None:
        """Convert a raw capture event into a step and broadcast it."""
        if self._capture_suspend_depth > 0:
            logger.debug("auto_capture_suppressed event_type=%s", evt.get("eventType", ""))
            return
        event_type = evt.get("eventType", "")
        target = evt.get("target") or {}
        semantic_key = target.get("targetSemanticKey") or ""

        if event_type == "input":
            # Debounce: buffer input events — only emit the final value per element.
            # The batch is flushed after all events in a poll cycle are processed.
            value = evt.get("value", "")
            self._pending_fills[semantic_key or id(evt)] = (value, target)
            return

        # For non-input events, flush any pending fill for the same element first
        # (e.g. user typed then clicked away — emit fill before the click)
        if semantic_key and semantic_key in self._pending_fills:
            fill_value, fill_target = self._pending_fills.pop(semantic_key)
            await self._emit_step_from_target("fill", fill_target, {"text": fill_value, "value": fill_value})

        if event_type == "click":
            # Skip duplicate click on same element within the same poll cycle
            if semantic_key and semantic_key == self._last_click_key:
                logger.debug("auto_click_deduped semantic_key=%s", semantic_key)
                return
            self._last_click_key = semantic_key
            await self._emit_step_from_target("click", target, {})

        elif event_type == "keydown":
            key = evt.get("key", "")
            if key in ("Enter", "Tab", "Escape", "Backspace", "Delete"):
                self._last_click_key = None
                await self._emit_step_from_target("press", target, {"text": key, "key": key})
            # Skip non-special keydowns
        else:
            pass  # Unknown event type — ignore

    async def _flush_pending_fills(self) -> None:
        """Emit any buffered fill steps that haven't been displaced by another event."""
        if not self._pending_fills:
            return
        for _key, (value, target) in list(self._pending_fills.items()):
            await self._emit_step_from_target("fill", target, {"text": value, "value": value})
        self._pending_fills.clear()

    async def _emit_step_from_target(
        self, action: str, target: dict[str, Any], params: dict[str, Any]
    ) -> None:
        """Rank locators for a target descriptor and emit a step."""
        try:
            ranked = await self.locator_engine.rank_candidates(self.page, target)
        except Exception:
            ranked = []

        if not ranked:
            # Auto mode should not silently drop clicks just because the best-candidate
            # path was ambiguous; retry with force and finally xpath fallback.
            try:
                ranked = await self.locator_engine.rank_candidates(self.page, target, force=True)
            except Exception:
                ranked = []
            if not ranked:
                fallback = _fallback_locator_from_target(target)
                if not fallback:
                    logger.debug("auto_step_dropped_no_locator action=%s target=%s", action, target.get("targetSemanticKey", ""))
                    return
                ranked = [
                    type(
                        "_AutoFallbackCandidate",
                        (),
                        {
                            "selector": fallback,
                            "confidence_score": 0.25,
                        },
                    )()
                ]

        locator = ranked[0].selector
        fallbacks = [c.selector for c in ranked[1:4]]

        step = _build_step(action=action, locator=locator, params=params, tab_id=self.tab_id)
        if step.target and fallbacks:
            from agent.stepgraph.models import LocatorBundle as LB
            bundle = LB(primarySelector=locator, fallbackSelectors=fallbacks, confidenceScore=ranked[0].confidence_score)
            object.__setattr__(step, "target", bundle)
        step.metadata["descriptor"] = target

        self.step_graph.steps.append(step)
        step_dict = _step_to_dict(step, locator, params)
        await self.bridge.broadcast_step_appended(step_dict)
        logger.debug("auto_step_recorded action=%s locator=%s", action, locator)

    async def handle_set_llm_config(self, msg: dict[str, Any]) -> None:
        """Update LLM provider from panel Settings UI."""
        payload = msg.get("payload", {})
        provider_name = payload.get("provider", "").lower()
        api_key = payload.get("apiKey", "")
        model = payload.get("model", "")
        api_base = payload.get("apiBase", "")

        import os
        new_provider = None
        default_model = model or ""
        try:
            if provider_name in ("openai",) and api_key:
                os.environ["OPENAI_API_KEY"] = api_key
                from agent.llm.openai import OpenAIProvider
                new_provider = OpenAIProvider(default_model=default_model or "gpt-4o")
            elif provider_name in ("anthropic", "claude") and api_key:
                os.environ["ANTHROPIC_API_KEY"] = api_key
                from agent.llm.anthropic import AnthropicProvider
                new_provider = AnthropicProvider(default_model=default_model or "claude-3-5-sonnet-20241022")
            elif provider_name in ("openai_compatible", "compatible") and api_base:
                if api_key:
                    os.environ["OPENAI_API_KEY"] = api_key
                from agent.llm.openai_compatible import OpenAICompatibleProvider
                new_provider = OpenAICompatibleProvider(
                    default_model=default_model or "local-model",
                    base_url=api_base,
                )
        except Exception as exc:
            logger.warning("set_llm_config_failed", error=str(exc))
            await self.bridge.send({"type": "llm_config_updated", "payload": {"success": False, "error": str(exc)}})
            return

        self.llm_provider = new_provider
        self.llm_enabled = new_provider is not None
        self._llm_verified = None
        self._llm_verify_message = "Checking connection…" if new_provider else "No provider configured."
        logger.info("llm_config_updated", provider=provider_name, has_provider=new_provider is not None)

        # Persist to ~/.agent/llm_config.json
        _persist_llm_config(provider_name, api_key, model, api_base)

        # Send immediate "configured, verifying…" response
        await self.bridge.send({
            "type": "llm_config_updated",
            "payload": {
                "success": True,
                "provider": provider_name,
                "model": model,
                "available": new_provider is not None,
                "verified": None,
                "verify_message": "Checking connection…" if new_provider else "No provider configured.",
            },
        })

        # Verify once after config change and cache the result for reconnects.
        if new_provider is not None:
            await self._verify_and_broadcast_llm()
        else:
            self._llm_verified = False
            self._llm_verify_message = "No provider configured."
            await self.bridge.send({
                "type": "llm_status",
                "payload": {"available": False, "verified": False, "verify_message": "No provider configured."},
            })

    # ── Connect (panel tab open / reopen) ─────────────────────────────────

    async def handle_connect(self, _msg: dict[str, Any]) -> None:
        """Send full session state when panel WebSocket (re)connects."""
        steps = [
            _step_to_dict(s, s.metadata.get("locator"), s.metadata.get("params"))
            for s in self.step_graph.steps
        ]
        # Restore any UI statuses if replay was paused
        for step_dict in steps:
            step_id = step_dict["stepId"]
            if step_id == self._failing_step_id:
                step_dict["_uiStatus"] = "failed"
            elif self._paused_step_id and step_id == self._paused_step_id:
                step_dict["_uiStatus"] = "paused"

        await self.bridge.send({"type": "steps_state", "payload": {"steps": steps}})
        await self._send_versions_list()

        # Send persisted LLM config so the settings form pre-populates
        _cfg_path = Path.home() / ".agent" / "llm_config.json"
        if _cfg_path.exists():
            try:
                _cfg = json.loads(_cfg_path.read_text())
                await self.bridge.send({
                    "type": "llm_config",
                    "payload": {
                        "provider": _cfg.get("provider", ""),
                        "model": _cfg.get("model", ""),
                        "apiBase": _cfg.get("api_base", ""),
                        "hasApiKey": bool(_cfg.get("api_key")),
                    },
                })
            except Exception:
                pass

        # Send LLM status; only verify when we do not already have a cached result.
        if self.llm_provider is None:
            self._llm_verified = False
            self._llm_verify_message = "No provider configured."
            await self.bridge.send({
                "type": "llm_status",
                "payload": {"available": False, "verified": False, "verify_message": "No provider configured."},
            })
        elif self._llm_verified is None:
            await self.bridge.send({
                "type": "llm_status",
                "payload": {
                    "available": True,
                    "verified": None,
                    "verify_message": "Checking connection…",
                },
            })
            self._schedule_llm_verify()
        else:
            await self.bridge.send({
                "type": "llm_status",
                "payload": {
                    "available": True,
                    "verified": self._llm_verified,
                    "verify_message": self._llm_verify_message,
                },
            })

        if self._failing_step_id:
            await self.bridge.broadcast_pause(self._failing_step_id, "Previously paused — click Resume or Force-fix")
        elif self._paused_step_id:
            await self.bridge.broadcast_pause(self._paused_step_id, "Previously paused — click Resume")

    # ── Action execution ──────────────────────────────────────────────────

    async def _execute_action(
        self,
        action: str,
        locator: str | None,
        params: dict[str, Any],
    ) -> None:
        """Execute a single action against the live page via the tool runtime."""
        tab_id = self.tab_id
        t = action.replace("-", "_")
        text = params.get("text") or params.get("value") or ""
        timeout_ms = 10_000.0
        try:
            custom_timeout = params.get("timeoutMs")
            if custom_timeout is None:
                custom_timeout = params.get("timeout_ms")
            if custom_timeout is not None:
                timeout_ms = float(custom_timeout)
            if timeout_ms <= 0:
                timeout_ms = 10_000.0
        except Exception:
            timeout_ms = 10_000.0

        if t == "click":
            await self.tool_runtime.click(tab_id=tab_id, target=_require(locator, "click"), timeout_ms=timeout_ms)
        elif t == "fill":
            await self.tool_runtime.fill(tab_id=tab_id, target=_require(locator, "fill"), text=text, timeout_ms=timeout_ms)
        elif t == "type":
            await self.tool_runtime.type(tab_id=tab_id, target=_require(locator, "type"), text=text, timeout_ms=timeout_ms)
        elif t == "check":
            await self.tool_runtime.check(tab_id=tab_id, target=_require(locator, "check"), timeout_ms=timeout_ms)
        elif t == "uncheck":
            await self.tool_runtime.uncheck(tab_id=tab_id, target=_require(locator, "uncheck"), timeout_ms=timeout_ms)
        elif t == "hover":
            await self.tool_runtime.hover(tab_id=tab_id, target=_require(locator, "hover"), timeout_ms=timeout_ms)
        elif t == "focus":
            await self.tool_runtime.focus(tab_id=tab_id, target=_require(locator, "focus"), timeout_ms=timeout_ms)
        elif t == "press":
            key = params.get("text") or params.get("key") or "Enter"
            await self.tool_runtime.press(tab_id=tab_id, target=_require(locator, "press"), key=key, timeout_ms=timeout_ms)
        elif t == "select":
            await self.tool_runtime.select(tab_id=tab_id, target=_require(locator, "select"), value=text, timeout_ms=timeout_ms)
        elif t == "upload":
            path = params.get("path") or params.get("file_paths") or text
            await self.tool_runtime.upload(tab_id=tab_id, target=_require(locator, "upload"), file_paths=path, timeout_ms=timeout_ms)
        elif t == "navigate":
            url = params.get("url") or text
            if not url:
                raise ValueError("navigate requires a URL")
            await self.tool_runtime.navigate(tab_id=tab_id, url=url, wait_until="load", timeout_ms=30_000)
        elif t == "navigate_back":
            await self.tool_runtime.navigate_back(tab_id=tab_id, wait_until="load", timeout_ms=30_000)
        elif t == "assert_visible":
            await self.tool_runtime.assert_visible(tab_id=tab_id, target=_require(locator, "assert-visible"), timeout_ms=timeout_ms)
        elif t == "assert_hidden":
            await self.tool_runtime.assert_hidden(tab_id=tab_id, target=_require(locator, "assert-hidden"), timeout_ms=timeout_ms)
        elif t == "assert_text":
            expected = params.get("text") or params.get("expected") or ""
            await self.tool_runtime.assert_text(tab_id=tab_id, target=_require(locator, "assert-text"), expected=expected, contains=True, timeout_ms=timeout_ms)
        elif t == "assert_value":
            expected = params.get("value") or params.get("expected") or ""
            await self.tool_runtime.assert_value(tab_id=tab_id, target=_require(locator, "assert-value"), expected=expected, timeout_ms=timeout_ms)
        elif t == "assert_url":
            expected = params.get("text") or params.get("expected") or ""
            await self.tool_runtime.assert_url(tab_id=tab_id, expected=expected, contains=True)
        elif t == "assert_title":
            expected = params.get("text") or params.get("expected") or ""
            await self.tool_runtime.assert_title(tab_id=tab_id, expected=expected, contains=True)
        elif t == "assert_checked":
            await self.tool_runtime.assert_checked(tab_id=tab_id, target=_require(locator, "assert-checked"), timeout_ms=timeout_ms)
        elif t == "assert_enabled":
            await self.tool_runtime.assert_enabled(tab_id=tab_id, target=_require(locator, "assert-enabled"), timeout_ms=timeout_ms)
        elif t == "wait_timeout":
            ms = int(params.get("value") or params.get("timeoutMs") or 5000)
            await self.tool_runtime.wait_timeout(tab_id=tab_id, timeout_ms=ms)
        elif t == "wait_for":
            state = params.get("state") or "visible"
            if locator:
                await self.tool_runtime.wait_for(tab_id=tab_id, target=locator, state=state, timeout_ms=timeout_ms)
        elif t == "dialog_handle":
            dialog_action = params.get("action") or "accept"
            await self.tool_runtime.dialog_handle(tab_id=tab_id, accept=(dialog_action == "accept"))
        else:
            raise ValueError(f"Unsupported action: {action}")

    async def _suspend_auto_capture(self) -> None:
        self._capture_suspend_depth += 1
        if self._capture_suspend_depth == 1:
            self._pending_fills.clear()
            self._last_click_key = None
            if self._recording_active:
                try:
                    await self.page.evaluate("window.__agentRecorderArmed = false;")
                except Exception:
                    pass

    async def _resume_auto_capture(self) -> None:
        if self._capture_suspend_depth > 0:
            self._capture_suspend_depth -= 1
        if self._capture_suspend_depth == 0 and self._recording_active:
            try:
                # Drop any queued events captured right around suspend/resume boundaries.
                await self.page.evaluate("""
                    (() => {
                        if (window.__agentRecorderDrain) window.__agentRecorderDrain();
                        window.__agentRecorderArmed = true;
                    })()
                """)
            except Exception:
                pass


# ── Helpers ────────────────────────────────────────────────────────────────────

def _require(locator: str | None, action: str) -> str:
    if not locator:
        raise ValueError(f"Action '{action}' requires a locator (pick an element first).")
    return locator


def _build_step(
    action: str,
    locator: str | None,
    params: dict[str, Any],
    tab_id: str,
) -> Step:
    mode = _infer_mode(action)
    target = None
    if locator:
        target = LocatorBundle(
            primarySelector=locator,
            fallbackSelectors=[],
            confidenceScore=0.9,
        )
    return Step(
        mode=mode,
        action=action.replace("-", "_"),
        target=target,
        timeout_policy=TimeoutPolicy(timeoutMs=15_000),
        recovery_policy=RecoveryPolicy(maxRetries=0),
        metadata={"tabId": tab_id, "locator": locator, "params": params},
    )


def _infer_mode(action: str) -> StepMode:
    if action.startswith("assert") or action.startswith("assert-"):
        return StepMode.ASSERTION
    if action in ("navigate", "navigate-back", "navigate_back"):
        return StepMode.NAVIGATION
    if action in ("wait-for", "wait_for", "wait-timeout", "wait_timeout"):
        return StepMode.WAIT
    return StepMode.ACTION


def _step_to_dict(step: Step, locator: str | None, params: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "stepId": step.step_id,
        "action": step.action,
        "locator": locator or (step.target.primary_selector if step.target else None),
        "params": params or step.metadata.get("params") or {},
        "mode": step.mode.value,
        "_uiStatus": "pending",
    }


def _friendly_error(error: str) -> str:
    if "TimeoutError" in error or "Timeout" in error:
        return "Timeout — element not found within time limit (try higher timeout or fix locator)"
    if "not visible" in error or "not actionable" in error:
        return "Element not actionable"
    if "locator not found" in error or "No element" in error or "strict mode" in error:
        return "Locator not found — no matching element"
    if "Expected" in error and "received" in error:
        return error[:120]
    return error[:120]


def _fallback_locator_from_target(target: dict[str, Any]) -> str | None:
    xpath = target.get("absoluteXPath")
    if isinstance(xpath, str) and xpath.strip():
        return f"xpath={xpath.strip()}"
    return None


def _step_from_dict(raw: dict[str, Any], tab_id: str) -> Step:
    action = str(raw.get("action") or "click")
    locator = raw.get("locator")
    params = raw.get("params") or {}
    step = _build_step(action=action, locator=locator, params=params, tab_id=tab_id)
    step_id = raw.get("stepId")
    if isinstance(step_id, str) and step_id.strip():
        step = step.model_copy(update={"step_id": step_id})
    return step


def _try_load_llm_provider() -> Any | None:
    """Try to instantiate an LLM provider from environment config or persisted config."""
    import os
    _load_persisted_llm_config()
    provider_name = os.environ.get("AGENT_LLM_PROVIDER", "")
    logger.info("llm_provider_load_attempt provider=%r has_key=%r", provider_name, bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")))
    if not provider_name:
        return None
    try:
        if provider_name.lower() in ("anthropic", "claude"):
            default_model = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022")
            from agent.llm.anthropic import AnthropicProvider
            return AnthropicProvider(default_model=default_model)
        if provider_name.lower() in ("openai",):
            default_model = os.environ.get("OPENAI_MODEL", "gpt-4o")
            from agent.llm.openai import OpenAIProvider
            return OpenAIProvider(default_model=default_model)
        if provider_name.lower() in ("openai_compatible", "compatible"):
            api_base = os.environ.get("OPENAI_API_BASE", "")
            default_model = os.environ.get("OPENAI_MODEL", "local-model")
            if api_base:
                from agent.llm.openai_compatible import OpenAICompatibleProvider
                return OpenAICompatibleProvider(default_model=default_model, base_url=api_base)
    except Exception as exc:
        import traceback
        logger.warning("llm_provider_load_failed provider=%r error=%s trace=%s", provider_name, str(exc), traceback.format_exc())
    return None


def _persist_llm_config(provider: str, api_key: str, model: str, api_base: str) -> None:
    """Save LLM config to ~/.agent/llm_config.json so it survives panel restarts."""
    import json
    config_dir = Path.home() / ".agent"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "llm_config.json"
    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
        except Exception:
            pass
    data.update({"provider": provider, "model": model, "api_base": api_base})
    if api_key:
        data["api_key"] = api_key
    try:
        config_path.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        logger.debug("persist_llm_config_failed", error=str(exc))


def _load_persisted_llm_config() -> None:
    """Load persisted LLM config into env vars if env vars not already set."""
    import os

    config_path = Path.home() / ".agent" / "llm_config.json"
    if not config_path.exists():
        return
    try:
        data = json.loads(config_path.read_text())
    except Exception:
        return
    provider = data.get("provider", "")
    if provider and not os.environ.get("AGENT_LLM_PROVIDER"):
        os.environ["AGENT_LLM_PROVIDER"] = provider
    if data.get("api_key"):
        if provider in ("openai", "openai_compatible", "compatible") and not os.environ.get("OPENAI_API_KEY"):
            os.environ["OPENAI_API_KEY"] = data["api_key"]
        elif provider in ("anthropic", "claude") and not os.environ.get("ANTHROPIC_API_KEY"):
            os.environ["ANTHROPIC_API_KEY"] = data["api_key"]
    if data.get("model"):
        if provider in ("openai", "openai_compatible", "compatible") and not os.environ.get("OPENAI_MODEL"):
            os.environ["OPENAI_MODEL"] = data["model"]
        elif provider in ("anthropic", "claude") and not os.environ.get("ANTHROPIC_MODEL"):
            os.environ["ANTHROPIC_MODEL"] = data["model"]
    if data.get("api_base") and not os.environ.get("OPENAI_API_BASE"):
        os.environ["OPENAI_API_BASE"] = data["api_base"]

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote

from agent.cache.engine import CacheEngine
from agent.cache.models import CacheDecision
from agent.core.logging import get_logger
from agent.execution.events import (
    EventType,
    RunAbortedEvent,
    RunPausedEvent,
    RunCompletedEvent,
    StepFailedEvent,
    StepRetriedEvent,
    StepStartedEvent,
    StepSucceededEvent,
)
from agent.execution.tools import ToolRuntime
from agent.stepgraph.models import (
    LocatorBundle,
    Postcondition,
    PostconditionType,
    Precondition,
    PreconditionType,
    RecoveryAction,
    Step,
    StepGraph,
    TimeoutWaitUntil,
)
from agent.execution.snapshot import SnapshotEngine
from agent.policy.approval import ApprovalClassifier, ApprovalLevel, HardApprovalRequest
from agent.policy.audit import AuditLogger
from agent.policy.restrictions import RestrictionViolation, RestrictionsPolicy


class RunnerError(RuntimeError):
    pass


class PauseRequested(RunnerError):
    pass


class EventSink(Protocol):
    async def emit(self, event: Any) -> None: ...


class HardApprovalResolver(Protocol):
    def __call__(self, request: HardApprovalRequest) -> Awaitable[bool] | bool: ...


async def _maybe_await(maybe_awaitable: Awaitable[None] | None) -> None:
    if maybe_awaitable is None:
        return
    await maybe_awaitable


async def _maybe_await_bool(maybe_awaitable: Awaitable[bool] | bool) -> bool:
    if isinstance(maybe_awaitable, bool):
        return maybe_awaitable
    return await maybe_awaitable


def _get_tab_id(step: Step) -> str:
    tab_id = step.metadata.get("tabId") or step.metadata.get("tab_id")
    if not isinstance(tab_id, str) or not tab_id.strip():
        msg = (
            f"Step '{step.step_id}' missing tab id. "
            "Provide `metadata.tabId` (preferred) or `metadata.tab_id`."
        )
        raise RunnerError(msg)
    return tab_id


def _selectors_from_bundle(bundle: LocatorBundle | None) -> list[str]:
    if bundle is None:
        return []
    selectors = [bundle.primary_selector, *bundle.fallback_selectors]
    return [s for s in selectors if isinstance(s, str) and s.strip()]


def _timeout_wait_until(wait_until: TimeoutWaitUntil | None) -> str | None:
    if wait_until is None:
        return None
    return str(wait_until.value)


@dataclass(frozen=True)
class StepAttemptContext:
    attempt_index: int
    max_attempts: int


class StepGraphRunner:
    def __init__(
        self,
        runtime: ToolRuntime,
        *,
        actor: str = "runner",
        event_sink: EventSink | None = None,
        event_emitter: Callable[[Any], Awaitable[None] | None] | None = None,
        cache_engine: CacheEngine | None = None,
        snapshot_engine: SnapshotEngine | None = None,
        approval_classifier: ApprovalClassifier | None = None,
        hard_approval_resolver: HardApprovalResolver | None = None,
        restrictions_policy: RestrictionsPolicy | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self._logger = get_logger(__name__)
        self._runtime = runtime
        self._actor = actor
        self._event_sink = event_sink
        self._event_emitter = event_emitter
        self._cache_engine = cache_engine
        self._snapshot_engine = snapshot_engine
        self._approval_classifier = approval_classifier
        self._hard_approval_resolver = hard_approval_resolver
        self._restrictions_policy = restrictions_policy
        self._audit_logger = audit_logger

    async def run(
        self,
        graph: StepGraph,
        *,
        start_step_id: str | None = None,
        pause_requested: Callable[[], bool] | None = None,
    ) -> None:
        self._logger.info("run_started", run_id=graph.run_id, steps=len(graph.steps))
        try:
            started = start_step_id is None
            for step in graph.steps:
                if not started:
                    if step.step_id == start_step_id:
                        started = True
                    else:
                        continue

                if pause_requested is not None and pause_requested():
                    raise PauseRequested("Pause requested")
                await self._run_step(graph, step)
        except PauseRequested as exc:
            await self._emit(
                RunPausedEvent(
                    run_id=graph.run_id,
                    actor=self._actor,
                    type=EventType.RUN_PAUSED,
                    payload={
                        "reason": str(exc),
                        "start_step_id": start_step_id,
                        "next_step_id": step.step_id,
                    },
                )
            )
            self._logger.info("run_paused", run_id=graph.run_id)
            return
        except Exception as exc:
            await self._emit(
                RunAbortedEvent(
                    run_id=graph.run_id,
                    actor=self._actor,
                    type=EventType.RUN_ABORTED,
                    payload={"error": str(exc)},
                )
            )
            raise

        await self._emit(
            RunCompletedEvent(
                run_id=graph.run_id,
                actor=self._actor,
                type=EventType.RUN_COMPLETED,
                payload={},
            )
        )
        self._logger.info("run_completed", run_id=graph.run_id)

    async def run_one_step(self, graph: StepGraph, step: Step) -> None:
        """Execute a single step (used by interactive / debugger-style UIs)."""
        await self._run_step(graph, step)

    async def _run_step(self, graph: StepGraph, step: Step) -> None:
        tab_id = _get_tab_id(step)
        selectors = _selectors_from_bundle(step.target)
        stale_ref_detected = False

        max_retries = int(step.recovery_policy.max_retries)
        max_attempts = max_retries + 1

        for attempt_index in range(max_attempts):
            attempt = StepAttemptContext(attempt_index=attempt_index, max_attempts=max_attempts)

            await self._emit(
                StepStartedEvent(
                    run_id=graph.run_id,
                    step_id=step.step_id,
                    actor=self._actor,
                    type=EventType.STEP_STARTED,
                    payload={
                        "action": step.action,
                        "attempt": attempt.attempt_index,
                        "max_attempts": attempt.max_attempts,
                    },
                )
            )

            try:
                await self._prepare_step_context(
                    graph=graph,
                    step=step,
                    tab_id=tab_id,
                    selectors=selectors,
                    stale_ref_detected=stale_ref_detected,
                )
                await self._evaluate_preconditions(graph, step, tab_id, selectors)
                await self._execute_step_action(graph, step, tab_id, selectors, attempt=attempt)
                await self._evaluate_postconditions(graph, step, tab_id, selectors)
                stale_ref_detected = False
            except Exception as exc:
                stale_ref_detected = _is_stale_ref_error(exc)
                is_last_attempt = attempt_index >= (max_attempts - 1)
                should_retry = (
                    not is_last_attempt
                    and RecoveryAction.RETRY in step.recovery_policy.allowed_actions
                    and step.recovery_policy.max_retries > 0
                )

                if should_retry:
                    await self._emit(
                        StepRetriedEvent(
                            run_id=graph.run_id,
                            step_id=step.step_id,
                            actor=self._actor,
                            type=EventType.STEP_RETRIED,
                            payload={"error": str(exc), "attempt": attempt_index},
                        )
                    )
                    backoff_ms = int(step.recovery_policy.retry_backoff_ms)
                    if backoff_ms > 0:
                        await asyncio.sleep(backoff_ms / 1000)
                    continue

                await self._emit(
                    StepFailedEvent(
                        run_id=graph.run_id,
                        step_id=step.step_id,
                        actor=self._actor,
                        type=EventType.STEP_FAILED,
                        payload={"error": str(exc), "attempt": attempt_index},
                    )
                )
                raise

            await self._emit(
                StepSucceededEvent(
                    run_id=graph.run_id,
                    step_id=step.step_id,
                    actor=self._actor,
                    type=EventType.STEP_SUCCEEDED,
                    payload={"attempt": attempt_index},
                )
            )
            return

    async def _prepare_step_context(
        self,
        *,
        graph: StepGraph,
        step: Step,
        tab_id: str,
        selectors: list[str],
        stale_ref_detected: bool,
    ) -> None:
        if self._cache_engine is None:
            if self._snapshot_engine is not None:
                await self._snapshot_engine.capture_snapshot(tab_id)
            return

        decision_result = await self._cache_engine.decide(
            run_id=graph.run_id,
            step_id=step.step_id,
            tab_id=tab_id,
            target_selectors=selectors,
            stale_ref_detected=stale_ref_detected,
        )
        if (
            decision_result.decision in {CacheDecision.PARTIAL_REFRESH, CacheDecision.FULL_REFRESH}
            and self._snapshot_engine is not None
        ):
            snapshot = await self._snapshot_engine.capture_snapshot(tab_id)
            self._logger.info(
                "step_context_refreshed",
                run_id=graph.run_id,
                step_id=step.step_id,
                decision=decision_result.decision.value,
                reasons=decision_result.reasons,
                tab_id=tab_id,
                snapshot_element_count=len(snapshot.elements),
            )
            return

        self._logger.info(
            "step_context_reused",
            run_id=graph.run_id,
            step_id=step.step_id,
            decision=decision_result.decision.value,
            reasons=decision_result.reasons,
            tab_id=tab_id,
        )

    async def _evaluate_preconditions(
        self,
        graph: StepGraph,
        step: Step,
        tab_id: str,
        selectors: list[str],
    ) -> None:
        for pre in step.preconditions:
            await self._evaluate_precondition(graph, step, tab_id, selectors, pre)

    async def _evaluate_postconditions(
        self,
        graph: StepGraph,
        step: Step,
        tab_id: str,
        selectors: list[str],
    ) -> None:
        for post in step.postconditions:
            await self._evaluate_postcondition(graph, step, tab_id, selectors, post)

    async def _evaluate_precondition(
        self,
        graph: StepGraph,
        step: Step,
        tab_id: str,
        selectors: list[str],
        pre: Precondition,
    ) -> None:
        if pre.type in {PreconditionType.CUSTOM, PreconditionType.DIALOG_EXPECTED}:
            msg = f"Unsupported precondition type in manual runner: {pre.type}"
            raise RunnerError(msg)

        if pre.type in {PreconditionType.URL_MATCHES, PreconditionType.TITLE_MATCHES}:
            expected = pre.payload.get("expected")
            contains = bool(pre.payload.get("contains", True))
            if not isinstance(expected, str):
                raise RunnerError(f"Precondition payload missing string 'expected' for {pre.type}")

            if pre.type == PreconditionType.URL_MATCHES:
                await self._runtime.assert_url(tab_id=tab_id, expected=expected, contains=contains)
            else:
                await self._runtime.assert_title(tab_id=tab_id, expected=expected, contains=contains)
            return

        # Element-based preconditions
        target = pre.payload.get("target")
        if isinstance(target, str) and target.strip():
            target_selectors = [target]
        else:
            target_selectors = selectors

        if not target_selectors:
            raise RunnerError(f"Precondition {pre.type} requires a target selector.")

        timeout_ms = float(step.timeout_policy.timeout_ms)
        last_exc: Exception | None = None
        for selector in target_selectors:
            try:
                if pre.type == PreconditionType.ELEMENT_VISIBLE:
                    await self._runtime.assert_visible(tab_id=tab_id, target=selector, timeout_ms=timeout_ms)
                elif pre.type == PreconditionType.ELEMENT_HIDDEN:
                    await self._runtime.wait_for(
                        tab_id=tab_id,
                        target=selector,
                        state="hidden",
                        timeout_ms=timeout_ms,
                    )
                elif pre.type == PreconditionType.FRAME_SELECTED:
                    # Frame tracking is handled via explicit frame_enter/frame_exit steps.
                    return
                else:
                    raise RunnerError(f"Unsupported precondition type: {pre.type}")
                return
            except Exception as exc:
                last_exc = exc
                continue

        raise RunnerError(f"Precondition {pre.type} failed for all selectors.") from last_exc

    async def _evaluate_postcondition(
        self,
        graph: StepGraph,
        step: Step,
        tab_id: str,
        selectors: list[str],
        post: Postcondition,
    ) -> None:
        if post.type in {PostconditionType.CUSTOM, PostconditionType.EVENT_EMITTED}:
            msg = f"Unsupported postcondition type in manual runner: {post.type}"
            raise RunnerError(msg)

        if post.type in {PostconditionType.URL_MATCHES, PostconditionType.TITLE_MATCHES}:
            expected = post.payload.get("expected")
            contains = bool(post.payload.get("contains", True))
            if not isinstance(expected, str):
                raise RunnerError(f"Postcondition payload missing string 'expected' for {post.type}")
            if post.type == PostconditionType.URL_MATCHES:
                await self._runtime.assert_url(tab_id=tab_id, expected=expected, contains=contains)
            else:
                await self._runtime.assert_title(tab_id=tab_id, expected=expected, contains=contains)
            return

        target = post.payload.get("target")
        if isinstance(target, str) and target.strip():
            target_selectors = [target]
        else:
            target_selectors = selectors

        if not target_selectors:
            raise RunnerError(f"Postcondition {post.type} requires a target selector.")

        timeout_ms = float(step.timeout_policy.timeout_ms)
        last_exc: Exception | None = None
        for selector in target_selectors:
            try:
                if post.type == PostconditionType.ELEMENT_VISIBLE:
                    await self._runtime.assert_visible(tab_id=tab_id, target=selector, timeout_ms=timeout_ms)
                elif post.type == PostconditionType.ELEMENT_HIDDEN:
                    await self._runtime.wait_for(
                        tab_id=tab_id,
                        target=selector,
                        state="hidden",
                        timeout_ms=timeout_ms,
                    )
                elif post.type == PostconditionType.TEXT_MATCHES:
                    expected = post.payload.get("expected")
                    contains = bool(post.payload.get("contains", True))
                    if not isinstance(expected, str):
                        raise RunnerError("Postcondition TEXT_MATCHES requires payload.expected (string).")
                    await self._runtime.assert_text(
                        tab_id=tab_id,
                        target=selector,
                        expected=expected,
                        contains=contains,
                        timeout_ms=timeout_ms,
                    )
                elif post.type == PostconditionType.VALUE_MATCHES:
                    # Tool layer doesn't yet expose assert_value; treat as unsupported for now.
                    raise RunnerError("Postcondition VALUE_MATCHES is not implemented in tool layer yet.")
                else:
                    raise RunnerError(f"Unsupported postcondition type: {post.type}")
                return
            except Exception as exc:
                last_exc = exc
                continue

        raise RunnerError(f"Postcondition {post.type} failed for all selectors.") from last_exc

    async def _execute_step_action(
        self,
        graph: StepGraph,
        step: Step,
        tab_id: str,
        selectors: list[str],
        *,
        attempt: StepAttemptContext,
    ) -> None:
        action = step.action.strip()
        timeout_ms = float(step.timeout_policy.timeout_ms)

        # Copy step.metadata so we can pop and normalize values without mutating the model.
        metadata: dict[str, Any] = dict(step.metadata)
        metadata.pop("tabId", None)
        metadata.pop("tab_id", None)
        explicit_target = metadata.get("target")

        wait_until = _timeout_wait_until(step.timeout_policy.wait_until)
        policy_targets = list(selectors)
        if isinstance(explicit_target, str) and explicit_target.strip():
            policy_targets.insert(0, explicit_target)

        await self._enforce_action_policy(
            graph=graph,
            step=step,
            metadata=metadata,
            target_selectors=policy_targets,
            attempt=attempt,
        )

        if action == "navigate":
            url = metadata.get("url")
            if not isinstance(url, str) or not url.strip():
                raise RunnerError("navigate requires metadata.url (string).")
            if self._restrictions_policy is not None:
                try:
                    self._restrictions_policy.enforce_navigation_url(url)
                except RestrictionViolation as exc:
                    raise RunnerError(f"navigate blocked by restrictions policy: {exc}") from exc
            await self._runtime.navigate(
                tab_id=tab_id,
                url=url,
                wait_until=wait_until or "load",
                timeout_ms=timeout_ms,
            )
            return

        if action == "navigate_back":
            await self._runtime.navigate_back(
                tab_id=tab_id,
                wait_until=wait_until or "load",
                timeout_ms=timeout_ms,
            )
            return

        if action == "assert_url":
            expected = metadata.get("expected")
            contains = bool(metadata.get("contains", True))
            if not isinstance(expected, str):
                raise RunnerError("assert_url requires metadata.expected (string).")
            await self._runtime.assert_url(tab_id=tab_id, expected=expected, contains=contains)
            return

        if action == "assert_title":
            expected = metadata.get("expected")
            contains = bool(metadata.get("contains", True))
            if not isinstance(expected, str):
                raise RunnerError("assert_title requires metadata.expected (string).")
            await self._runtime.assert_title(tab_id=tab_id, expected=expected, contains=contains)
            return

        if action == "wait_timeout":
            t = metadata.get("timeoutMs") or metadata.get("timeout_ms") or step.timeout_policy.timeout_ms
            await self._runtime.wait_timeout(tab_id=tab_id, timeout_ms=int(t))
            return

        if action == "wait_for":
            target = metadata.get("target")
            state = metadata.get("state", "visible")
            if target is None and selectors:
                target = selectors[0]
            await self._runtime.wait_for(
                tab_id=tab_id,
                target=target,
                state=state,
                timeout_ms=timeout_ms,
            )
            return

        if action == "frame_enter":
            target = metadata.get("target")
            if not isinstance(target, str) and selectors:
                target = selectors[0]
            if not isinstance(target, str) or not target.strip():
                raise RunnerError("frame_enter requires metadata.target or step.target selectors.")
            await self._runtime.frame_enter(tab_id=tab_id, target=target)
            return

        if action == "frame_exit":
            await self._runtime.frame_exit(tab_id=tab_id)
            return

        # Element-targeted actions: try primary selector then fallbacks deterministically.
        candidate_targets: list[str] = []
        if isinstance(explicit_target, str) and explicit_target.strip():
            candidate_targets = [explicit_target]
        else:
            candidate_targets = selectors

        if not candidate_targets:
            raise RunnerError(f"Action '{action}' requires a target selector (step.target or metadata.target).")

        last_exc: Exception | None = None
        for target in candidate_targets:
            try:
                if action == "click":
                    button = metadata.get("button", "left")
                    await self._runtime.click(tab_id=tab_id, target=target, button=button, timeout_ms=timeout_ms)
                elif action == "fill":
                    text = _resolve_fill_text(metadata)
                    await self._runtime.fill(tab_id=tab_id, target=target, text=text, timeout_ms=timeout_ms)
                elif action == "type":
                    text = metadata.get("text")
                    delay_ms = float(metadata.get("delayMs", metadata.get("delay_ms", 0)))
                    if not isinstance(text, str):
                        raise RunnerError("type requires metadata.text (string).")
                    await self._runtime.type(
                        tab_id=tab_id,
                        target=target,
                        text=text,
                        delay_ms=delay_ms,
                        timeout_ms=timeout_ms,
                    )
                elif action == "press":
                    key = metadata.get("key")
                    if not isinstance(key, str) or not key.strip():
                        raise RunnerError("press requires metadata.key (string).")
                    await self._runtime.press(tab_id=tab_id, key=key, target=target, timeout_ms=timeout_ms)
                elif action == "assert_visible":
                    await self._runtime.assert_visible(tab_id=tab_id, target=target, timeout_ms=timeout_ms)
                elif action == "assert_text":
                    expected = metadata.get("expected")
                    contains = bool(metadata.get("contains", True))
                    if not isinstance(expected, str):
                        raise RunnerError("assert_text requires metadata.expected (string).")
                    await self._runtime.assert_text(
                        tab_id=tab_id,
                        target=target,
                        expected=expected,
                        contains=contains,
                        timeout_ms=timeout_ms,
                    )
                elif action == "dialog_handle":
                    accept = bool(metadata.get("accept", True))
                    prompt_text = metadata.get("promptText") or metadata.get("prompt_text")
                    await self._runtime.dialog_handle(
                        tab_id=tab_id,
                        accept=accept,
                        prompt_text=prompt_text if isinstance(prompt_text, str) else None,
                    )
                elif action == "upload":
                    file_paths = _coerce_upload_paths(metadata)
                    if self._restrictions_policy is not None:
                        try:
                            file_paths = self._restrictions_policy.enforce_upload_paths(file_paths)
                        except RestrictionViolation as exc:
                            raise RunnerError(f"upload blocked by restrictions policy: {exc}") from exc
                    await self._runtime.upload(
                        tab_id=tab_id,
                        target=target,
                        file_paths=file_paths,
                        timeout_ms=timeout_ms,
                    )
                else:
                    raise RunnerError(f"Unknown or unsupported step action: '{action}'")
                return
            except Exception as exc:
                last_exc = exc
                continue

        detail = str(last_exc) if last_exc else ""
        if len(detail) > 420:
            detail = detail[:417] + "…"
        suffix = f" Last error: {detail}" if detail else ""
        raise RunnerError(f"Action '{action}' failed for all selectors.{suffix}") from last_exc

    async def _enforce_action_policy(
        self,
        *,
        graph: StepGraph,
        step: Step,
        metadata: dict[str, Any],
        target_selectors: list[str],
        attempt: StepAttemptContext,
    ) -> None:
        if self._approval_classifier is None:
            return

        decision = self._approval_classifier.classify(
            step=step,
            metadata=metadata,
            target_selectors=target_selectors,
        )
        approved = True

        if decision.level is ApprovalLevel.HARD_APPROVAL:
            # APPROVAL_POINT: hard-risk actions require explicit operator approval.
            if self._hard_approval_resolver is None:
                approved = False
            else:
                request = HardApprovalRequest(
                    runId=graph.run_id,
                    stepId=step.step_id,
                    action=step.action,
                    decision=decision,
                    attemptIndex=attempt.attempt_index,
                )
                approved = await _maybe_await_bool(self._hard_approval_resolver(request))

            if not approved:
                if self._audit_logger is not None:
                    self._audit_logger.record_approval(
                        step=step,
                        decision=decision,
                        approved=False,
                        actor=self._actor,
                        attempt_index=attempt.attempt_index,
                    )
                raise RunnerError(
                    f"Hard approval denied for action '{step.action}' on step '{step.step_id}'."
                )

        if self._audit_logger is not None:
            self._audit_logger.record_approval(
                step=step,
                decision=decision,
                approved=approved,
                actor=self._actor,
                attempt_index=attempt.attempt_index,
            )

        if decision.level is ApprovalLevel.REVIEW:
            self._logger.info(
                "action_review_classified",
                run_id=graph.run_id,
                step_id=step.step_id,
                action=step.action,
                reason_codes=decision.reason_codes,
            )

    async def _emit(self, event: Any) -> None:
        self._logger.info(
            "execution_event",
            run_id=getattr(event, "run_id", None),
            step_id=getattr(event, "step_id", None),
            type=getattr(event, "type", None),
            payload=getattr(event, "payload", None),
        )
        if self._event_sink is not None:
            await self._event_sink.emit(event)
        if self._event_emitter is not None:
            await _maybe_await(self._event_emitter(event))
        if self._audit_logger is not None and getattr(event, "type", None) == EventType.STEP_RETRIED:
            self._audit_logger.record_retry(event)


def _coerce_upload_paths(metadata: dict[str, Any]) -> str | list[str]:
    paths = metadata.get("filePaths") or metadata.get("file_paths") or metadata.get("files")

    def _norm(p: str) -> str:
        return str(Path(unquote(p.strip())).expanduser())

    if isinstance(paths, str) and paths.strip():
        return _norm(paths)
    if isinstance(paths, list) and paths and all(isinstance(item, str) and item.strip() for item in paths):
        return [_norm(item) for item in paths]
    raise RunnerError("upload requires metadata.filePaths (string or list of strings).")


def _resolve_fill_text(metadata: dict[str, Any]) -> str:
    text = metadata.get("text")
    if isinstance(text, str):
        return text

    value_ref = metadata.get("valueRef") or metadata.get("value_ref")
    if not isinstance(value_ref, str) or not value_ref.strip():
        raise RunnerError("fill requires metadata.text (string) or metadata.valueRef (string).")

    normalized_ref = value_ref.strip()
    if normalized_ref.startswith("env:"):
        env_key = normalized_ref.split(":", 1)[1].strip()
        if not env_key:
            raise RunnerError("fill metadata.valueRef env reference is missing a key (expected env:KEY).")
        env_value = os.getenv(env_key)
        if not env_value:
            raise RunnerError(
                f"fill metadata.valueRef requires environment variable '{env_key}' to be set."
            )
        return env_value

    if normalized_ref.lower() == "redacted":
        fallback = os.getenv("FLOWHUB_PASSWORD")
        if fallback:
            return fallback
        raise RunnerError(
            "fill metadata.valueRef='redacted' cannot be replayed without a concrete value. "
            "Set FLOWHUB_PASSWORD or record with metadata.valueRef='env:<KEY>'."
        )

    raise RunnerError(
        "Unsupported fill metadata.valueRef format. Expected 'env:<KEY>' or 'redacted'."
    )


def _is_stale_ref_error(exc: Exception) -> bool:
    message = str(exc).lower()
    markers = (
        "failed for all selectors",
        "detached frame",
        "ref '",
        "requires a target selector",
        "unknown tab id",
    )
    return any(marker in message for marker in markers)


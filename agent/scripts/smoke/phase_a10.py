from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(SCRIPT_DIR))

from agent.core.ids import generate_run_id  # noqa: E402
from agent.core.mode import ModeController, RuntimeBinding, RuntimeMode  # noqa: E402
from agent.execution.events import EventType, InterventionRecordedEvent  # noqa: E402
from agent.execution.runner import RunnerError, StepGraphRunner  # noqa: E402
from agent.execution.tools import InteractionResult, ToolCallEvent  # noqa: E402
from agent.llm.orchestrator import PHASE3_TOOL_NAMES  # noqa: E402
from agent.policy.approval import (  # noqa: E402
    ApprovalClassifier,
    ApprovalLevel,
    HardApprovalRequest,
)
from agent.policy.audit import AuditKind, AuditLogger  # noqa: E402
from agent.policy.restrictions import RestrictionViolation, RestrictionsPolicy  # noqa: E402
from agent.stepgraph.models import (  # noqa: E402
    LocatorBundle,
    RecoveryAction,
    RecoveryPolicy,
    Step,
    StepGraph,
    StepMode,
    TimeoutPolicy,
)
from _runner import SmokeRunner  # noqa: E402


def _expected_approval_level_for_phase3_action(action: str) -> ApprovalLevel:
    """Expected `ApprovalLevel` per docs/07-security-and-guardrails.md (neutral metadata)."""
    a = action.strip().lower()
    if a == "upload":
        return ApprovalLevel.HARD_APPROVAL
    if a in {
        "click",
        "fill",
        "type",
        "press",
        "check",
        "uncheck",
        "select",
        "drag",
        "hover",
        "focus",
    }:
        return ApprovalLevel.REVIEW
    return ApprovalLevel.AUTO_ALLOW


def _neutral_step(action: str) -> Step:
    return Step(
        mode=StepMode.ACTION,
        action=action,
        metadata={"tabId": "tab_policy", "semanticTarget": "fixture neutral"},
        target=LocatorBundle(
            primarySelector="button#fixture",
            fallbackSelectors=[],
            confidenceScore=0.9,
        ),
    )


class _EventCollector:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


class _StubClickRuntime:
    """Minimal async runtime for click-only policy harness (no browser)."""

    def __init__(self, *, audit_logger: AuditLogger | None = None) -> None:
        self._audit = audit_logger
        self._click_attempts = 0

    async def click(
        self,
        *,
        tab_id: str,
        target: str,
        button: str = "left",
        timeout_ms: float = 30_000,
    ) -> InteractionResult:
        self._click_attempts += 1
        if self._click_attempts == 1 and getattr(self, "_fail_once", False):
            raise RuntimeError("stub transient selector failure")
        ev = ToolCallEvent(
            tool="click",
            tabId=tab_id,
            status="succeeded",
            actor="tool_layer",
            payload={"target": target, "button": button},
        )
        if self._audit is not None:
            self._audit.record_tool_call(ev)
        return InteractionResult(
            tool="click",
            tabId=tab_id,
            framePath=[],
            target=target,
            details={"button": button},
        )


async def main() -> int:
    smoke = SmokeRunner(phase="A10", default_task="A10.1")
    classifier = ApprovalClassifier()

    with smoke.case("a10_1_classifier_matches_docs_07", task="A10.1", feature="approval_classifier"):
        for action in PHASE3_TOOL_NAMES:
            step = _neutral_step(action)
            got = classifier.classify(step=step, metadata=step.metadata, target_selectors=["button#fixture"])
            exp = _expected_approval_level_for_phase3_action(action)
            smoke.check(
                got.level is exp,
                f"action={action!r}: expected {exp.value}, got {got.level.value} ({got.reason_codes})",
            )

    with smoke.case("a10_2_hard_approval_denied_aborts", task="A10.2", feature="hard_approval"):
        sink = _EventCollector()
        run_id = generate_run_id()
        with tempfile.TemporaryDirectory(prefix="a10-deny-") as tmp:
            runs_root = Path(tmp).resolve()
            audit = AuditLogger.for_run(run_id=run_id, runs_root=runs_root)
            hard_step = Step(
                mode=StepMode.ACTION,
                action="click",
                metadata={
                    "tabId": "tab_a10",
                    "semanticTarget": "place order checkout",
                },
                target=LocatorBundle(
                    primarySelector="button#pay",
                    fallbackSelectors=[],
                    confidenceScore=0.85,
                ),
            )
            graph = StepGraph(run_id=run_id, steps=[hard_step])
            dec = classifier.classify(step=hard_step)
            smoke.check(dec.level is ApprovalLevel.HARD_APPROVAL, dec.level)

            runner = StepGraphRunner(
                _StubClickRuntime(audit_logger=audit),
                actor="smoke_a10",
                event_sink=sink,
                approval_classifier=classifier,
                hard_approval_resolver=lambda _r: False,
                audit_logger=audit,
            )
            try:
                await runner.run(graph)
            except RunnerError:
                pass
            else:
                raise AssertionError("Expected RunnerError when hard approval denied")

            aborted = [e for e in sink.events if getattr(e, "type", None) == EventType.RUN_ABORTED]
            smoke.check(len(aborted) == 1, f"Expected run_aborted, events={[getattr(e,'type',e) for e in sink.events]}")
            err = (aborted[0].payload or {}).get("error", "")
            smoke.check("Hard approval denied" in str(err), err)

    with smoke.case("a10_2_hard_approval_allowed_continues", task="A10.2", feature="hard_approval"):
        sink = _EventCollector()
        run_id = generate_run_id()
        with tempfile.TemporaryDirectory(prefix="a10-allow-") as tmp:
            runs_root = Path(tmp).resolve()
            audit = AuditLogger.for_run(run_id=run_id, runs_root=runs_root)
            hard_step = Step(
                mode=StepMode.ACTION,
                action="click",
                metadata={
                    "tabId": "tab_a10",
                    "semanticTarget": "confirm purchase submit",
                },
                target=LocatorBundle(
                    primarySelector="button#submit",
                    fallbackSelectors=[],
                    confidenceScore=0.85,
                ),
            )
            graph = StepGraph(run_id=run_id, steps=[hard_step])
            approvals: list[bool] = []

            def _resolver(req: HardApprovalRequest) -> bool:
                approvals.append(True)
                smoke.check(req.decision.requires_hard_approval, "resolver should only run for hard approval")
                return True

            runner = StepGraphRunner(
                _StubClickRuntime(audit_logger=audit),
                actor="smoke_a10",
                event_sink=sink,
                approval_classifier=classifier,
                hard_approval_resolver=_resolver,
                audit_logger=audit,
            )
            await runner.run(graph)
            smoke.check(approvals == [True], f"Expected resolver invoked once, got {approvals}")
            completed = [e for e in sink.events if getattr(e, "type", None) == EventType.RUN_COMPLETED]
            smoke.check(len(completed) == 1, "Expected run_completed after approval")

    with smoke.case("a10_3_restrictions_reason_codes", task="A10.3", feature="restrictions"):
        with tempfile.TemporaryDirectory(prefix="a10-restrict-") as tmp:
            allowed_root = Path(tmp) / "uploads"
            allowed_root.mkdir(parents=True, exist_ok=True)
            allowed_file = allowed_root / "ok.txt"
            allowed_file.write_text("x", encoding="utf-8")
            outside_file = Path(tmp) / "secret.txt"
            outside_file.write_text("y", encoding="utf-8")

            policy = RestrictionsPolicy(
                domain_allowlist=["good.example"],
                domain_denylist=["evil.example"],
                upload_root_allowlist=[str(allowed_root)],
                allow_file_urls=False,
            )

            try:
                policy.enforce_navigation_url("https://evil.example/x")
            except RestrictionViolation as exc:
                smoke.check(exc.decision.reason_code == "domain_denied", exc.decision.reason_code)
            else:
                raise AssertionError("denylist domain must raise")

            try:
                policy.enforce_navigation_url("https://other.test/")
            except RestrictionViolation as exc:
                smoke.check(exc.decision.reason_code == "domain_not_allowlisted", exc.decision.reason_code)
            else:
                raise AssertionError("non-allowlisted domain must raise")

            try:
                policy.enforce_navigation_url("file:///etc/passwd")
            except RestrictionViolation as exc:
                smoke.check(exc.decision.reason_code == "file_scheme_blocked", exc.decision.reason_code)
            else:
                raise AssertionError("file:// must raise")

            policy.enforce_upload_paths(str(allowed_file))

            try:
                policy.enforce_upload_paths(str(outside_file))
            except RestrictionViolation as exc:
                smoke.check(
                    exc.decision.reason_code == "upload_path_outside_allowlist",
                    exc.decision.reason_code,
                )
            else:
                raise AssertionError("upload outside root must raise")

    with smoke.case("a10_4_audit_completeness", task="A10.4", feature="audit"):
        run_id = generate_run_id()
        sink = _EventCollector()
        with tempfile.TemporaryDirectory(prefix="a10-audit-") as tmp:
            runs_root = Path(tmp).resolve()
            audit = AuditLogger.for_run(run_id=run_id, runs_root=runs_root)

            await ModeController(initial_mode=RuntimeMode.MANUAL).switch_mode(
                target_mode=RuntimeMode.HYBRID,
                reason="a10_audit_mode_switch",
                actor="smoke_a10",
                binding=RuntimeBinding(
                    run_id=run_id,
                    current_step_id="step_audit_1",
                    browser_session_id="bs_a10",
                    tab_id="tab_a10",
                ),
                sqlite_path=Path(tmp) / "events.sqlite",
                runs_root=runs_root,
            )

            flaky = _StubClickRuntime(audit_logger=audit)
            flaky._fail_once = True  # noqa: SLF001
            retry_step = Step(
                mode=StepMode.ACTION,
                action="click",
                metadata={"tabId": "tab_a10", "semanticTarget": "neutral widget"},
                target=LocatorBundle(
                    primarySelector="#w",
                    fallbackSelectors=[],
                    confidenceScore=0.8,
                ),
                recovery_policy=RecoveryPolicy(
                    maxRetries=1,
                    retryBackoffMs=0,
                    allowedActions=[RecoveryAction.RETRY],
                ),
                timeout_policy=TimeoutPolicy(timeoutMs=1000),
            )
            graph = StepGraph(run_id=run_id, steps=[retry_step])
            runner = StepGraphRunner(
                flaky,
                actor="smoke_a10",
                event_sink=sink,
                approval_classifier=classifier,
                hard_approval_resolver=lambda _r: True,
                audit_logger=audit,
            )
            await runner.run(graph)

            audit.record_intervention(
                InterventionRecordedEvent(
                    run_id=run_id,
                    step_id=retry_step.step_id,
                    actor="operator",
                    type=EventType.INTERVENTION_RECORDED,
                    payload={"fixType": "locator_refresh", "note": "post-run fixture"},
                )
            )

            lines = [ln for ln in audit.output_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            kinds: list[str] = []
            for ln in lines:
                row = json.loads(ln)
                kinds.append(row.get("kind", ""))
                smoke.check(str(row.get("actor", "")).strip() != "", f"missing actor: {row}")
                payload = row.get("payload")
                smoke.check(isinstance(payload, dict), f"payload must be dict: {row}")

            for required in (
                AuditKind.APPROVAL.value,
                AuditKind.MODE_SWITCH.value,
                AuditKind.TOOL_CALL.value,
                AuditKind.RETRY.value,
                AuditKind.INTERVENTION.value,
            ):
                smoke.check(required in kinds, f"audit missing kind {required!r}, got {sorted(set(kinds))}")

    return smoke.finalize()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

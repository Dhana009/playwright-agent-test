from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agent.core.ids import generate_run_id  # noqa: E402
from agent.execution.events import EventType, InterventionRecordedEvent, StepRetriedEvent  # noqa: E402
from agent.policy.approval import ApprovalClassifier, ApprovalLevel  # noqa: E402
from agent.policy.audit import AuditLogger  # noqa: E402
from agent.policy.restrictions import RestrictionViolation, RestrictionsPolicy  # noqa: E402
from agent.stepgraph.models import Step, StepMode  # noqa: E402


def _print_result(name: str, ok: bool, detail: str) -> None:
    status = "PASS" if ok else "FAIL"
    print(f"{status:<5} {name:<24} {detail}")


async def main() -> None:
    run_id = generate_run_id()
    classifier = ApprovalClassifier()
    audit_logger = AuditLogger.for_run(run_id=run_id)

    safe_step = Step(
        mode=StepMode.ASSERTION,
        action="assert_text",
        metadata={"expected": "Dashboard"},
    )
    review_step = Step(
        mode=StepMode.ACTION,
        action="fill",
        metadata={"text": "alice@example.com"},
    )
    hard_step = Step(
        mode=StepMode.ACTION,
        action="click",
        metadata={"semanticIntent": "Delete user permanently"},
    )

    safe_decision = classifier.classify(step=safe_step)
    review_decision = classifier.classify(step=review_step)
    hard_decision = classifier.classify(step=hard_step)

    _print_result(
        "approval_auto_allow",
        safe_decision.level is ApprovalLevel.AUTO_ALLOW,
        f"level={safe_decision.level.value}",
    )
    _print_result(
        "approval_review",
        review_decision.level is ApprovalLevel.REVIEW,
        f"level={review_decision.level.value}",
    )
    _print_result(
        "approval_hard",
        hard_decision.level is ApprovalLevel.HARD_APPROVAL,
        f"level={hard_decision.level.value} reasons={hard_decision.reason_codes}",
    )

    audit_logger.record_approval(
        step=safe_step,
        decision=safe_decision,
        approved=True,
        actor="smoke",
        attempt_index=0,
    )
    audit_logger.record_approval(
        step=hard_step,
        decision=hard_decision,
        approved=False,
        actor="smoke",
        attempt_index=0,
    )
    audit_logger.record_mode_switch(
        actor="smoke",
        previous_mode="manual",
        new_mode="hybrid",
        reason="phase_10_smoke_mode_switch",
        step_id=hard_step.step_id,
    )
    audit_logger.record_tool_call(
        {
            "tool": "click",
            "tabId": "tab_smoke",
            "status": "started",
            "actor": "tool_layer",
            "payload": {"target": "button#submit"},
        }
    )
    audit_logger.record_retry(
        StepRetriedEvent(
            run_id=run_id,
            step_id=hard_step.step_id,
            actor="runner",
            type=EventType.STEP_RETRIED,
            payload={"error": "timeout", "attempt": 1},
        )
    )
    audit_logger.record_intervention(
        InterventionRecordedEvent(
            run_id=run_id,
            step_id=hard_step.step_id,
            actor="operator",
            type=EventType.INTERVENTION_RECORDED,
            payload={"fix_type": "manual-fix"},
        )
    )

    with TemporaryDirectory(prefix="phase10-smoke-") as temp_root:
        allowed_root = Path(temp_root) / "uploads"
        allowed_root.mkdir(parents=True, exist_ok=True)
        allowed_file = allowed_root / "avatar.txt"
        allowed_file.write_text("smoke", encoding="utf-8")
        outside_file = Path(temp_root) / "outside.txt"
        outside_file.write_text("blocked", encoding="utf-8")

        restrictions = RestrictionsPolicy(
            domain_allowlist=["example.com"],
            domain_denylist=["blocked.example.com"],
            upload_root_allowlist=[str(allowed_root)],
            allow_file_urls=False,
        )

        nav_ok = restrictions.enforce_navigation_url("https://example.com/settings")
        _print_result("restriction_domain", nav_ok.allowed, f"reason={nav_ok.reason_code}")

        file_blocked = False
        try:
            restrictions.enforce_navigation_url("file:///tmp/secrets.txt")
        except RestrictionViolation:
            file_blocked = True
        _print_result("restriction_file_scheme", file_blocked, "file:// blocked")

        upload_ok = restrictions.enforce_upload_paths(str(allowed_file))
        _print_result(
            "restriction_upload_allow",
            bool(upload_ok),
            f"path={upload_ok[0]}",
        )

        upload_blocked = False
        try:
            restrictions.enforce_upload_paths(str(outside_file))
        except RestrictionViolation:
            upload_blocked = True
        _print_result("restriction_upload_block", upload_blocked, "outside allowlist blocked")

    audit_lines = audit_logger.output_path.read_text(encoding="utf-8").splitlines()
    _print_result(
        "audit_entries_written",
        len(audit_lines) >= 6,
        f"count={len(audit_lines)} path={audit_logger.output_path}",
    )


if __name__ == "__main__":
    asyncio.run(main())

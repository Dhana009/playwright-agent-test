from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(SCRIPT_DIR))

from agent.core.ids import generate_run_id  # noqa: E402
from agent.memory.compiled import CompiledMemoryStore  # noqa: E402
from agent.memory.contradictions import (  # noqa: E402
    ContradictionPolicyConfig,
    ContradictionPolicyOutcome,
    ContradictionResolver,
    ContradictionType,
    apply_policy,
    classify_contradiction,
)
from agent.memory.models import (  # noqa: E402
    MemoryEntryType,
    RawEvidence,
    RawEvidenceType,
    RepairLifecycleState,
)
from agent.memory.raw import RawEvidenceWriter  # noqa: E402
from agent.memory.repairs import (  # noqa: E402
    LearnedRepairStore,
    RepairLifecyclePolicy,
)
from _runner import SmokeRunner  # noqa: E402


async def main() -> int:
    smoke = SmokeRunner(phase="A8", default_task="A8.1")
    contradiction_cfg = ContradictionPolicyConfig(
        confidence_epsilon=0.0,
        require_manual_when_unvalidated=True,
    )

    with smoke.case("a8_1_raw_evidence_append_only", task="A8.1", feature="raw_evidence"):
        with tempfile.TemporaryDirectory(prefix="a8-smoke-") as tmp:
            root = Path(tmp).resolve()
            db = root / "m.sqlite"
            runs_root = root / "runs"
            run_id = generate_run_id()
            writer = RawEvidenceWriter.for_run(
                run_id=run_id,
                sqlite_path=db,
                runs_root=runs_root,
            )
            await writer.append(
                actor="smoke_a8",
                evidence_type=RawEvidenceType.SNAPSHOT,
                artifact_ref="snap://1",
                step_id="s1",
            )
            await writer.append(
                actor="smoke_a8",
                evidence_type=RawEvidenceType.SCREENSHOT,
                artifact_ref="png://2",
                step_id="s2",
            )
            jsonl = writer.jsonl_path
            lines = [ln for ln in jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
            smoke.check(len(lines) == 2, f"Expected 2 jsonl lines, got {len(lines)}")

            listed = await writer.list(limit=10)
            smoke.check(len(listed) == 2, "Expected 2 sqlite rows for run")

            dup = RawEvidence(
                evidenceId=listed[0].evidence_id,
                runId=run_id,
                stepId="s9",
                actor="smoke_a8",
                evidenceType=RawEvidenceType.OTHER,
                artifactRef="dup",
            )
            try:
                await writer.append_record(dup)
            except ValueError as exc:
                smoke.check(
                    "append-only" in str(exc).lower() or "already exists" in str(exc).lower(),
                    f"Expected append-only rejection, got {exc!r}",
                )
            else:
                raise AssertionError("Duplicate evidence_id must be rejected")

            lines_after = [ln for ln in jsonl.read_text(encoding="utf-8").splitlines() if ln.strip()]
            smoke.check(
                len(lines_after) == 2,
                "jsonl must not grow when sqlite insert is rejected",
            )

    with smoke.case("a8_2_compiled_memory_version_provenance", task="A8.2", feature="compiled_memory"):
        with tempfile.TemporaryDirectory(prefix="a8-smoke-") as tmp:
            root = Path(tmp).resolve()
            db = root / "m.sqlite"
            runs_root = root / "runs"
            run_id = generate_run_id()
            writer = RawEvidenceWriter.for_run(
                run_id=run_id,
                sqlite_path=db,
                runs_root=runs_root,
            )
            e1 = await writer.append(
                actor="smoke_a8",
                evidence_type=RawEvidenceType.OTHER,
                artifact_ref="ev/1",
            )
            e2 = await writer.append(
                actor="smoke_a8",
                evidence_type=RawEvidenceType.OTHER,
                artifact_ref="ev/2",
            )

            store = CompiledMemoryStore.create(sqlite_path=db)
            first = await store.upsert(
                entry_type=MemoryEntryType.LOCATOR_BUNDLE,
                key="fixture:submit",
                value={"primarySelector": "#v1"},
                raw_evidence_ids=[e1.evidence_id],
                version=1,
            )
            smoke.check(first.version == 1, f"Expected version 1, got {first.version}")

            second = await store.upsert(
                entry_type=MemoryEntryType.LOCATOR_BUNDLE,
                key="fixture:submit",
                value={"primarySelector": "#v2"},
                raw_evidence_ids=[e1.evidence_id, e2.evidence_id],
                version=1,
                entry_id=first.entry_id,
            )
            smoke.check(second.version == 2, f"Expected bumped version 2, got {second.version}")
            smoke.check(
                set(second.raw_evidence_ids) == {e1.evidence_id, e2.evidence_id},
                f"Provenance must include both evidence ids, got {second.raw_evidence_ids}",
            )

            loaded = await store.get(first.entry_id)
            smoke.check(loaded is not None and loaded.version == 2, "Reload must show version 2")

            try:
                await store.upsert(
                    entry_type=MemoryEntryType.OTHER,
                    key="k",
                    value={},
                    raw_evidence_ids=["missing_evidence_id"],
                )
            except ValueError as exc:
                smoke.check("provenance" in str(exc).lower(), f"Expected provenance error, got {exc!r}")
            else:
                raise AssertionError("Unknown raw evidence id must fail provenance check")

    with smoke.case("a8_3_learned_repair_lifecycle", task="A8.3", feature="learned_repairs"):
        with tempfile.TemporaryDirectory(prefix="a8-smoke-") as tmp:
            db = Path(tmp).resolve() / "m.sqlite"
            policy = RepairLifecyclePolicy(
                promote_after_successes=3,
                degrade_after_failures=2,
                retire_after_failures=4,
            )
            store = LearnedRepairStore.create(sqlite_path=db, lifecycle_policy=policy)
            run_id = generate_run_id()
            repair = await store.record_manual_fix_candidate(
                domain="fixture.test",
                normalized_route_template="/dashboard",
                frame_context=[],
                target_semantic_key="submit",
                source_run_id=run_id,
                source_step_id="step_1",
                actor="smoke_a8",
                confidence_score=0.88,
            )
            smoke.check(repair.state == RepairLifecycleState.CANDIDATE, "New repair must be candidate")

            for _ in range(3):
                repair = await store.record_validation(repair_id=repair.repair_id, succeeded=True)
            smoke.check(
                repair.state == RepairLifecycleState.TRUSTED,
                f"Expected trusted after 3 successes, got {repair.state!r}",
            )

            repair = await store.record_validation(repair_id=repair.repair_id, succeeded=False)
            smoke.check(
                repair.state == RepairLifecycleState.TRUSTED,
                "Single failure should not degrade yet",
            )
            repair = await store.record_validation(repair_id=repair.repair_id, succeeded=False)
            smoke.check(
                repair.state == RepairLifecycleState.DEGRADED,
                f"Expected degraded after 2 failures, got {repair.state!r}",
            )

    with smoke.case("a8_4_contradiction_classify_and_policy", task="A8.4", feature="contradictions"):
        cfg = contradiction_cfg

        smoke.check(
            classify_contradiction(route_changed=False, frame_changed=False, stale_ref_detected=True)
            == ContradictionType.STALE_LOCATOR,
            "stale_ref -> stale_locator",
        )
        smoke.check(
            classify_contradiction(route_changed=True, frame_changed=False, stale_ref_detected=False)
            == ContradictionType.STRUCTURE_DRIFT,
            "route_changed -> structure_drift",
        )
        smoke.check(
            classify_contradiction(route_changed=False, frame_changed=True, stale_ref_detected=False)
            == ContradictionType.STRUCTURE_DRIFT,
            "frame_changed -> structure_drift",
        )
        smoke.check(
            classify_contradiction(route_changed=False, frame_changed=False, stale_ref_detected=False)
            == ContradictionType.CONTENT_DRIFT,
            "default -> content_drift",
        )

        d, _r = apply_policy(
            contradiction_type=ContradictionType.CONTENT_DRIFT,
            old_confidence=0.9,
            new_confidence=0.95,
            newer_evidence_validated=True,
            manual_review_required=False,
            policy_config=cfg,
        )
        smoke.check(d == ContradictionPolicyOutcome.ACCEPT_NEW, "Higher new confidence -> accept_new")

        d, _r = apply_policy(
            contradiction_type=ContradictionType.CONTENT_DRIFT,
            old_confidence=0.9,
            new_confidence=0.5,
            newer_evidence_validated=True,
            manual_review_required=False,
            policy_config=cfg,
        )
        smoke.check(
            d == ContradictionPolicyOutcome.DUAL_TRACK_WITH_FALLBACK,
            "Lower new confidence -> dual_track_with_fallback",
        )

        d, _r = apply_policy(
            contradiction_type=ContradictionType.CONTENT_DRIFT,
            old_confidence=None,
            new_confidence=None,
            newer_evidence_validated=True,
            manual_review_required=False,
            policy_config=cfg,
        )
        smoke.check(d == ContradictionPolicyOutcome.KEEP_OLD, "No confidence data -> keep_old")

        d, _r = apply_policy(
            contradiction_type=ContradictionType.CONTENT_DRIFT,
            old_confidence=0.8,
            new_confidence=0.9,
            newer_evidence_validated=False,
            manual_review_required=False,
            policy_config=cfg,
        )
        smoke.check(
            d == ContradictionPolicyOutcome.REQUIRE_MANUAL_REVIEW,
            "Unvalidated evidence -> require_manual_review",
        )

        d, _r = apply_policy(
            contradiction_type=ContradictionType.CONTENT_DRIFT,
            old_confidence=0.8,
            new_confidence=0.9,
            newer_evidence_validated=True,
            manual_review_required=True,
            policy_config=cfg,
        )
        smoke.check(
            d == ContradictionPolicyOutcome.REQUIRE_MANUAL_REVIEW,
            "manual_review_required -> require_manual_review",
        )

        d, _r = apply_policy(
            contradiction_type=ContradictionType.STALE_LOCATOR,
            old_confidence=None,
            new_confidence=None,
            newer_evidence_validated=True,
            manual_review_required=False,
            policy_config=cfg,
        )
        smoke.check(
            d == ContradictionPolicyOutcome.ACCEPT_NEW,
            "stale_locator favors accept_new without confidence",
        )

    with smoke.case("a8_4_contradiction_resolver_persists", task="A8.4", feature="contradictions"):
        with tempfile.TemporaryDirectory(prefix="a8-smoke-") as tmp:
            runs_root = Path(tmp).resolve() / "runs"
            resolver = ContradictionResolver.create(
                runs_root=runs_root,
                policy_config=contradiction_cfg,
            )
            resolution = await resolver.resolve(
                run_id=generate_run_id(),
                step_id="a8_step",
                old_value={"primarySelector": "#old"},
                new_value={"primarySelector": "#new"},
                old_confidence=0.7,
                new_confidence=0.95,
                route_changed=False,
                frame_changed=False,
                stale_ref_detected=False,
                newer_evidence_validated=True,
            )
            smoke.check(
                resolution.record.contradiction_type == ContradictionType.CONTENT_DRIFT,
                "Resolver record type must match classifier",
            )
            smoke.check(
                resolution.record.decision == ContradictionPolicyOutcome.ACCEPT_NEW,
                "Resolver decision must match policy",
            )
            path = resolver.records_path
            smoke.check(path.exists(), "contradictions.jsonl must exist")
            rows = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
            smoke.check(len(rows) >= 1, "At least one persisted contradiction record")
            payload = json.loads(rows[-1])
            smoke.check(
                payload.get("contradictionType") == "content_drift",
                f"Last line must be content_drift, got {payload.get('contradictionType')!r}",
            )

    return smoke.finalize()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

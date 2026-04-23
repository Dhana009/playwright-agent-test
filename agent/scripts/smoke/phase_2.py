from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(SCRIPT_DIR))

from agent.cache.models import CacheDecision, CacheRecord  # noqa: E402
from agent.core.ids import (  # noqa: E402
    generate_event_id,
    generate_memory_entry_id,
    generate_repair_id,
    generate_run_id,
    generate_step_id,
)
from agent.execution.checkpoint import Checkpoint  # noqa: E402
from agent.execution.events import (  # noqa: E402
    EventType,
    InterventionRecordedEvent,
    ModeSwitchedEvent,
    RunAbortedEvent,
    RunCompletedEvent,
    RunPausedEvent,
    RunResumedEvent,
    StepFailedEvent,
    StepRetriedEvent,
    StepStartedEvent,
    StepSucceededEvent,
)
from agent.memory.models import (  # noqa: E402
    CompiledMemoryEntry,
    LearnedRepair,
    MemoryEntryType,
    SchemaPolicyVersion,
)
from agent.stepgraph.models import StepGraph  # noqa: E402
from _runner import SmokeRunner  # noqa: E402

_PINNED_TS = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)


def _sorted_json_blob(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def main() -> int:
    runner = SmokeRunner(phase="A2", default_task="A2.1")
    graphs_dir = PROJECT_ROOT / "scripts" / "fixtures" / "graphs"

    with runner.case(
        "a2_1_fixture_step_graphs_round_trip",
        task="A2.1",
        feature="stepgraph",
    ):
        graph_files = sorted(graphs_dir.glob("*.json"))
        runner.check(graph_files, f"Expected fixture graphs under {graphs_dir}")
        runner.check(
            len(graph_files) >= 5,
            f"Expected at least 5 committed fixture graphs, got {len(graph_files)}",
        )
        for path in graph_files:
            raw = json.loads(path.read_text(encoding="utf-8"))
            graph = StepGraph.model_validate(raw)
            dumped = graph.model_dump(mode="json", by_alias=True)
            round_tripped = StepGraph.model_validate(dumped)
            runner.check(
                round_tripped.model_dump(mode="json", by_alias=True) == dumped,
                f"Expected StepGraph round-trip equality for {path.name}",
            )

    with runner.case("a2_2_event_types_json_deterministic", task="A2.2", feature="events"):
        run_id = generate_run_id()
        step_id = generate_step_id()
        event_id = generate_event_id()
        event_specs = [
            (StepStartedEvent, EventType.STEP_STARTED, {"step_id": step_id}),
            (StepSucceededEvent, EventType.STEP_SUCCEEDED, {"step_id": step_id}),
            (StepFailedEvent, EventType.STEP_FAILED, {"step_id": step_id}),
            (StepRetriedEvent, EventType.STEP_RETRIED, {"step_id": step_id}),
            (RunPausedEvent, EventType.RUN_PAUSED, {}),
            (RunResumedEvent, EventType.RUN_RESUMED, {}),
            (InterventionRecordedEvent, EventType.INTERVENTION_RECORDED, {"step_id": step_id}),
            (ModeSwitchedEvent, EventType.MODE_SWITCHED, {}),
            (RunCompletedEvent, EventType.RUN_COMPLETED, {}),
            (RunAbortedEvent, EventType.RUN_ABORTED, {}),
        ]

        observed_types: set[str] = set()
        for event_cls, expected_type, extra_fields in event_specs:
            common = {
                "event_id": event_id,
                "ts": _PINNED_TS,
                "run_id": run_id,
                "actor": "smoke_a2",
                "payload": {"case": "a2_2"},
                **extra_fields,
            }
            first = event_cls.model_validate(common)
            second = event_cls.model_validate(common)
            dump_a = first.model_dump(mode="json")
            dump_b = second.model_dump(mode="json")
            runner.check(
                dump_a == dump_b,
                f"Expected identical model_dump for two {event_cls.__name__} instances",
            )
            json_a = _sorted_json_blob(dump_a)
            json_b = _sorted_json_blob(dump_b)
            runner.check(json_a == json_b, f"Expected sorted JSON equality for {event_cls.__name__}")
            runner.check(
                dump_a.get("type") == expected_type.value,
                f"Expected event type {expected_type.value}",
            )
            observed_types.add(str(dump_a["type"]))

        expected_types = {event_type.value for event_type in EventType}
        runner.check(observed_types == expected_types, "Expected coverage of all 10 event types")

    with runner.case(
        "a2_3_data_contracts_round_trip_and_cache_decision",
        task="A2.3",
        feature="checkpoint_cache_memory",
    ):
        checkpoint_payload = {
            "currentStepId": generate_step_id(),
            "eventOffset": 42,
            "browserSessionId": "browser_session_test",
            "tabId": "tab_1",
            "framePath": ["main", "iframe#content"],
            "storageStateRef": "runs/example/storage_state.json",
            "pausedRecoveryState": {"attempt": 1, "reason": "manual_pause"},
        }
        checkpoint = Checkpoint.model_validate(checkpoint_payload)
        checkpoint_round_trip = Checkpoint.model_validate(
            checkpoint.model_dump(mode="json", by_alias=True)
        )
        runner.check(
            checkpoint_round_trip.model_dump(mode="json", by_alias=True)
            == checkpoint.model_dump(mode="json", by_alias=True),
            "Expected Checkpoint to round-trip",
        )

        cache_record = CacheRecord.model_validate(
            {
                "runId": generate_run_id(),
                "stepId": generate_step_id(),
                "fingerprint": {
                    "routeTemplate": "/dashboard",
                    "domHash": "dom_hash_123",
                    "frameHash": "frame_hash_123",
                    "modalState": "none",
                },
                "decision": "partial_refresh",
                "decisionReasons": ["target_scope_mutated"],
            }
        )
        cache_round_trip = CacheRecord.model_validate(
            cache_record.model_dump(mode="json", by_alias=True)
        )
        runner.check(
            cache_round_trip.model_dump(mode="json", by_alias=True)
            == cache_record.model_dump(mode="json", by_alias=True),
            "Expected CacheRecord to round-trip",
        )

        pinned_memory_ts = datetime(2026, 1, 15, 8, 30, 0, tzinfo=UTC)
        compiled_entry = CompiledMemoryEntry.model_validate(
            {
                "entryId": generate_memory_entry_id(),
                "entryType": MemoryEntryType.LEARNED_REPAIR.value,
                "key": "testing-box|/dashboard|main|target:submit",
                "value": {"selector": '[data-testid="submit"]'},
                "version": 2,
                "rawEvidenceIds": ["evidence_abc", "evidence_def"],
                "confidenceScore": 0.87,
                "updatedAt": pinned_memory_ts.isoformat().replace("+00:00", "Z"),
            }
        )
        compiled_round_trip = CompiledMemoryEntry.model_validate(
            compiled_entry.model_dump(mode="json", by_alias=True)
        )
        runner.check(
            compiled_round_trip.model_dump(mode="json", by_alias=True)
            == compiled_entry.model_dump(mode="json", by_alias=True),
            "Expected CompiledMemoryEntry to round-trip",
        )

        learned_repair = LearnedRepair.model_validate(
            {
                "repairId": generate_repair_id(),
                "domain": "testing-box.vercel.app",
                "normalizedRouteTemplate": "/dashboard",
                "frameContext": ["main"],
                "targetSemanticKey": "button:submit_login",
                "sourceRunId": generate_run_id(),
                "sourceStepId": generate_step_id(),
                "actor": "operator",
                "confidenceScore": 0.91,
                "metadata": {"source": "manual_fix"},
            }
        )
        learned_round_trip = LearnedRepair.model_validate(
            learned_repair.model_dump(mode="json", by_alias=True)
        )
        runner.check(
            learned_round_trip.model_dump(mode="json", by_alias=True)
            == learned_repair.model_dump(mode="json", by_alias=True),
            "Expected LearnedRepair to round-trip",
        )
        runner.check(
            bool(learned_repair.scope_key),
            "Expected LearnedRepair scope_key to be hydrated automatically",
        )

        schema_policy = SchemaPolicyVersion.model_validate(
            {
                "schemaVersion": "stepgraph@1.0",
                "policyVersion": "approval@2",
                "configVersion": "default@1",
                "activatedAt": "2026-04-23T00:00:00Z",
                "notes": "fixture policy pin",
            }
        )
        schema_round_trip = SchemaPolicyVersion.model_validate(
            schema_policy.model_dump(mode="json", by_alias=True)
        )
        runner.check(
            schema_round_trip.model_dump(mode="json", by_alias=True)
            == schema_policy.model_dump(mode="json", by_alias=True),
            "Expected SchemaPolicyVersion to round-trip",
        )

        cache_decisions = {decision.value for decision in CacheDecision}
        runner.check(
            cache_decisions == {"reuse", "partial_refresh", "full_refresh"},
            "Expected CacheDecision enum members to match spec",
        )

    return runner.finalize()


if __name__ == "__main__":
    raise SystemExit(main())

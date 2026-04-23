# System Composition

## Reused Components

### Hermes-Agent Reuse

- LLM-centered orchestration loop and tool-calling patterns.
- Memory management and self-learning primitives.
- Session-aware reasoning and background improvement patterns.

### Playwright CLI Reuse

- Stable command surface for browser actions and session operations.
- Snapshot/ref-based interaction model.
- Existing operational patterns for state, tabs, traces, video, and console/network inspection.

### playwright-repo-test Reuse

- Working record/replay/review flow.
- Deterministic locator candidates and validation strategy.
- Existing force-fix/manual-fix and LLM integration patterns.
- Session save/load and generated-spec workflow.

## Net-New Components

- Unified Step Graph contract spanning Manual/LLM/Hybrid modes.
- Runtime Mode Switch support (LLM ON/OFF mid-run without reset).
- Checkpoint and Event Log model for durable resume semantics.
- Policy layer for recovery decisions and security approvals.
- Standardized documentation and artifact schemas.
- LLM telemetry and cost-observability layer (calls/tokens/cache/cost/provider-model).
- Persistent Execution Memory layer (raw snapshots + compiled step memory + policy/schema).
- Cache and Invalidation engine for stateful reuse across steps and runs.
- Contradiction Resolver for selector/state conflicts over time.

## Integration Boundaries

- Hermes controls planning and policy in LLM mode; tool execution remains Playwright-native.
- Playwright CLI and recorder capabilities are treated as execution tools.
- Recovery and intervention decisions are persisted as first-class events.
- Provider adapters support OpenAI, Anthropic, and OpenAI-compatible endpoints (for example LM Studio).
- Raw browser evidence remains immutable; compiled memory is writable and continuously maintained.

## Three-Layer Memory Model

1. **Raw Evidence Layer**: page snapshots, accessibility trees, traces, console/network logs.
2. **Compiled Memory Layer**: locator bundles, step graph state, learned repairs, route signatures.
3. **Schema/Policy Layer**: conventions for updates, conflict resolution, confidence thresholds, and approvals.

This model allows cache-first execution while preserving auditable source evidence.

## Memory Update Path Defaults

- Raw Evidence Layer is append-only and never mutated in place.
- Compiled Memory Layer supports versioned upserts with provenance.
- Schema/Policy Layer changes are controlled by explicit config versions.
- Compiled updates must always retain linkage to raw evidence IDs.

## Data and Control Flow

1. Input enters as user intent, manual actions, or both.
2. Planner creates/updates Step Graph.
3. Cache/Invalidation engine decides `reuse`, `partial_refresh`, or `full_refresh`.
4. Execution Engine dispatches Playwright actions with pre/post conditions.
5. State Layer records events, checkpoints, and memory updates.
6. Recovery Policy applies retry/fix/escalation.
7. Artifacts are exported as portable manifests and optional generated code.

## Traceability Matrix

- **Hermes-Agent-derived**: orchestration, memory, learning loop, tool governance.
- **Playwright CLI-derived**: command/session/snapshot operational model.
- **playwright-repo-test-derived**: recorder/replay/heal baseline and practical UI handling patterns.
- **Net-new**: cross-mode runtime contract, checkpointed continuity, and formalized doc/spec system.

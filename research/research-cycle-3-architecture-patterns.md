# Research Cycle 3: Architecture Patterns for Pause/Fix/Resume

## Objective
- Define a robust execution architecture that supports long-running, human-in-the-loop browser automation with deterministic recovery.

## Pattern findings
- Durable workflow orchestration (Temporal-style) fits human latency:
  - Pause on approval/wait conditions without burning compute.
  - Resume from persisted state via signals/events.
  - Keep explicit timeout/escalation behavior.
- Event history + replay is critical:
  - Enables deterministic recovery after crashes.
  - Enables "time-travel debugging" and auditability.
- Continue-as-new / history compaction:
  - Necessary for long sessions to avoid replay bloat.
- Retry + idempotency + compensation:
  - Retry transient failures.
  - Use idempotency keys for duplicate-safe actions.
  - Add compensation for partially completed multi-step operations.

## Proposed architecture (logical layers)
- Intent Layer:
  - Convert NL + highlights into ordered step graph.
- Planner Layer:
  - Determine deterministic path vs LLM fallback path per step.
- Execution Layer:
  - Executes atomic actions and assertions.
  - Emits step events (`started`, `succeeded`, `failed`, `retried`, `paused`, `resumed`).
- State Layer:
  - Event-sourced run log + periodic snapshots.
  - Fast resume and replay support.
- Human Control Layer:
  - Approval/review gates, pause/fix, resume, reject with reason.
- Recovery Layer:
  - Retry policy, alternative locator strategies, optional compensation actions.

## Required runtime contracts
- Step IDs are stable and immutable within a run.
- Every side-effecting action is idempotency-guarded.
- Resume always references checkpoint + event offset.
- Human interventions become first-class events (not comments).
- Rejections carry machine-usable reason codes for future policy learning.

## Pause/fix/resume flow (recommended)
1. Step fails with classified reason (`not_found`, `ambiguous`, `not_actionable`, etc.).
2. Engine pauses run and serializes checkpoint.
3. User chooses:
   - manual in-browser fix, or
   - edit selector/action metadata.
4. System records intervention event and validates new candidate.
5. Resume from same state, not from run start.
6. Persist successful repair into selector knowledge store.

## Operational guardrails
- Max retries per step and per run.
- Circuit breaker when rejection/failure rate spikes.
- Step deadlines and whole-run SLAs.
- Dead-letter queue for irrecoverable runs.

## Why this matters for your product
- This design converts "AI test attempt" into "reliable production process."
- It directly supports your core differentiator: control without reset.

## Sources used in this cycle
- Temporal HITL and long-running workflow guidance
- Workflow pattern references (retry, compensation, idempotency)
- Event-sourcing pattern guidance (Azure + implementation docs)
- Playwright tracing docs for forensic debugging model

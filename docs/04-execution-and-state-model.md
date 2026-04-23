# Execution and State Model

## Step Graph Schema

Each step must minimally contain:
- `stepId` (stable and immutable within run)
- `mode` (action/assertion/wait/navigation/system)
- `target` (locator bundle + frame context)
- `preconditions`
- `postconditions`
- `timeoutPolicy`
- `recoveryPolicy`

## Event Log Schema

Required event types:
- `step_started`
- `step_succeeded`
- `step_failed`
- `step_retried`
- `run_paused`
- `run_resumed`
- `intervention_recorded`
- `mode_switched`
- `run_completed`
- `run_aborted`

## Checkpoint Contract

Checkpoint must include:
- current Step ID and event offset,
- browser session identity and selected tab/frame context,
- storage-state reference (if enabled),
- unresolved recovery state (if paused).

## Pause/Fix/Resume Flow

1. Step fails and is classified.
2. Engine emits `run_paused` and persists checkpoint.
3. Operator chooses manual repair, deterministic repair, or LLM assist.
4. System validates candidate fix and records `intervention_recorded`.
5. Engine emits `run_resumed` and continues from same Step ID.

## Retry and Escalation Policy

- Per-step max retries and per-run retry budget are required.
- Policy must escalate from deterministic retry to assisted repair before skip/abort.
- Repeated failure clusters trigger operator review prompt.

## Idempotency and Consistency Rules

- Side-effecting operations must be idempotency-aware where feasible.
- Event ordering is append-only and auditable.
- Resume always binds to checkpoint + event offset.
- Human interventions are data, not comments.

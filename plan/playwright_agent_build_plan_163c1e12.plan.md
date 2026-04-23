---
name: playwright agent build plan
overview: A phase-by-phase, task-by-task build plan for a brand-new Python project (`agent/`) that adapts code/patterns from `Hermes-Agent`, `playwright-cli`, and `playwright-repo-test` without modifying them, and delivers the Step Graph + checkpointed resume + cache-first execution + LLM orchestration described in `docs/`.
todos:
  - id: phase-0
    content: "Phase 0: Catalog reusable assets from Hermes-Agent, playwright-cli, playwright-repo-test in agent/PORTING_NOTES.md"
    status: pending
  - id: phase-1
    content: "Phase 1: Create agent/ skeleton, pyproject.toml, config loader, structured logging + run IDs"
    status: pending
  - id: phase-2
    content: "Phase 2: Define pydantic data contracts for Step Graph, events, checkpoints, cache, memory, telemetry"
    status: pending
  - id: phase-3
    content: "Phase 3: Playwright tool layer — browser session, snapshot/ref engine, all v1 tool functions, locator engine"
    status: pending
  - id: phase-4
    content: "Phase 4: Storage layer — SQLite schema, repositories, per-run filesystem layout"
    status: pending
  - id: phase-5
    content: "Phase 5: Manual-mode Step Graph executor with checkpoint, pause/resume CLI, manual fix flow"
    status: pending
  - id: phase-6
    content: "Phase 6: Recorder — capture user actions into a Step Graph and CLI command"
    status: pending
  - id: phase-7
    content: "Phase 7: Cache & invalidation engine wired into the runner"
    status: pending
  - id: phase-8
    content: "Phase 8: Memory layer — raw evidence, compiled memory, policy versioning, learned repairs, contradiction resolver"
    status: pending
  - id: phase-9
    content: "Phase 9: LLM layer — provider adapters, telemetry, staged context builder, orchestrator, mode switch"
    status: pending
  - id: phase-10
    content: "Phase 10: Policy & security — approval classifier, restrictions, audit log"
    status: pending
  - id: phase-11
    content: "Phase 11: Export — confidence gating, portable manifest, optional Playwright spec generator"
    status: pending
  - id: phase-12
    content: "Phase 12: Benchmarking — run summary report and experiment harness"
    status: pending
  - id: phase-13
    content: "Phase 13: Final top-level CLI wiring all commands together"
    status: pending
isProject: false
---

# Playwright Agent — Detailed Build Plan

## Ground Rules

- New project lives in a fresh top-level folder `agent/` (Python). Existing folders (`Hermes-Agent/`, `playwright-cli/`, `playwright-repo-test/`) are read-only references.
- Reuse strategy: **copy-and-adapt** code/patterns into `agent/`. No runtime imports from the three baselines.
- Build order is sequential: each phase produces a runnable artifact you can manually test.
- No automated tests in this plan — the user runs manual checks between phases and feeds back issues.
- Manual mode is built first (no LLM). LLM orchestration is layered in Phase 6 onward.
- Each task lists: **Goal**, **Sub-tasks**, **Deliverable**, **Done when**.

---

## Phase 0 — Discovery & Inventory (no code yet)

### Task 0.1 — Catalog reusable assets in `Hermes-Agent/`
- **Goal**: identify exactly which Hermes modules to port.
- **Sub-tasks**:
  1. Read `Hermes-Agent/agent/`, `hermes/`, `tools/`, `toolsets.py`, `model_tools.py`, `run_agent.py`, `cli.py`.
  2. Note: orchestration loop entry, tool-calling abstraction, memory primitives, provider adapters, telemetry hooks.
  3. Write findings to `agent/PORTING_NOTES.md` as a table: source path → target path in `agent/` → adapt/copy/reject.
- **Deliverable**: `agent/PORTING_NOTES.md` (Hermes section).
- **Done when**: every reused symbol has a target path planned.

### Task 0.2 — Catalog reusable assets in `playwright-cli/`
- **Goal**: identify the snapshot/ref command surface to port.
- **Sub-tasks**:
  1. Read `playwright-cli/playwright-cli.js` end to end.
  2. List every command (navigate, click, type, snapshot, tabs, trace, console, network, dialog, lock, etc.) with input/output shape.
  3. Append to `agent/PORTING_NOTES.md` (CLI section) — these become Python tool functions in Phase 3.
- **Deliverable**: command inventory in `PORTING_NOTES.md`.
- **Done when**: each command has a target Python function name and signature sketch.

### Task 0.3 — Catalog reusable assets in `playwright-repo-test/`
- **Goal**: extract the working record/replay/heal MVP behavior.
- **Sub-tasks**:
  1. Map: recorder UI → headless recorder logic; replay engine; force-fix path; manual-fix path; LLM hook; storage-state save/load; generated-spec writer.
  2. Note JS-only pieces we will redesign in Python (UI parts can be deferred to a later phase or replaced with CLI prompts).
  3. Append to `PORTING_NOTES.md` (repo-test section).
- **Deliverable**: completed `PORTING_NOTES.md`.
- **Done when**: every Phase 1–9 task can point at "porting source X" or "net-new".

---

## Phase 1 — Project Skeleton

### Task 1.1 — Create the `agent/` folder structure
- **Goal**: empty but well-organized Python package.
- **Sub-tasks**:
  1. Create folders: `agent/src/agent/`, `agent/src/agent/core/`, `agent/src/agent/stepgraph/`, `agent/src/agent/execution/`, `agent/src/agent/memory/`, `agent/src/agent/cache/`, `agent/src/agent/locator/`, `agent/src/agent/llm/`, `agent/src/agent/policy/`, `agent/src/agent/telemetry/`, `agent/src/agent/storage/`, `agent/src/agent/io/`, `agent/src/agent/cli/`, `agent/src/agent/recorder/`, `agent/src/agent/export/`, `agent/runs/`, `agent/artifacts/`, `agent/config/`, `agent/scripts/`.
  2. Add empty `__init__.py` to every package folder.
- **Deliverable**: directory tree.
- **Done when**: `tree agent/src` matches the structure above.

### Task 1.2 — Python tooling
- **Sub-tasks**:
  1. Create `agent/pyproject.toml` with project name `playwright-agent`, Python `>=3.11`, build backend `hatchling`.
  2. Add core deps: `playwright`, `pydantic>=2`, `typer`, `rich`, `httpx`, `tiktoken`, `anthropic`, `openai`, `pyyaml`, `aiosqlite`, `structlog`, `python-dotenv`.
  3. Add dev deps: `ruff`, `mypy`.
  4. Add `agent/.python-version`, `agent/.gitignore` (ignore `runs/`, `artifacts/`, `.venv/`, `__pycache__/`, `*.sqlite`).
  5. Create `agent/README.md` with run instructions.
  6. Create `agent/scripts/install.sh` running `uv sync` then `playwright install chromium`.
- **Deliverable**: project installs cleanly with `uv sync`.
- **Done when**: `python -c "import agent"` works.

### Task 1.3 — Config loader
- **Sub-tasks**:
  1. Create `agent/config/default.yaml` with sections: `mode` (default `manual`), `llm` (provider/model placeholders), `cache`, `policy` (approval thresholds), `storage` (sqlite path).
  2. Create `agent/src/agent/core/config.py`: pydantic `Settings` model, loader merging `default.yaml` + env + CLI overrides.
- **Deliverable**: `Settings.load()` returns validated config.
- **Done when**: invalid YAML produces a clear pydantic error.

### Task 1.4 — Structured logging + run IDs
- **Sub-tasks**:
  1. `agent/src/agent/core/logging.py`: configure `structlog` with JSON output to `runs/<run_id>/log.jsonl` and pretty console output via `rich`.
  2. `agent/src/agent/core/ids.py`: generators for `run_id`, `step_id`, `event_id` (ULID-style, monotonic).
- **Deliverable**: every module imports `get_logger(__name__)`.
- **Done when**: a smoke script writes a log line to both console and file.

---

## Phase 2 — Data Contracts (Pydantic models, no behavior)

### Task 2.1 — Step Graph schema
- **File**: `agent/src/agent/stepgraph/models.py`
- **Sub-tasks**:
  1. `LocatorBundle` (primary, fallbacks, confidence, reasoningHint, frameContext).
  2. `Precondition`, `Postcondition` (typed enums + payload).
  3. `TimeoutPolicy`, `RecoveryPolicy`.
  4. `Step` (stepId, mode, action, target, pre/post, timeout, recovery, metadata).
  5. `StepGraph` (run_id, steps[], edges[], version).
- **Done when**: round-trip `Step.model_validate(step.model_dump())` works.

### Task 2.2 — Event log schema
- **File**: `agent/src/agent/execution/events.py`
- **Sub-tasks**:
  1. Base `Event` with `event_id`, `ts`, `run_id`, `step_id?`, `actor`, `type`, `payload`.
  2. Subclasses for the 10 event types from `docs/04-execution-and-state-model.md`: `step_started/succeeded/failed/retried`, `run_paused/resumed/completed/aborted`, `intervention_recorded`, `mode_switched`.
- **Done when**: each event serializes deterministically.

### Task 2.3 — Checkpoint contract
- **File**: `agent/src/agent/execution/checkpoint.py`
- **Sub-tasks**:
  1. `Checkpoint` (current_step_id, event_offset, browser_session_id, tab_id, frame_path, storage_state_ref, paused_recovery_state).
- **Done when**: model defined; serializer round-trips.

### Task 2.4 — Cache & memory schemas
- **Files**: `agent/src/agent/cache/models.py`, `agent/src/agent/memory/models.py`
- **Sub-tasks**:
  1. `ContextFingerprint` (route_template, dom_hash, frame_hash, modal_state).
  2. `CacheDecision` enum (`reuse`, `partial_refresh`, `full_refresh`) + `CacheRecord`.
  3. `RawEvidence`, `CompiledMemoryEntry`, `SchemaPolicyVersion`.
  4. `LearnedRepair` with scope key `domain + normalizedRouteTemplate + frameContext + targetSemanticKey`.
- **Done when**: models match `docs/02` and `docs/05`.

### Task 2.5 — Telemetry schemas
- **File**: `agent/src/agent/telemetry/models.py`
- **Sub-tasks**:
  1. `LLMCall` (provider, model, callPurpose, contextTier, input_tokens, output_tokens, cache_read, cache_write, est_cost, actual_cost, latency_ms).
  2. `RunSummary` aggregations.
- **Done when**: KPIs from `docs/08` are all derivable from these fields.

---

## Phase 3 — Playwright Tool Layer (deterministic, no LLM)

### Task 3.1 — Browser session manager
- **File**: `agent/src/agent/execution/browser.py`
- **Sub-tasks**:
  1. Async wrapper around `playwright.async_api`.
  2. `BrowserSession.start()`, `.stop()`, `.new_context(storage_state=...)`, `.save_storage_state()`.
  3. Tab + frame tracking with stable IDs.
- **Done when**: a script can open a page, navigate, and close.

### Task 3.2 — Snapshot/ref engine (port from `playwright-cli`)
- **File**: `agent/src/agent/execution/snapshot.py`
- **Sub-tasks**:
  1. Capture accessibility-tree snapshot with element refs (mirror `playwright-cli/playwright-cli.js`).
  2. Compute `ContextFingerprint` from snapshot.
  3. Resolve `ref → ElementHandle`.
- **Done when**: snapshot YAML matches the shape used by `playwright-cli`.

### Task 3.3 — Tool functions (one per Playwright behavior)
- **File**: `agent/src/agent/execution/tools.py`
- **Sub-tasks**: implement async functions covering the v1 set in `docs/06`:
  1. `navigate`, `navigate_back`, `wait_for`, `wait_timeout`.
  2. `click`, `fill`, `type`, `press`, `check`, `uncheck`, `select`, `upload`, `drag`, `hover`, `focus`.
  3. `assert_visible`, `assert_text`, `assert_value`, `assert_checked`, `assert_enabled`, `assert_hidden`, `assert_count`, `assert_in_viewport`, `assert_url`, `assert_title`.
  4. `dialog_handle` (accept/dismiss/promptText).
  5. `frame_enter`, `frame_exit`.
  6. `tabs_list`, `tabs_select`, `tabs_close`.
  7. `console_messages`, `network_requests`, `screenshot`, `take_trace`.
- Each tool returns a typed result and emits a tool-call event.
- **Done when**: each tool callable from a REPL on a real page.

### Task 3.4 — Locator engine (port from `playwright-repo-test`)
- **File**: `agent/src/agent/locator/engine.py`
- **Sub-tasks**:
  1. Candidate generator following the priority order in `docs/05` (testid → aria/label → role+name → placeholder → text → scoped CSS → xpath).
  2. `score_candidate()` producing confidence (uniqueness, visibility, stability, history, freshness).
  3. `LocatorBundle.build(target)` returning primary + fallbacks + confidence.
- **Done when**: given a sample page, returns ranked bundles.

---

## Phase 4 — Storage Layer

### Task 4.1 — SQLite schema
- **File**: `agent/src/agent/storage/sqlite.py`
- **Sub-tasks**:
  1. Tables: `runs`, `events`, `checkpoints`, `step_graph`, `compiled_memory`, `learned_repairs`, `cache_records`, `llm_calls`.
  2. Migration script `agent/src/agent/storage/migrations/001_init.sql`.
- **Done when**: `init_db()` creates all tables on first run.

### Task 4.2 — Repositories
- **Files**: `agent/src/agent/storage/repos/{events,checkpoints,memory,cache,telemetry}.py`
- **Sub-tasks**: append-only writers for events and raw evidence; versioned upserts for compiled memory; readers with run/step filters.
- **Done when**: each pydantic model has a `save()` and `load()` path.

### Task 4.3 — Run filesystem layout
- **File**: `agent/src/agent/storage/files.py`
- **Sub-tasks**: per-run folder `runs/<run_id>/` with `log.jsonl`, `events.jsonl`, `snapshots/`, `traces/`, `screenshots/`, `storage_state.json`, `manifest.json`.
- **Done when**: layout helper returns absolute paths and creates dirs lazily.

---

## Phase 5 — Step Graph Execution Engine (Manual Mode)

### Task 5.1 — Step executor
- **File**: `agent/src/agent/execution/runner.py`
- **Sub-tasks**:
  1. Load Step Graph → iterate steps in order.
  2. For each step: emit `step_started` → evaluate preconditions → resolve locator → call tool → check postconditions → emit `step_succeeded`/`step_failed`.
  3. Honor `TimeoutPolicy` and `RecoveryPolicy` (deterministic retries only at this stage).
- **Done when**: a hand-authored Step Graph JSON runs end-to-end.

### Task 5.2 — Checkpoint + event log writer
- **File**: `agent/src/agent/execution/checkpoint_writer.py`
- **Sub-tasks**:
  1. Persist checkpoint after every step success and on pause.
  2. Append events to `events.jsonl` and SQLite `events` table.
- **Done when**: killing the runner mid-flow leaves a recoverable checkpoint.

### Task 5.3 — Pause / Resume CLI
- **File**: `agent/src/agent/cli/run_cmd.py`
- **Sub-tasks**:
  1. `agent run <stepgraph.json>` starts a run.
  2. `agent resume <run_id>` continues from checkpoint.
  3. `agent pause <run_id>` (signal-driven) emits `run_paused`.
- **Done when**: a paused run resumes from the exact failing step.

### Task 5.4 — Manual fix flow
- **File**: `agent/src/agent/cli/fix_cmd.py`
- **Sub-tasks**:
  1. On failure, prompt user with failure classification + candidate fixes.
  2. Allow `force-fix` (broaden deterministic match) or `manual-fix` (operator picks selector).
  3. Persist `intervention_recorded`.
- **Done when**: fixed step revalidates and run resumes.

---

## Phase 6 — Recorder

### Task 6.1 — Headless recorder
- **File**: `agent/src/agent/recorder/recorder.py`
- **Sub-tasks**:
  1. Hook Playwright's `page.on("...")` events to capture user actions.
  2. For each captured action: build LocatorBundle, create Step, append to in-memory Step Graph.
  3. On stop: write `stepgraph.json` and `manifest.json`.
- **Done when**: `agent record --url <u>` produces a replayable graph.

### Task 6.2 — Recorder CLI
- **File**: `agent/src/agent/cli/record_cmd.py`
- **Sub-tasks**: typer command, hotkey to stop, optional `--storage-state` to load auth.
- **Done when**: record → replay round-trip works on a sample app.

---

## Phase 7 — Cache & Invalidation Engine

### Task 7.1 — Fingerprint + cache decision
- **File**: `agent/src/agent/cache/engine.py`
- **Sub-tasks**:
  1. Compute current fingerprint and compare to cached.
  2. Decide `reuse` / `partial_refresh` / `full_refresh` based on triggers in `docs/03` (route change, DOM mutation in target scope, modal state change, stale ref).
  3. Emit cache decision telemetry.
- **Done when**: decision log appears for every step.

### Task 7.2 — Wire cache into runner
- **Sub-tasks**: before snapshot capture in `runner.py`, ask cache engine; only capture fresh context per decision.
- **Done when**: repeated stable-page steps show `reuse` decisions in the log.

---

## Phase 8 — Memory Layer

### Task 8.1 — Raw evidence writer
- **File**: `agent/src/agent/memory/raw.py` (append-only).

### Task 8.2 — Compiled memory store
- **File**: `agent/src/agent/memory/compiled.py` (versioned upserts with provenance to raw IDs).

### Task 8.3 — Schema/policy versioning
- **File**: `agent/src/agent/memory/policy.py` (config-version controlled).

### Task 8.4 — Learned repair store
- **File**: `agent/src/agent/memory/repairs.py`
- **Sub-tasks**:
  1. Scope key per `docs/05`: `domain + normalizedRouteTemplate + frameContext + targetSemanticKey`.
  2. Promotion gates: `candidate → trusted → degraded → retired`.
- **Done when**: a successful manual fix becomes a repair candidate, then promotes after N validations.

### Task 8.5 — Contradiction resolver
- **File**: `agent/src/agent/memory/contradictions.py`
- **Sub-tasks**: classify (`stale_locator` / `content_drift` / `structure_drift`), apply policy (`accept_new` / `keep_old` / `dual_track_with_fallback` / `require_manual_review`), persist conflict record with rollback path.

---

## Phase 9 — LLM Layer

### Task 9.1 — Provider abstraction
- **File**: `agent/src/agent/llm/provider.py`
- **Sub-tasks**:
  1. `LLMProvider` interface: `chat(messages, tools, ...)`.
  2. Adapters: `openai.py`, `anthropic.py`, `openai_compatible.py` (LM Studio).
  3. Reuse Hermes patterns from `Hermes-Agent/agent/` and `model_tools.py`; copy needed code into `agent/src/agent/llm/_ported/` with attribution comments.
- **Done when**: same prompt runs through all three adapters.

### Task 9.2 — Telemetry hooks
- **Sub-tasks**: every adapter emits `LLMCall` with `callPurpose` + `contextTier`; aggregator updates `RunSummary`.

### Task 9.3 — Staged context builder
- **File**: `agent/src/agent/llm/context.py`
- **Sub-tasks**: builders for Tier 0 (step + outcome), Tier 1 (+ scoped target), Tier 2 (+ history + contradictions), Tier 3 (full snapshot). Tokenizer-based preflight via `tiktoken`.

### Task 9.4 — Orchestrator (Hermes-style loop)
- **File**: `agent/src/agent/llm/orchestrator.py`
- **Sub-tasks**:
  1. Plan-act-observe loop with tool-calling against Phase 3 tools.
  2. Tier escalation policy with bounded retries and no-progress budget guard.
  3. `callPurpose` tagging for `plan` / `repair` / `classification` / `review`.

### Task 9.5 — Mode switch
- **File**: `agent/src/agent/core/mode.py`
- **Sub-tasks**: runtime LLM ON/OFF without state reset; emit `mode_switched` event; CLI `agent mode set <manual|llm|hybrid>`.

---

## Phase 10 — Policy & Security

### Task 10.1 — Approval classifier
- **File**: `agent/src/agent/policy/approval.py`
- **Sub-tasks**: classify each action as `auto_allow` / `review` / `hard_approval` per `docs/07`; CLI prompt for hard approvals.

### Task 10.2 — Restrictions
- **File**: `agent/src/agent/policy/restrictions.py`
- **Sub-tasks**: domain allow/deny list, file upload root allowlist, path normalization, `file://` block.

### Task 10.3 — Audit log
- **File**: `agent/src/agent/policy/audit.py`
- **Sub-tasks**: structured audit entries for every approval, mode switch, tool call, intervention, retry.

---

## Phase 11 — Export

### Task 11.1 — Confidence gating
- **File**: `agent/src/agent/export/gating.py`
- **Sub-tasks**: thresholds `<0.70 block`, `0.70–0.85 review`, `>=0.85 allow` per `docs/03`; machine-readable block reasons.

### Task 11.2 — Portable manifest writer
- **File**: `agent/src/agent/export/manifest.py`
- **Sub-tasks**: write `manifest.json` (Step Graph + locator bundles + fingerprints + provenance) suitable for downstream codegen.

### Task 11.3 — Optional Playwright spec generator
- **File**: `agent/src/agent/export/spec_writer.py`
- **Sub-tasks**: emit `*.spec.ts` (port the generator from `playwright-repo-test`).

---

## Phase 12 — Benchmarking & KPI Reporting

### Task 12.1 — Run summary report
- **File**: `agent/src/agent/telemetry/report.py`
- **Sub-tasks**: compute every KPI in `docs/08` from SQLite; CLI `agent report <run_id>` prints rich table + writes `runs/<run_id>/report.json`.

### Task 12.2 — Experiment harness
- **File**: `agent/src/agent/cli/bench_cmd.py`
- **Sub-tasks**: run the same Step Graph under Manual / LLM / Hybrid (with/without storage-state, with/without learned repairs) and aggregate KPIs.

---

## Phase 13 — CLI Surface (final integration)

### Task 13.1 — Top-level `agent` CLI
- **File**: `agent/src/agent/cli/__main__.py`
- **Sub-tasks**: typer app wiring `record`, `run`, `resume`, `pause`, `fix`, `mode`, `report`, `bench`, `export`.
- **Done when**: `agent --help` lists all commands.

---

## Build Order Cheat Sheet

```
0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13
discovery → skeleton → models → tools → storage → manual runner → recorder → cache → memory → LLM → policy → export → bench → CLI
```

After each phase you will manually test the produced artifact and feed back issues before moving to the next phase.
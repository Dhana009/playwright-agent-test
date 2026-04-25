# System Composition

## Layer Diagram

```
┌──────────────────────────────────────────────────────────┐
│                   Browser (Playwright Chromium)           │
│  ┌───────────────────────────────────────────────────┐   │
│  │          Page Under Test                          │   │
│  │  ┌─────────────────────────────────────────────┐ │   │
│  │  │   Panel Overlay  (Shadow DOM, injected JS)   │ │   │
│  │  │  Pick · Action Palette · Validate · Steps    │ │   │
│  │  │  Replay · Pause · Fix · Force-fix · Versions │ │   │
│  │  └─────────────────────────────────────────────┘ │   │
│  └───────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────┘
                        │  WebSocket / page.expose_binding
┌──────────────────────────────────────────────────────────┐
│                   Panel Bridge  (bridge.py)               │
│   Translates panel messages ↔ Python runner events        │
└──────────────────────────────────────────────────────────┘
                        │
┌──────────────────────────────────────────────────────────┐
│              Step Graph Runner  (runner.py)               │
│  step loop · pause/resume · preconditions/postconditions  │
│  checkpoint writer · event log · cache/invalidation       │
└──────────────────────────────────────────────────────────┘
        │                │                │
  Tool Layer        Locator Engine    Force-Fix Cascade
  (tools.py)        (engine.py)       (healing/force_fix.py)
  Playwright                           deterministic retries
  actions +                            → alternate strategies
  assertions                           → LLM last resort
        │
  Browser Session
  (browser.py / Playwright async API)
```

## Components

### Panel Overlay (new in v1)

- Static HTML/JS bundle injected as a Shadow DOM overlay into the Playwright-launched page.
- **Designed with Claude Design** — component mockups and styles generated with Claude Design, then inlined into the Shadow DOM bundle. No external design-system runtime dependency shipped with the panel.
- Communicates with the Python runner via the Panel Bridge.
- See `docs/10-panel-ux.md` for the full UX contract.
- Source: `agent/src/agent/panel/web/`

### Panel Bridge (new in v1)

- Lightweight WebSocket or `page.expose_binding` channel.
- Protocol: `pick_start`, `pick_result`, `validate_step`, `append_step`, `replay`, `pause`, `resume`, `fix`, `force_fix`, `save_version`.
- Source: `agent/src/agent/panel/bridge.py`

### Panel Command (new in v1)

- `agent panel --url <target>` — launches Chromium, injects the panel, boots the bridge, attaches to a run.
- Source: `agent/src/agent/cli/panel_cmd.py`

### Step Graph Runner (existing, extended)

- Iterates steps, emits events, honors pause/resume checkpoints.
- Extended: emits pause events to the bridge on postcondition failure; accepts `resume_with_override` carrying an edited step.
- Source: `agent/src/agent/execution/runner.py`

### Locator Engine (existing)

- Multi-strategy deterministic candidate generator with confidence scoring.
- Priority: test-id → aria/label → role+name → placeholder → text → scoped CSS → xpath.
- Top-N ranked candidates surfaced to the panel.
- Source: `agent/src/agent/locator/engine.py`

### Force-Fix Cascade (new thin layer)

- Retry current locator → try next-ranked candidate → try alternate strategies → LLM call (last resort only).
- LLM receives frozen DOM snippet + failed locator; returns repair or structured "stuck because…" explanation.
- Source: `agent/src/agent/healing/force_fix.py`

### Recording Versions (new)

- Copy-on-write save of a step-subset into a named version.
- List / load versions; originals are never mutated.
- Extends existing SQLite repo.
- Source: `agent/src/agent/stepgraph/versions.py`

### Recorder (existing, extended)

- Injects JS binding to capture user actions.
- Extended: hover-outline + pick-confirm handshake for the panel picker.
- Source: `agent/src/agent/recorder/recorder.py`

### Tool Layer (existing)

- Async Playwright wrappers: navigate, click, fill, type, check, upload, select, assert-*, dialog, frame, tabs.
- Source: `agent/src/agent/execution/tools.py`

### Storage Layer (existing)

- SQLite + per-run filesystem layout.
- Source: `agent/src/agent/storage/`

### LLM Providers (existing, scoped)

- Anthropic, OpenAI, OpenAI-compatible adapters.
- In v1 called **only** by the force-fix cascade.
- Source: `agent/src/agent/llm/`

## Deferred / Parked Subsystems

The following subsystems have partial implementations in `agent/src/agent/` but are **explicitly out of scope for v1**. They are preserved as-is and not deleted.

| Subsystem | Location | Parked reason |
|-----------|----------|---------------|
| Full LLM orchestrator plan-act-observe loop | `llm/orchestrator.py` | Not needed until picker loop is validated with real use |
| Contradiction resolver enforcement in runner | `memory/contradictions.py` | Deferred; locator lifecycle is handled by force-fix in v1 |
| Export confidence gating thresholds | `export/gating.py` | Deferred until export pipeline is wired end-to-end |
| Policy/approval classifier decision logic | `policy/approval.py` | Deferred; hard-approval prompt remains in runner but policy routing is not enforced |
| Human session testing framework | `testing/` | Orphaned; defer until panel slice is shipped |
| Benchmark/experiment harness | `cli/bench_cmd.py` | Deferred to Phase 2 |

## Data and Control Flow (v1)

1. User runs `agent panel --url <target>` → Chromium launches, panel injected.
2. User clicks Pick → recorder activates hover-outline mode.
3. User clicks element → recorder captures DOM descriptor → Locator Engine ranks candidates → bridge sends top-N to panel.
4. User chooses action + parameters → clicks Validate → bridge sends `validate_step` → runner executes one step → result returned to panel.
5. On pass: bridge sends `append_step` → step added to in-memory Step Graph.
6. On fail: panel shows error; user clicks Fix (manual edit) or Force-fix (cascade).
7. Replay: bridge sends `replay` → runner iterates graph → on any failure emits `pause` to bridge → panel jumps to failing step.
8. Resume: bridge sends `resume` (with optional override step) → runner continues from that step ID, not from step 1.
9. Save version: bridge sends `save_version` with selected step IDs and name → `stepgraph/versions.py` writes copy-on-write record to SQLite.

## Three-Layer Memory Model (preserved, not enforced in v1)

1. **Raw Evidence Layer**: page snapshots, accessibility trees, traces, console/network logs.
2. **Compiled Memory Layer**: locator bundles, step graph state, learned repairs, route signatures.
3. **Schema/Policy Layer**: conventions for updates, conflict resolution, confidence thresholds, approvals.

The raw evidence and compiled memory writers exist in `agent/src/agent/memory/`. They are written to during recording but the promotion / contradiction-enforcement logic is deferred to v2.

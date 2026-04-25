# Functional Requirements

## Panel Capabilities

The panel is the primary authoring surface. Every requirement is expressed as something the panel must do.

### Pick

- User clicks **Pick** in the panel → page enters hover-outline mode.
- Hovering an element draws a visible highlight border around it.
- Clicking a highlighted element ends pick mode and sends the element descriptor to the runner.
- Runner generates ≥1 locator candidate ranked by confidence score.
- Panel displays top candidates with strategy label (test-id / aria / role / CSS / xpath) and confidence score.
- User can accept the top candidate or choose a lower-ranked one.

### Action Palette

Panel exposes all v1 Playwright behaviors as named actions the user can choose after picking an element or independently:

**Actions (require picked element):**
`click`, `fill`, `type`, `check`, `uncheck`, `select`, `upload`, `drag`, `hover`, `focus`, `press`

**Assertions (require picked element):**
`assert-visible`, `assert-hidden`, `assert-text`, `assert-value`, `assert-checked`, `assert-enabled`, `assert-count`, `assert-in-viewport`

**Page-level (no element pick required):**
`assert-url`, `assert-title`, `wait-for`, `wait-timeout`, `navigate`, `navigate-back`, `dialog-handle`, `frame-enter`, `frame-exit`

For `fill` / `type` / `assert-text` / `assert-title` / `assert-url`: panel shows an inline text input.
For `upload`: panel shows a file path input.
For `select`: panel shows a value input.
For `wait-timeout`: panel shows a millisecond input.
For `dialog-handle`: panel shows accept/dismiss toggle.

### Validate in Place

- User clicks **Validate** → the single step (locator + action + parameters) executes against the live DOM.
- Result displayed inline: ✓ pass (green) or ✗ fail with reason (red).
- Validation does not add the step to the recording until the user clicks **Add Step**.
- User can validate multiple times (tweaking locator / parameters) before adding.

### Recording

- Validated steps are appended to the in-memory Step Graph in order.
- Step list shows: step number, action, primary locator (abbreviated), pass/fail indicator.
- User can reorder steps via drag (optional, v1.1) or delete a step.
- **Insert between**: user can pick a step from the list and insert a new step immediately before or after it.

### Replay

- User clicks **Replay** → runner iterates all recorded steps from step 1.
- Each step shows a running indicator while active.
- On success: step turns green.
- On failure: runner pauses; panel highlights the failing step with failure reason. Execution does not continue to later steps automatically.

### Pause / Resume

- **Pause mid-replay**: user can manually pause at any time; runner stops after the current step completes.
- **Auto-pause on failure**: runner emits a pause event to the bridge on any step failure.
- **Resume**: user clicks Resume → runner continues from the paused step ID, not from step 1.
- `resume_with_override`: if the user edited the failing step (changed locator or action), Resume sends the overridden step; runner substitutes it for the original and continues.

### Fix

**Manual Fix**: panel makes the failing step's locator and action parameters editable inline. User updates them and clicks Validate before resuming.

**Force-fix**: panel sends `force_fix` → runner's force-fix cascade runs:
1. Retry current locator (same selector, fresh DOM).
2. Try next-ranked candidate from the engine.
3. Try alternate-strategy selectors (aria swap, role swap, text anchor).
4. LLM last resort: send frozen DOM snippet + failed locator to LLM → LLM returns repaired locator or structured "stuck because…" explanation.
5. If all strategies fail: panel shows the LLM's explanation and leaves the step in manual-fix state.

Each cascade stage is shown in the panel as it runs so the user can see progress.

### Recording Versions

- Panel has a **Versions** dropdown showing the current recording ("main") and any saved versions.
- User can select a subset of steps (checkbox per step) and click **Save as version** with a name.
- Saved version is a copy-on-write snapshot — original "main" recording is unchanged.
- Loading a version sets the active step list to that version's steps.
- Versions are persisted to SQLite; survive session restart.

## Playwright Behavior Coverage (v1)

All items below must be reachable from the panel's action palette.

| Category | Actions |
|----------|---------|
| Navigation | navigate, navigate-back, wait-for, wait-timeout |
| Interaction | click, fill, type, press, check, uncheck, select, upload, drag, hover, focus |
| Element assertions | assert-visible, assert-hidden, assert-text, assert-value, assert-checked, assert-enabled, assert-count, assert-in-viewport |
| Page assertions | assert-url, assert-title |
| Dialog | dialog-handle (accept / dismiss / prompt-text) |
| Frame | frame-enter, frame-exit |
| Auth | load storage-state at session start; save storage-state at session end |

## Mode Requirements

### Manual Mode

- All panel capabilities work with no LLM provider configured.
- Deterministic locator cascade runs silently in the background.
- Force-fix ends at stage 3 (alternate strategies) if no LLM is configured; panel shows "No LLM configured — manual fix required."

### Hybrid Mode

- Manual mode capabilities are fully available.
- Force-fix cascade continues to stage 4 (LLM) when a provider is configured.
- Intent compression: user can describe a multi-step goal in English in the panel → LLM returns a draft step graph → panel loads it as an editable recording in Manual mode.
- LLM toggle (on/off): available per-step in the panel; toggling off returns to deterministic behavior immediately without resetting browser or checkpoint state.
- LLM calls emit `callPurpose` (`repair` for force-fix, `compression` for intent compression) and `contextTier` for cost attribution.

## Error and Recovery Requirements

- Failures classified by reason: `not_found`, `ambiguous`, `not_actionable`, `timeout`, `auth_required`, `restriction_violation`.
- Recovery options per step: retry (automatic, N times), force-fix, manual fix, skip, abort with save.
- Resume always continues from the paused step ID — never from step 1.
- Long waits (uploads, async processing) supported via `wait-timeout` + `wait-for` pair.

## Deferred Requirements (v2+)

The following requirements from earlier drafts are preserved but **not implemented in v1**:

- Contradiction resolver enforcement (stale_locator / content_drift / structure_drift detection and policy application).
- Export confidence gating (block/review/allow thresholds on portable manifest export).
- Policy/approval classifier for auto_allow / review / hard_approval routing.
- Cache/invalidation engine wired into replay (reuse / partial_refresh / full_refresh decisions).
- Full staged context builder for LLM (Tier 0–3 escalation) — v1 uses a fixed compact context for force-fix only.
- Benchmark/experiment harness.
- Human session testing framework.

## Acceptance Criteria

- `agent panel --url <target>` launches Chromium with panel visible.
- User can pick, add an action, validate, and record ≥3 steps including ≥1 assertion.
- Replay runs all steps; pass indicator shown per step.
- Deliberately break a locator → replay pauses at the failing step with reason shown.
- Force-fix cascade runs and either repairs the step or shows a "stuck because…" explanation.
- Resume continues from the fixed step, not from step 1.
- User saves a 2-step version from a 5-step recording; reloads it; original is unchanged.
- All capabilities above work with no LLM configured (Manual mode).
- With LLM configured, force-fix stage 4 (LLM) runs on exhaustion of deterministic strategies.

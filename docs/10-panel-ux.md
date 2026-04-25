# Panel UX Contract

## Overview

The panel is a Shadow DOM overlay injected into the Playwright-launched page. It floats in the bottom-right corner of the viewport and never covers more than 380px × 100% of viewport height. It communicates with the Python runner via the Panel Bridge.

**Visual design**: All component styles, colors, and layout are designed with Claude Design and inlined into the Shadow DOM bundle. No external CSS framework is loaded at runtime. The design uses a dark sidebar aesthetic that contrasts clearly with any page content without clashing.

---

## Layout

```
┌──────────────────────────────────────────┐
│  ⬤ Playwright Agent                 ─ ✕ │  ← header bar
│  [Manual ▾]          [🔗 hybrid off]     │  ← mode + LLM toggle
├──────────────────────────────────────────┤
│  PICK                                    │  ← always-visible pick button
│  ┌──────────────────────────────────┐   │
│  │ Waiting to pick…                 │   │  ← pick status
│  └──────────────────────────────────┘   │
├──────────────────────────────────────────┤
│  ACTION                                  │
│  [click          ▾]  [Validate]          │  ← action dropdown + validate
│  ┌──────────────────────────────────┐   │
│  │ ✓ Step passed                    │   │  ← inline result
│  └──────────────────────────────────┘   │
│  [+ Add Step]                            │
├──────────────────────────────────────────┤
│  RECORDING                               │  ← step list
│  1  navigate  https://…                 │
│  2  click     [data-testid="btn"]       │
│  3 ✗ fill      #username  ← failed     │  ← failing step highlighted
│  4  assert…                             │
│                                          │
│  [▶ Replay] [⏸ Pause] [▶ Resume]        │
│  [Fix] [⚡ Force-fix]                   │
├──────────────────────────────────────────┤
│  VERSIONS                                │
│  [main ▾]  [💾 Save version]            │
└──────────────────────────────────────────┘
```

Panel can be collapsed to a tab that shows only the header bar.

---

## Pick Interaction

1. User clicks **PICK** button.
2. Panel enters pick mode: page cursor changes to crosshair; panel shows "Click an element…".
3. Hovering any page element draws a 2px blue outline around it (injected via JS, not a CSS class on the element).
4. Clicking a highlighted element:
   - Ends pick mode; cursor returns to default.
   - Panel shows a locator candidate list (see below).
5. Pressing `Escape` cancels pick mode.

### Locator Candidate List

```
┌────────────────────────────────────────────────┐
│ Locators for: button "Upload PDF"              │
│                                                │
│  ● [data-testid="upload-pdf-btn"]   0.97  ★   │  ← primary, recommended
│  ○ role=button[name="Upload PDF"]   0.88      │
│  ○ .upload-section > button:last-of-type  0.61 ⚠ │  ← low confidence
│                                                │
│  [Use selected ▾]                              │
└────────────────────────────────────────────────┘
```

- `★` = recommended (top-ranked)
- `⚠` = low confidence warning (score < 0.70)
- User can select any candidate before proceeding to the action step.

---

## Action Palette

After picking an element, the Action dropdown shows contextual options based on element type:

| Element type | Default action | Available actions |
|---|---|---|
| button | click | click, assert-visible, assert-hidden, assert-text |
| input[text] | fill | fill, type, assert-value, assert-visible, assert-hidden |
| input[checkbox] | check | check, uncheck, assert-checked, assert-visible |
| select | select | select, assert-value, assert-visible |
| input[file] | upload | upload |
| any | click | full palette |

### Action Parameter Inputs

- `fill` / `type` / `assert-text` / `assert-title` / `assert-url`: shows inline text input.
- `upload`: shows file path input with a file-picker button.
- `select`: shows value input.
- `wait-timeout`: shows millisecond input (default 5000).
- `dialog-handle`: shows accept/dismiss radio.
- `assert-count`: shows integer input.
- `wait-for` (state): shows a state dropdown (visible / hidden / attached / detached).

Page-level actions (no element pick required) are accessible via an **Add page action** button that bypasses the pick flow.

---

## Validate in Place

- **Validate** button executes the step (locator + action + params) against the live DOM without adding it to the recording.
- Result shown inline immediately below the action row:
  - `✓ Passed (23ms)` — green
  - `✗ Failed: locator not found` — red, with reason text
  - `✗ Failed: element not actionable` — red
  - `✗ Failed: assertion mismatch — got "Hello", expected "Submit"` — red
- User can edit the locator or parameters and re-validate before adding.
- **Add Step** button appears on pass; clicking it appends to the recording.

---

## Step List

Each step row shows:
- Step number
- Action name (bold)
- Primary selector (truncated to 40 chars)
- Detail: URL for navigate, expected text for assert-text, value for fill, etc.
- Status icon: none (not run), `▶` (running), `✓` (passed), `✗` (failed), `⏸` (paused)

Clicking a step row selects it. A selected step can be:
- Deleted (delete key or trash icon)
- Duplicated
- Used as the insertion point for a new step (insert before / after buttons appear)

---

## Replay Controls

```
[▶ Replay]   Run all steps from step 1
[⏸ Pause]   Pause after current step completes
[▶ Resume]  Continue from the paused step
[Fix]        Make the failing step editable inline
[⚡ Force-fix]  Run the cascade (stages 1–4)
```

During replay each step's status icon updates live. On failure the step turns red, replay stops, and **Fix** / **Force-fix** buttons become active.

### Force-fix Progress Display

```
⚡ Force-fix running…
  Stage 1 — retrying same selector…  ✗
  Stage 2 — trying fallback selectors… ✗
  Stage 3 — alternate strategies…     ✓  role=button[name="Upload PDF"]

Step repaired with: role=button[name="Upload PDF"]
[Accept repair]  [Try anyway with original]
```

On LLM stage:
```
  Stage 4 — asking LLM…
  LLM response: "The upload button has moved inside a modal.
  Suggested: [data-testid='modal-upload-btn']"
  [Validate suggestion]  [Manual fix instead]
```

---

## Recording Versions

```
VERSIONS
[main (10 steps) ▾]  [💾 Save version]

Version list dropdown:
  • main (10 steps)         current
  • smoke-short (3 steps)   saved 2m ago
  • auth-only (2 steps)     saved 1h ago
```

**Save version flow**:
1. User clicks **💾 Save version**.
2. Panel shows checkboxes on each step row.
3. User selects desired steps; types a version name in an input field.
4. Clicks **Save** → version is persisted to SQLite; dropdown updates.
5. "main" recording is unchanged.

**Loading a version**: user selects from dropdown → step list updates to show that version's steps. A banner shows "Viewing version: smoke-short — [switch to main]".

---

## Panel Bridge Protocol

Messages sent from panel JS to Python runner (via WebSocket or expose_binding):

| Message type | Payload | Direction |
|---|---|---|
| `pick_start` | `{}` | JS → Python |
| `pick_result` | `{descriptor, candidates[]}` | Python → JS |
| `validate_step` | `{action, locator, params}` | JS → Python |
| `validate_result` | `{passed, error?, durationMs}` | Python → JS |
| `append_step` | `{step}` | JS → Python |
| `replay` | `{fromStepId?}` | JS → Python |
| `replay_step_status` | `{stepId, status, error?}` | Python → JS |
| `pause` | `{stepId, reason}` | Python → JS |
| `resume` | `{overrideStep?}` | JS → Python |
| `fix` | `{stepId, locator, params}` | JS → Python |
| `force_fix` | `{stepId}` | JS → Python |
| `force_fix_progress` | `{stage, status, repaired?, locator?}` | Python → JS |
| `save_version` | `{name, stepIds[]}` | JS → Python |
| `list_versions` | `{}` | JS → Python |
| `versions_response` | `{versions[]}` | Python → JS |

---

## End-to-End Acceptance Test

Manually verify these 10 steps to declare Phase 1 complete:

1. `agent panel --url https://playwright.dev` → Chromium opens → panel visible in bottom-right corner.
2. Click **Pick** → hover the "Get Started" link → blue outline appears → click → panel shows ≥2 locator candidates with confidence scores and strategy labels.
3. Select action `click`, click **Validate** → "Get Started" link clicked → panel shows `✓ Passed`. Click **Add Step**.
4. Click **Pick** on a heading element → select action `assert-text` → enter expected heading text → **Validate** → `✓ Passed`. **Add Step**.
5. Navigate to a page with an input → **Pick** input → action `fill` → value `hello` → **Validate** → `✓ Passed`. **Add Step**.
6. Click **▶ Replay** → all 3 steps run in order → all show `✓`.
7. In DevTools, rename `data-testid` on the button to break the locator → click **▶ Replay** → runner pauses at step 1 → step 1 shows `✗`, failure reason shown in panel.
8. Click **⚡ Force-fix** → cascade runs → panel shows stage progress → either step is repaired (stage 2 or 3 succeeds) or LLM explanation shown (stage 4 if LLM configured).
9. Click **▶ Resume** → steps 2 and 3 execute → both show `✓`. Confirm replay did not restart from step 1.
10. Click **💾 Save version** → check steps 2 and 3 → name it `"no-click"` → **Save** → dropdown shows `no-click (2 steps)`. Select `main` from dropdown → 3 steps shown. Select `no-click` → 2 steps shown. Originals unchanged.

# Locator and Healing Strategy

## Locator Bundle Contract

A locator bundle contains:
- `primarySelector` — the highest-confidence selector to try first.
- `fallbackSelectors[]` — ordered list of alternates; tried in order if primary fails.
- `confidenceScore` — 0.0–1.0 composite score (uniqueness × visibility × stability × history × freshness).
- `reasoningHint` — short machine-usable label (e.g. `"test-id:submit-btn"`, `"role:button[Upload]"`).
- `frameContext` — list of frame selectors if the element is inside an iframe.

## Deterministic Strategy Order

The engine always tries strategies in this priority order. Lower strategies are fallbacks, not alternatives:

1. `data-testid` / `data-test-id` / `data-pw` attributes
2. `aria-label` / `aria-labelledby` + label text
3. `role` + accessible name (Playwright `getByRole`)
4. `placeholder` / `name` attribute
5. Stable visible text anchor (`getByText` with exact match)
6. Scoped CSS / data attributes (non-volatile classes, `data-*` other than test-id)
7. Absolute XPath / nth fallbacks

**Deterministic-first is the default in all modes.** No LLM is called while any deterministic strategy has candidates to try.

## Confidence Scoring

Composite score per candidate reflects:
- **Uniqueness**: does the selector match exactly 1 element on the page? (0.0 if >1 match)
- **Visibility/actionability**: is the matched element visible and interactable?
- **Stability signal**: are there dynamic class names, volatile text, or inline style risks? (penalty)
- **Prior success history**: has this exact selector succeeded in prior runs on this route? (bonus)
- **Freshness**: how recently was the selector validated on a page with a matching fingerprint? (decay penalty for old evidence)

Low-confidence candidates (score < 0.70) are shown in the panel with a warning indicator. They can be used but trigger a "low confidence" annotation on the step.

## Selector Lifecycle

```
candidate ──(N validations)──▶ trusted
trusted ──(repeated failures)──▶ degraded
degraded ──(revalidated)──▶ trusted
degraded / trusted ──(force-retired)──▶ retired
```

In v1, the lifecycle states are computed and stored but **promotion/demotion enforcement is not wired into the runner** (deferred to v2). The panel displays the lifecycle state for informational purposes.

## Force-Fix Cascade (user-facing)

When a step fails during replay, the user can click **Force-fix**. The cascade runs in sequence, stopping at the first successful repair:

### Stage 1 — Retry current locator

Re-attempt the primary selector against the fresh DOM. Handles transient timing issues.

### Stage 2 — Try next-ranked candidate

Try each fallback selector from the locator bundle in ranked order. If any succeeds, the step is repaired using that selector.

### Stage 3 — Alternate-strategy generation

Generate new candidates using alternate strategies for the same semantic target:
- If the step used a role-based selector, try aria-label instead.
- If the step used a text anchor, try role + accessible name.
- If the step used CSS, try test-id or placeholder.

Each generated alternate is validated against the live DOM.

### Stage 4 — LLM last resort (Hybrid mode only)

If all deterministic strategies are exhausted and a LLM provider is configured:

1. Capture a frozen DOM snippet scoped to the element's last-known parent context (~500 tokens max).
2. Build a compact repair prompt:
   - Failed locator
   - Strategy that was used
   - DOM snippet
   - Element description (action, visible text, nearby labels)
3. LLM returns **one of**:
   - A repaired selector string (validated immediately against live DOM).
   - A structured "stuck because" explanation if it cannot determine a working selector.

4. If LLM returns a selector: validate live → if passes, step is repaired and recorded as an intervention event.
5. If LLM returns "stuck because": panel displays the explanation text and leaves the step in manual-fix state with the context surfaced for the user.

**LLM is never called if a deterministic stage succeeds first.** LLM is never called in Manual mode (no provider configured).

### Cascade Outcome Display

The panel shows each stage as it runs:
```
Force-fix running…
  ✗ Stage 1: retry — same selector, still failing
  ✗ Stage 2: fallback selectors — none matched
  ✓ Stage 3: alternate strategy — role[button "Upload"] succeeded
Step repaired.
```

Or on full cascade exhaustion:
```
  ✗ Stage 1–3: all deterministic strategies failed
  ↳ Stage 4: asking LLM…
  LLM: "The upload button is now inside a modal overlay that wasn't present during recording.
        Suggested selector: [data-testid='modal-upload-btn'] — please validate manually."
```

## Manual Fix

If the user prefers to fix manually rather than run the cascade:

1. Panel makes the failing step's locator editable inline.
2. User types or pastes a new selector.
3. User clicks **Validate** — live validation runs against the DOM.
4. On pass: step is updated; resume becomes available.
5. On fail: user can continue editing or trigger force-fix.

Successful manual fixes are recorded as `intervention_recorded` events and can be promoted to learned repairs in v2.

## Learned Repair Memory (v2)

Scope key for a learned repair:
- `domain` + `normalizedRouteTemplate` + `frameContext` + `targetSemanticKey`

v1 records the raw intervention data and scope key to SQLite, but **does not enforce repair promotion or contradiction resolution** (those are deferred to v2).

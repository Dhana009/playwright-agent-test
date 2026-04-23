# Locator and Healing Strategy

## Locator Bundle Contract

A locator bundle contains:
- `primarySelector`
- `fallbackSelectors[]`
- `confidenceScore`
- `reasoningHint` (short, machine-usable)
- `frameContext`

## Deterministic Strategy Order

Default priority order:
1. test id attributes
2. label/aria-based
3. role + accessible name
4. placeholder/name
5. stable text anchors
6. scoped CSS/data attributes
7. xpath/nth fallbacks

Deterministic first remains the default execution policy in all modes.

## Confidence Scoring

Confidence should reflect:
- uniqueness,
- visibility/actionability,
- stability signal (dynamic class risk, volatile text risk),
- prior success history.
- freshness score (how recently selector was validated in similar page signature).

Low-confidence selectors require explicit recovery policy and should be marked in export artifacts.

## Selector Lifecycle and Memory

- New locator bundles start in `candidate` state.
- A locator is promoted to `trusted` only after successful validation signals.
- A trusted locator can move to `degraded` when repeated mismatches occur.
- Degraded locators require revalidation before becoming default again.

## Confidence Lifecycle Defaults (v1)

- `candidate`: initial confidence from deterministic validation.
- `trusted`: promoted after repeated successful validations in matching route/template signature.
- `degraded`: set after repeated contradiction or actionability failure events.
- `retired`: no longer default; retained as historical fallback with provenance.

## Invalidation and Contradiction Policy

- Locator invalidation must trigger on:
  - route/template signature change,
  - target scope fingerprint mismatch,
  - repeated actionability failures.
- Contradictions between old and new locator evidence must be recorded with:
  - old selector,
  - new selector,
  - confidence delta,
  - decision rationale.
- Default conflict rule: validated newer selector wins when confidence is equal or higher.
- If confidence drops, keep old selector as fallback and require manual or LLM-assisted confirmation.
- If contradiction repeats after promotion, auto-demote selector to `degraded` and trigger review policy.

## Force-Fix and Manual Fix

- **Force-Fix**: broaden deterministic matching with guarded fallback rules.
- **Manual Fix**: operator picks/edits selector in context and validates before continue.
- Successful fixes become Intervention Events and can be promoted to learned patterns.

## LLM-Assisted Repair Policy

- LLM assist can be invoked by policy or operator.
- In LLM-Orchestrated mode, LLM decides when to call deterministic/force/manual tools.
- In Hybrid mode, operator can enable LLM assist per step and disable afterward.

## Learned Repair Memory Rules

- Store repairs with scoped keys (domain, route/template signature, frame context).
- Require validation signal before promoting learned repair as default.
- Keep rollback path for poisoned or regressive learned selectors.
- Keep per-repair provenance and expiry metadata to prevent permanent drift lock-in.

## Canonical Scoping Key Defaults (v1)

- Default learned-repair scope key must include:
  - `domain`
  - `normalizedRouteTemplate`
  - `frameContext`
  - `targetSemanticKey` (prefer test id, else role+accessible-name hash)
- Optional scope discriminator may include `appVersion` when available to reduce cross-release drift.
- Repairs must not be promoted across different `domain` or `normalizedRouteTemplate` values.
- If `targetSemanticKey` is unavailable, repair remains route-scoped and requires stricter revalidation before promotion.

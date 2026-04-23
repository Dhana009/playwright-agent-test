# Functional Requirements

## Core Requirements

- System must support full flow authoring and execution, not locator-only operations.
- Step Graph must include action, locator bundle, preconditions, postconditions, timeout, and recovery metadata.
- Runtime must support record, replay, review, and export workflows.
- System must support pause/fix/resume from current checkpoint.
- System must support cache-first execution using previously compiled page/step memory.

## Playwright Behavior Coverage

Required v1 behavior coverage:
- Click, fill, type, check/uncheck, select, upload, drag, hover, focus, press.
- Wait conditions: timeout waits, element visible, URL match, title checks, load-state checks.
- Assertions: element/text/value/checked/enabled/hidden/count/in-viewport, plus page URL/title.
- Modal/dialog handling with accept/dismiss behavior.
- iframe-aware interaction targeting.
- Auth/session handling with save/load storage state.

## Mode-Specific Behavior

- **Manual Mode**: deterministic execution and operator-driven decisions.
- **LLM-Orchestrated Mode**: Hermes LLM plans and decides each next action.
- **Hybrid Mode**: user can request LLM assist for selected steps, then return to manual control.

## LLM Usage and Cost Observability Requirements

- System must track per-run and per-step LLM telemetry:
  - total LLM calls,
  - input tokens,
  - output tokens,
  - estimated and actual cost.
- System must persist provider/model metadata for each LLM call.
- System must expose prompt caching metrics where provider supports them:
  - cache write tokens,
  - cache read tokens,
  - cache hit/miss ratio.
- System must support tokenizer-based preflight estimates and compare them against provider-reported usage.
- Telemetry must be visible in run summaries and exportable for benchmark analysis.

## Provider Compatibility Requirements

- LLM layer must support:
  - OpenAI-native APIs,
  - Anthropic-native APIs,
  - OpenAI-compatible endpoints (including LM Studio).
- Provider selection must be configurable per workspace and overridable per run.
- Hybrid and Manual modes must run even when no external LLM provider is configured.

## Token Efficiency Policy Requirements (v1)

- Runtime must be deterministic-first: Playwright-native tools execute by default; LLM is used for planning, ambiguity resolution, and recovery decisions.
- LLM planning must follow staged context escalation:
  - Tier 0: step metadata and recent outcome only,
  - Tier 1: add scoped target context,
  - Tier 2: add local history and contradiction context,
  - Tier 3: full snapshot/context only when prior tiers cannot resolve safely.
- Every LLM call must emit `callPurpose` (`plan`, `repair`, `classification`, `review`) and `contextTier` for cost attribution.
- System must prevent redundant context capture in stable page state using context fingerprints and cache decision telemetry.
- Recovery policy must enforce bounded retries and escalate to operator review when token spend rises without progress.
- Provider/model routing must support low-cost default models for classification/planning and higher-capability models for targeted repair only.

## Runtime Toggle Requirements (LLM ON/OFF Mid-Run)

- Mode Switch must be available at run time for current step.
- Mode Switch must not reset browser/session/checkpoint state.
- Mode Switch events must be logged with reason and actor.
- On LLM OFF, execution returns to deterministic/manual policy immediately.

## Cache and Invalidation Requirements

- Every step must attempt cached context reuse before requesting fresh page context.
- Reuse decision must support three outcomes:
  - `reuse` (no refresh),
  - `partial_refresh` (targeted region/step context),
  - `full_refresh` (state reset required).
- Invalidation triggers must include at least:
  - navigation/route change,
  - significant DOM mutation in target scope,
  - modal/overlay state change affecting actionability,
  - stale ref/locator mismatch on validation.
- System must persist per-step context fingerprints to evaluate reuse safely.
- System must avoid duplicate context capture within the same stable page state.

## Contradiction and Freshness Requirements

- Selector/state contradictions must be detected and classified (`stale_locator`, `content_drift`, `structure_drift`).
- Conflict policy must define winner strategy (for example: validated newer evidence wins unless lower confidence).
- Successful post-conflict repair must update compiled memory and preserve prior version for rollback.
- System must keep provenance for each learned repair (source step/run, timestamp, actor/mode).
- Contradiction handling must be deterministic: same inputs must produce same conflict outcome.
- Policy must support explicit outcomes:
  - `accept_new`,
  - `keep_old`,
  - `dual_track_with_fallback`,
  - `require_manual_review`.

## Error and Recovery Requirements

- Failures must be classified by reason code (`not_found`, `ambiguous`, `not_actionable`, `timeout`, `auth_required`, etc.).
- Recovery options must include retry, force-fix, manual fix, LLM assist, skip, and safe abort with save.
- Long waits (for uploads/processing) must support bounded timeout + fallback checks.
- Resume must continue from current Step ID and checkpoint, not replay from start.
- Recovery policy must include token-budget-aware stop conditions to avoid repeated expensive loops with no state change.

## Export Confidence Gating Requirements

- Export must evaluate per-step confidence before generating portable artifacts.
- Default v1 confidence thresholds:
  - `< 0.70`: block export by default,
  - `0.70-0.85`: allow only with explicit review annotation,
  - `>= 0.85`: allow by default.
- Any blocked or review-gated export must include machine-readable reasons and implicated Step IDs.

## Acceptance Criteria

- Mid-run LLM toggle works without state loss.
- iframe and modal scenarios are executable in both Manual and LLM modes.
- Storage-state reuse can skip repeated login when configured.
- Recovery path resolves most mid-flow failures without full restart.
- Cache reuse works across repeated step attempts without unnecessary full context recapture.
- Token efficiency telemetry shows context-tier escalation instead of full-context-by-default behavior.
- Export confidence gates enforce block/review/allow outcomes according to policy thresholds.

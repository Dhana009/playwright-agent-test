# Open Questions and Decisions

## Open Questions

- What is the exact fallback policy for unstable nested iframes?
- Is Shadow DOM explicitly deferred or conditionally included in v1 scope?
- What is the default contradiction winner rule when newer evidence has lower confidence?
- What are the exact thresholds for `reuse` vs `partial_refresh` vs `full_refresh`?
- How long should learned repairs remain active before mandatory revalidation?

## Decisions Made

- Three runtime modes are mandatory: Manual, LLM-Orchestrated, Hybrid.
- LLM is the orchestrator in LLM mode, not a generic fallback helper.
- Runtime mode switch (LLM ON/OFF) must work mid-run without state reset.
- Pause/fix/resume must continue from checkpoint, not from run start.
- System scope is full flow intelligence, not locator-only behavior.
- System architecture uses three memory layers: raw evidence, compiled memory, and schema/policy.
- Cache-first execution is required; full context recapture is not default behavior.
- Contradiction handling must be explicit and auditable, with rollback support.
- Canonical learned-repair scoping key is `domain + normalizedRouteTemplate + frameContext + targetSemanticKey` (optional `appVersion` discriminator).
- Default export confidence gating is:
  - `< 0.70`: block,
  - `0.70-0.85`: review-gated,
  - `>= 0.85`: allow.
- Default hard-approval action set includes:
  - final submit actions with server-side effects,
  - destructive mutations,
  - external post/send operations,
  - auth/permission mutations,
  - local file uploads.

## Key Design Decisions (Locked)

- Raw evidence is immutable; compiled memory is versioned and writable.
- Cache decision model is always tri-state: `reuse`, `partial_refresh`, `full_refresh`.
- Contradiction resolution is policy-driven and deterministic.
- Runtime mode changes are first-class events in the event log.
- Token-efficiency policy is staged-context by default, not full-context by default.

## Default Operating Values (v1)

- Default run mode: `Hybrid`.
- Default cache strategy: `cache-first`.
- Default contradiction winner: `validated newer evidence` when confidence is not lower.
- Default fallback strategy on lower-confidence contradiction: `dual_track_with_fallback`.
- Default response when contradiction cannot be resolved automatically: `require_manual_review`.
- Default provider posture: support OpenAI, Anthropic, and OpenAI-compatible endpoints (including LM Studio).
- Default context strategy for LLM calls:
  - start at Tier 0,
  - escalate only when unresolved,
  - avoid repeated no-progress retries beyond policy budget.

## Deferred Decisions

- Enterprise governance design details beyond baseline security controls.
- Full autonomous multi-app orchestration strategy.
- Complete Shadow DOM coverage timeline.
- Optional search/index layer over compiled memory when index files outgrow practical context size.

## Decision Owners and Dates

- **Product owner**: Dhanunjaya (final scope and mode policy).
- **Architecture owner**: TBD.
- **Security owner**: TBD.
- **Benchmark owner**: TBD.

Update this file whenever a major architecture or policy decision changes.

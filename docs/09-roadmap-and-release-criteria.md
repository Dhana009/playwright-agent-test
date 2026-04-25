# Roadmap and Release Criteria

## Phase 1 (current — picker-panel slice)

**Goal**: one user can launch the panel, record ≥3 steps including ≥1 assertion, replay, hit a failure, fix/force-fix, resume from the failing step, and save a named version. All from the panel, never restarting from scratch.

**Deliverables**:
- `agent panel` CLI command
- Panel overlay (Shadow DOM) with full Manual mode action palette
- Panel bridge (WebSocket / expose_binding)
- Recorder extension for hover-outline + pick-confirm
- Force-fix cascade (stages 1–3 deterministic + stage 4 LLM when configured)
- Recording versions (copy-on-write, SQLite-backed)
- Runner extended with pause-to-bridge on failure + resume_with_override
- Updated docs: 01, 02, 03, 05, 09, 10 (this pass)

**Out of scope for Phase 1** (explicitly deferred — code preserved, not deleted):
- Contradiction resolver enforcement
- Export confidence gating thresholds
- Policy/approval classifier routing
- Full LLM orchestrator plan-act-observe loop
- Cache/invalidation wired into replay
- Benchmark/experiment harness
- Human session testing framework integration

**Go criteria**:
- All 10 steps of the acceptance test in `docs/10-panel-ux.md` pass manually.
- Panel works with no LLM provider configured (Manual mode fully functional).
- Force-fix LLM stage runs and returns useful output with a provider configured.

## Phase 2

- Wire contradiction resolver into the runner (stale_locator / content_drift / structure_drift detection + policy).
- Export confidence gating thresholds applied to portable manifest export.
- Cache/invalidation engine wired into replay (reuse / partial_refresh / full_refresh decisions).
- Adaptive recovery policies and dynamic confidence thresholds.
- Repair memory promotion (candidate → trusted → degraded) enforced.
- Cost/reliability summary panel in the UI.

## Phase 3

- Full LLM orchestrator plan-act-observe loop (intent compression from a full English description, not just single-goal compression).
- Policy/approval classifier decision logic.
- Benchmark/experiment harness.
- Multi-flow orchestration.
- Enterprise integration features (SSO/RBAC/compliance workflows).
- Extended framework adapters.

## Go / No-Go Criteria (Phase 1)

Go if:
- Pause/fix/resume loop works reliably on real apps.
- Force-fix cascade resolves >60% of single-locator failures without LLM in a sample test.
- Panel is usable without reading any docs (self-describing action palette).
- Recording version save/load round-trip is lossless.

No-go / pivot if:
- Injected panel causes CSS collision or JS conflicts on real target pages that can't be mitigated with Shadow DOM scoping.
- Pause/resume checkpoint semantics break across browser navigation events.
- Force-fix LLM stage produces locators that silently match the wrong element.

## Risks and Mitigations

- **Risk**: Shadow DOM panel interferes with the page's own event handling.
  **Mitigation**: use `pointer-events: none` on the overlay wrapper except on panel UI elements; contain all event listeners inside Shadow DOM.

- **Risk**: LLM force-fix returns a selector that matches a different element (false positive repair).
  **Mitigation**: every LLM-returned selector is validated live before being accepted; the panel shows what matched so the user can confirm.

- **Risk**: Pause/resume breaks after page navigation between steps.
  **Mitigation**: checkpoint includes `browser_session_id` + `tab_id`; runner re-attaches to the existing tab after navigation.

- **Risk**: Learned repairs regress future runs (v2 concern, but intervening events accumulate in v1).
  **Mitigation**: intervention events are stored with full provenance; promotion logic is gated in v2; no auto-promotion in v1.

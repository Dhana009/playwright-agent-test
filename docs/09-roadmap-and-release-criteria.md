# Roadmap and Release Criteria

## Phase 1 (MVP)

- Unify Manual and LLM-Orchestrated execution under one Step Graph model.
- Deliver Hybrid mode with runtime LLM ON/OFF switch.
- Implement checkpoint/event-log pause/fix/resume.
- Implement three-layer memory model (raw evidence, compiled memory, schema/policy).
- Implement cache/invalidation engine with `reuse` / `partial_refresh` / `full_refresh`.
- Cover required Playwright behaviors: waits/assertions/iframe/modal/storage-state.
- Implement contradiction detection and auditable conflict-resolution policy for locator/state drift.
- Export portable manifests for downstream codegen pipelines.

## Phase 2

- Adaptive recovery policies and dynamic confidence thresholds.
- Repair memory quality controls and rollback handling.
- Stronger approval rules and policy automation.
- Cost/reliability dashboards for team-level operations.

## Phase 3

- Multi-flow orchestration and larger workflow packs.
- Extended framework adapters and enterprise integration features.
- Governance features (SSO/RBAC/compliance workflows).

## Go / No-Go Criteria

Go if:
- KPI targets are met across realistic benchmark suites.
- Hybrid mode materially improves operator throughput.
- Restart-from-zero behavior is rare and bounded.
- Cache hit and refresh-ratio thresholds in benchmark gates remain stable.

No-go or pivot if:
- Recovery still requires frequent full reruns.
- Deterministic + assisted strategy fails on practical UI entropy.
- Portability value is weak for target teams.

## Risks and Mitigations

- **Risk**: mode complexity increases operator confusion.  
  **Mitigation**: explicit mode indicators and action-scoped assist prompts.
- **Risk**: learned repairs regress future runs.  
  **Mitigation**: scoped memory keys + validation before promotion.
- **Risk**: stale cached context causes incorrect actions.  
  **Mitigation**: strict invalidation triggers + targeted partial refresh before fallback full refresh.
- **Risk**: cost rises in high-assist sessions.  
  **Mitigation**: visibility and budgets on LLM assist usage.

# Research Cycle 6: Product Strategy and Roadmap

## Final strategic objective
- Build the most cost-efficient and controllable automation flow engine for teams using Playwright-style ecosystems.

## Product thesis (final)
- "Reliability per token" is the market gap.
- Win by combining:
  - deterministic selector intelligence,
  - human-guided recovery,
  - minimal-context LLM orchestration,
  - and portable outputs for downstream code generation.

## ICP (ideal customer profile)
- Mid-size to enterprise QA/platform teams with:
  - growing E2E suites,
  - rising cloud-LLM/test infra costs,
  - pain from flaky selectors and reruns.
- Secondary ICP:
  - AI-first engineering teams building in-house browser agents.

## Wedge use case
- "Convert manual flow instructions + highlights into stable locator/action manifests with pause/fix/resume recovery."
- This avoids direct head-to-head with full no-code suites on day one.

## Packaging direction (early)
- Developer-first platform component, not monolithic replacement:
  - API/SDK for orchestration and locator generation
  - UI console for run control and approvals
  - export adapters for Playwright/POM codegen pipelines
- Pricing anchor:
  - usage-based around successful flow execution and optional premium recovery/security controls.

## 3-phase roadmap
- Phase 1 (MVP, 8-12 weeks):
  - NL + highlight -> structured step graph
  - deterministic locator generation + confidence
  - pause/fix/resume loop
  - exportable locator manifest
  - baseline telemetry for token and recovery metrics
- Phase 2:
  - adaptive fallback policies
  - selector memory and self-learning repair store
  - security policies and approval rule engine
  - dashboard for flakiness/cost trends
- Phase 3:
  - multi-flow orchestration
  - richer framework adapters
  - enterprise governance (SSO, RBAC, compliance workflows)

## What to build now vs later
- Build now:
  - deterministic-first core and checkpointed runtime
  - human repair UX
  - measurable benchmark harness
- Build later:
  - fully autonomous multi-app agents
  - broad no-code authoring surface
  - extensive visual/CUA-first features

## Go / no-go criteria after MVP pilot
- Go if:
  - significant token reduction with no meaningful drop in completion rate
  - repair loop materially cuts restart/rework
  - teams can consume exported manifests in existing pipelines
- No-go or pivot if:
  - deterministic-first fails to handle practical UI entropy
  - human intervention overhead remains too high
  - portability value is weak relative to full-suite vendors

## Final one-line positioning
- "A token-efficient, human-in-the-loop locator and execution control plane that keeps browser automation running through failures without expensive re-generation."

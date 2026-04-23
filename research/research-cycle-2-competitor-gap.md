# Research Cycle 2: Competitor and Gap Analysis

## Objective
- Map current solutions against your vision: token-efficient, human-guided, pause/fix/resume automation with locator export.

## Competitor signals (evidence-backed)
- Playwright Codegen/CLI:
  - Strong deterministic locator generation (`role`, `text`, `testId`), inspector controls, low-friction code output.
  - Weakness for your use case: no first-class NL-to-structured flow planner with constrained token orchestration.
- Playwright MCP:
  - Efficient snapshots vs screenshots; mode controls; flexible automation tooling.
  - Weakness: context/token overhead can still spike on large pages if not carefully constrained.
- mabl:
  - Advanced auto-healing, multi-attribute element history, confidence-aware adaptation.
  - Weakness: platform-centric flow; less emphasis on exportable low-level locator manifest for external codegen chains.
- Autify:
  - Strong recorder/editor UX, step-level editing, locator adjustments, conversion paths.
  - Weakness: still recorder/platform bounded; limited explicit token-optimization narrative.
- Reflect:
  - Multi-selector fallback + AI fallback for stale selectors; practical resilience model.
  - Weakness: less explicit user-steerable token budgets and region-scoped extraction controls.
- testRigor:
  - Powerful locatorless plain-English model from user perspective.
  - Weakness: abstraction can reduce transparency/control over deterministic selector artifacts needed by engineering teams.

## Where your product can win
- Deterministic-first + LLM-fallback by design (cost is productized, not incidental).
- In-browser pause/fix/resume with state continuity, not rerun-from-start.
- Region-scoped extraction and reusable context cache to avoid full recapture loops.
- Export contract explicitly built for downstream codegen engines and POM workflows.
- Confidence-scored locator bundles (primary + fallback + rationale).

## Gap matrix (bullet form)
- **Low token control in market**: most platforms optimize reliability first, cost second.
- **Limited repair-in-place UX**: many tools support debugging, fewer support seamless resume after manual repair in the same run-state.
- **Weak artifact portability**: many outputs are tied to the platform, not neutral manifests.
- **Insufficient hybrid governance**: either too manual or too autonomous; fewer products optimize adaptive autonomy per-step risk/cost.

## Suggested category positioning
- Not "another no-code test recorder."
- Position as:
  - "Token-efficient automation control plane"
  - "Locator intelligence and recovery layer"
  - "Human-in-the-loop execution engine for resilient test generation"

## Strategic implications
- Prioritize interoperability (Playwright-first adapters) over full-stack monolith.
- Sell reliability-per-dollar and recovery speed, not only "AI magic."
- Treat prompt/token governance as a visible feature (budgets, cost reports, cache hit ratios).

## Sources used in this cycle
- Autify docs: step editing, recorder flow, trace debugging
- mabl docs/pages: adaptive auto-healing and attribute modeling
- Reflect docs/articles: multi-selector + AI fallback behavior
- testRigor docs/blog: plain-English locatorless strategy
- Playwright docs and CLI repo materials

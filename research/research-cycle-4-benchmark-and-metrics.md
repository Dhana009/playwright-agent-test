# Research Cycle 4: Benchmark and Metrics Plan

## Objective
- Define a reproducible evaluation harness for cost, reliability, and recovery performance.

## Benchmark principles
- Reproducible environments (containerized sites and fixed datasets).
- Fixed task set with versioned scenarios and expected outcomes.
- Deterministic scoring pipeline and per-run artifacts.
- Cross-run tracking for trends, not one-off snapshots.

## Task suite design
- Compose a balanced suite:
  - Form fill + upload + submit
  - Multi-page navigation with assertions
  - Dynamic UI changes / locator drift scenarios
  - Recovery-required scenarios (forced failures)
  - High-noise pages (large DOM/a11y trees)
- Include both "known stable UI" and "changing UI" tracks.

## Core KPI set
- Cost:
  - Tokens per successful step
  - Tokens per completed flow
  - Cost per completed flow
- Reliability:
  - First-pass step success rate
  - Flow completion rate
  - Flaky rate (passes only on retry)
- Recovery:
  - Mean time to recover (MTTR-step)
  - Resume success rate
  - Restart avoidance rate
- Efficiency:
  - Context reuse ratio
  - Snapshot size and retrieval ratio
  - LLM fallback rate (lower is better, up to quality threshold)

## Instrumentation recommendations
- Token accounting:
  - Preflight count and post-call actual usage capture per model/provider.
  - Separate prompt-cache read/write/miss where available.
- Execution telemetry:
  - OpenTelemetry spans per run/step/retry.
  - Metrics counters and histograms for outcomes, latency, retry counts.
- Artifact capture:
  - Snapshots/traces/logs linked by run and step IDs.

## Experiment matrix (minimum)
- Baselines:
  1. Full-context LLM-first approach
  2. Accessibility snapshot default approach
  3. Proposed deterministic-first + scoped extraction + fallback LLM
- Variants to test:
  - With/without region highlighting
  - With/without context cache
  - With/without prompt caching
  - Different fallback thresholds

## Acceptance targets for MVP (initial)
- >= 30% token reduction vs baseline 2
- >= 50% token reduction vs baseline 1
- >= 90% step recovery success after manual intervention
- <= 10% runs requiring full restart
- No material drop in final flow success rate vs strongest baseline

## Reporting cadence
- Daily: smoke metrics for regressions
- Weekly: trend review (cost, reliability, recovery)
- Release gate: fail build if cost or flaky thresholds regress beyond budget

## Sources used in this cycle
- OpenAI token counting guidance (tiktoken)
- Playwright retries/flaky classification docs
- SWE-bench evaluation reproducibility principles
- OTel reliability instrumentation patterns

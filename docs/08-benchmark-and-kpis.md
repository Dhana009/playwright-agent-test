# Benchmark and KPIs

## Baselines

Compare against:
1. LLM-first full-context execution baseline.
2. Snapshot-heavy baseline without scoped reuse.
3. Proposed architecture (deterministic tools + LLM orchestrator + checkpointed recovery).

## KPI Definitions

- **Tokens per successful step**
- **Cost per completed flow**
- **LLM calls per completed flow**
- **Input tokens per call**
- **Output tokens per call**
- **Token efficiency per resolved step** (successful or safely escalated step / total tokens spent)
- **Prompt cache hit ratio** (for providers with caching support)
- **Flow completion rate**
- **First-pass step success rate**
- **MTTR-step**
- **Restart avoidance rate**
- **LLM assist invocation rate**
- **Context reuse ratio**
- **Cache hit rate** (`reuse` decisions / total step evaluations)
- **Partial refresh ratio** (`partial_refresh` / total refresh operations)
- **Full refresh ratio** (`full_refresh` / total refresh operations)
- **Contradiction rate** (selector/state conflicts per 100 steps)
- **Repair promotion success rate** (learned repairs promoted after validation)
- **Tier-0/Tier-1 resolution ratio** (share of steps resolved without high-context escalation)
- **No-progress token burn rate** (tokens spent in retries without state change)

Metric notes:
- Cache hit rate should be reported by mode (Manual/LLM/Hybrid).
- Refresh ratios should be split by invalidation reason.
- Contradiction rate should be split by class (`stale_locator`, `content_drift`, `structure_drift`).
- Tier resolution ratio should be split by `callPurpose` (`plan`, `classification`, `repair`, `review`).
- No-progress token burn rate should trigger alerts when retry clusters exceed policy budgets.

## Experiment Matrix

Run comparative tests for:
- Manual mode only.
- LLM-Orchestrated mode only.
- Hybrid mode with runtime toggles.
- With/without storage-state reuse.
- With/without learned repair memory.

Include forced-failure scenarios: iframe breakage, modal interruption, upload delays, selector drift.
Include repeated-step scenarios on stable pages to verify no redundant context recapture.

## Instrumentation Requirements

- Capture both tokenizer-based preflight token estimates and provider-reported actual usage.
- Emit usage telemetry at step and run granularity:
  - provider,
  - model,
  - call count,
  - input/output tokens,
  - cache read/write tokens (if available),
  - estimated/actual cost.
- Emit context-efficiency telemetry:
  - `callPurpose`,
  - `contextTier`,
  - escalation path (`tier0->tier1->tier2->tier3`),
  - no-progress retry counters.
- Support mixed provider experiments:
  - OpenAI,
  - Anthropic,
  - OpenAI-compatible endpoints such as LM Studio.
- Persist telemetry in machine-readable format for trend analysis.
- Emit cache decision telemetry at step granularity:
  - decision type (`reuse`, `partial_refresh`, `full_refresh`),
  - invalidation reason,
  - fingerprint match/mismatch state.

## Release Gates

Initial gate targets:
- >= 30% token reduction vs snapshot-heavy baseline.
- >= 50% token reduction vs full-context LLM-first baseline.
- >= 90% successful step recovery after intervention.
- <= 10% full-run restarts.
- No material drop in flow completion vs strongest baseline.
- >= 70% cache hit rate on repeated stable-page scenarios.
- <= 15% unnecessary full refreshes in repeated stable-page scenarios.
- >= 75% of planning/classification decisions resolved in Tier 0 or Tier 1 context.
- <= 10% of per-run token spend consumed by no-progress retry loops.

## Reporting Cadence

- Daily: smoke metrics and regression alerts.
- Weekly: trend review across reliability, cost, and recovery.
- Milestone gate: promote release only if KPI thresholds remain stable.

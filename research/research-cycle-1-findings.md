# Research Cycle 1: Token-Efficient Locator Copilot

## Scope
- Goal: identify practical patterns to build a low-token, high-control locator/action copilot.
- Focus areas: Playwright ecosystem, agent frameworks, cost controls, pause/fix/resume UX.

## Key findings from tools and docs
- Playwright codegen prioritizes robust locators (`role`, `text`, `testId`) and refines uniqueness.
- Playwright `page.pause()` enables custom setup + interactive recording controls in Inspector.
- Playwright MCP snapshots are much cheaper than screenshots and support mode controls.
- MCP snapshot modes matter: use incremental/default for low token; `none` for manual-only capture.
- Large snapshots can be redirected to files and searched selectively instead of full context injection.
- Testim-like flows validate the value of pause, step-by-step, run-from-here, and record-at-position UX.
- Playwright CLI emphasizes token efficiency by returning concise command outputs and file-backed snapshots.

## Competitive/architectural patterns observed
- Stagehand: hybrid accessibility + DOM context, deterministic execution, AI fallback, action caching.
- Skyvern: vision-heavy resilience for unknown sites; good adaptability but potentially higher token cost.
- Browser-use ecosystem discussions: node ranking, caching, and filtered tree extraction reduce token load.
- Emerging strategy trend: compact semantic tree + selective retrieval + fallback AI reasoning.

## Playwright CLI implementation cues (from docs/repo)
- Core idea: avoid pushing heavy page state into model context by default; expose concise commands.
- Snapshot strategy:
  - Snapshot links/artifacts instead of dumping full trees inline
  - On-demand snapshot command with `--depth` and element-scoped snapshot support
- Interaction strategy:
  - Prefer ref-based interactions from snapshots (`click e15`) to reduce ambiguity and retries
  - Allow semantic locator fallback (`getByRole`, `getByTestId`) when needed
- Pipeline strategy:
  - Command outputs can be stripped with `--raw` for scripting and diff-based workflows
  - Session persistence avoids repetitive auth/setup cost across actions

## Cost efficiency patterns (high confidence)
1. Deterministic first, LLM fallback only on failure/ambiguity.
2. Region-scoped extraction (user highlight or section target) before global page context.
3. Incremental deltas (only changed nodes/steps) instead of full recapture.
4. Snapshot/file indirection for huge outputs (store once, query slices).
5. Stable prompt prefix design + provider prompt caching for repeated orchestration context.

## Product implications for your vision
- Your strongest wedge is not "AI clicks buttons"; it is "interactive repair without restart."
- Keep execution state as a first-class object so pause/fix/resume is native, not a workaround.
- Separate outputs:
  - Runtime refs (short-lived interaction handles)
  - Portable locators (for reusable generated test code)
- Add confidence scoring and fallback locator chains per step.

## Recommended MVP architecture (v1)
- Intent parser: NL + highlights -> structured step graph.
- Step planner: classify each step (`click`, `fill`, `upload`, `assert`, `wait`, `navigate`).
- Selector engine (deterministic):
  - Prefer testId/role/label/text anchors
  - Restrict search to highlighted/identified container
  - Generate primary + fallback selectors with confidence
- Executor:
  - Run step
  - Capture minimal delta state
  - Persist artifacts/logs
- Recovery manager:
  - On failure -> pause UI
  - User fix manually or edit selector
  - Resume from same state
  - Save repair to knowledge cache
- Exporter:
  - Emit locator/action manifest for downstream code generation.

## Suggested metrics for next cycle
- Tokens per successful step
- Cost per completed flow
- First-pass step success rate
- Mean time to recover from failed step
- Restart rate (should approach zero with pause/fix/resume)
- Reuse rate of cached context and repaired selectors

## Risks to design early
- Ref lifetime invalidation after page change (must resnapshot deterministically)
- Mid-flow replay correctness when prior state dependencies exist
- Overfitting to text labels in dynamic UIs
- Latency trade-off when overusing manual snapshot-to-file search

## Sources (primary)
- Playwright CLI repo/readme: https://github.com/microsoft/playwright-cli
- Playwright codegen: https://playwright.dev/docs/codegen
- Playwright MCP snapshots: https://playwright.dev/mcp/snapshots
- Playwright MCP accessibility snapshots: https://microsoft-playwright-mcp.mintlify.app/concepts/accessibility-snapshots
- Playwright MCP config/options: https://playwright.dev/mcp/configuration/options
- Playwright MCP large snapshot issue: https://github.com/microsoft/playwright-mcp/issues/1329
- Testim pause/debug: https://help.testim.io/docs/stop-pause-debug-tests
- Testim record-at-position: https://help.testim.io/docs/recording-additional-steps-to-fix-bugs
- Stagehand overview: https://browserbase.com/stagehand
- Stagehand internals: https://mintlify.com/browserbase/stagehand/concepts/how-stagehand-works
- Skyvern repo: https://github.com/Skyvern-AI/skyvern
- Anthropic prompt caching: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
- OpenAI prompt caching: https://platform.openai.com/docs/guides/prompt-caching

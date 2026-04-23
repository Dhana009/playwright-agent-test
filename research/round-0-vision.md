# Token-Efficient Locator Copilot: Vision and Intent

## Why this exists
- Current browser automation + LLM workflows are expensive in tokens.
- Tools often read full DOM/accessibility trees even for tiny actions.
- Failures usually force re-generation instead of controlled recovery.

## Core problem
- Teams need robust locators and step plans without repeated full-page context capture.
- Existing flows lack in-browser pause/fix/resume control during execution.

## Product intent
- Build a human-in-the-loop locator and action copilot for browser automation.
- Accept natural language instructions and optional highlighted UI regions.
- Convert intent into ordered, executable automation steps.

## Input examples
- "In upload section, click Upload, attach this file path, click Proceed."
- "Verify Processing text appears, then continue to next page."
- "Use these three highlighted elements and generate stable locators."

## Operating model (deterministic-first)
1. Parse user intent into structured steps and validation checkpoints.
2. Try manual/deterministic selector discovery first (cheap path).
3. Reuse previously captured context; avoid duplicate extraction.
4. Escalate to LLM reasoning only when deterministic logic is blocked.
5. Return validated locators + step metadata for downstream code generation.

## Human control loop (major differentiator)
- Record full flow state while executing.
- Allow pause at any failing step.
- Let user manually fix/intervene in-browser.
- Resume from the same state without restarting entire flow.
- Persist successful repair as reusable knowledge.

## Output contract
- Locator list per step (primary + fallback selectors).
- Action/assertion metadata (click, fill, upload, verify, wait, navigate).
- Confidence and failure-recovery hints.
- Format ready for test-code generation (POM/framework adapters).

## Success criteria
- Lower token cost per completed step.
- Higher first-pass locator success rate.
- Faster recovery from broken selectors.
- Less rework after UI changes.

## Research plan
- Round 1: map current tools and architectural patterns.
- Round 2: analyze token-saving strategies in agent/browser stacks.
- Round 3: benchmark pause/fix/resume interaction models.
- Round 4: define MVP architecture, metrics, and experiment plan.

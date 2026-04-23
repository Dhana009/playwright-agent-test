# Documentation Quality Checklist

Use this checklist before finalizing any document.

## Discovery and Consistency

- [ ] Doc uses canonical mode model: Manual, LLM-Orchestrated, Hybrid.
- [ ] Terms are consistent with `SKILL.md` (no conflicting synonyms).
- [ ] Claims are specific and implementation-oriented.
- [ ] Unknowns are marked as **Open Question**.

## Scope Integrity

- [ ] Doc states this is not locator-only; includes full flow intelligence.
- [ ] Pause/fix/resume without restart is explicitly defined.
- [ ] Mid-run LLM toggle behavior is documented (ON/OFF without state reset).
- [ ] Auth/storage state strategy is covered where relevant.

## Technical Coverage

- [ ] Wait/timeout conditions are addressed.
- [ ] Assertions (URL/title/element/text) are addressed.
- [ ] iframe and modal/dialog handling are addressed.
- [ ] Upload and long-processing cases are addressed.
- [ ] Recovery and retry policy is explicit.

## Structure and Readability

- [ ] Document follows the matching template from `DOC_TEMPLATES.md`.
- [ ] Sections are short and scannable.
- [ ] Requirements are testable and measurable.
- [ ] Acceptance criteria are included.
- [ ] Out-of-scope section is included.

## Traceability

- [ ] Reuse from Hermes-Agent is explicitly listed.
- [ ] Reuse from Playwright CLI is explicitly listed.
- [ ] Reuse from `playwright-repo-test` is explicitly listed.
- [ ] Net-new components are clearly identified.

## Release Readiness

- [ ] Security/guardrail implications are captured.
- [ ] Benchmark/KPI implications are captured.
- [ ] Any blocker decisions are logged in decisions doc.

---
name: doc-system-authoring
description: Create and maintain consistent product and technical documentation for the Playwright agent initiative. Use when drafting PRDs, architecture docs, requirements, roadmap, benchmark plans, security guardrails, and decision logs; enforce shared structure, terminology, and traceability to Hermes-Agent, Playwright CLI, and playwright-repo-test.
---

# Documentation System Authoring

## Purpose

Produce clear, consistent, implementation-ready documentation for this program:
- Reuse Hermes-Agent intelligence and learning loops.
- Reuse Playwright CLI command/session model.
- Reuse `playwright-repo-test` proven recorder/replay/healing patterns.

Do not write ad-hoc docs with custom structure. Use the templates and checklist in this skill.

## Canonical Product Model

Always model the system with three runtime modes:
1. **Manual Mode**: deterministic/manual recorder + replay workflow, no LLM required.
2. **LLM-Orchestrated Mode**: Hermes-powered LLM plans and orchestrates end-to-end execution.
3. **Hybrid Mode**: user can toggle LLM on/off at any step without resetting session state.

Always state: LLM is the orchestrator in LLM mode, not only a fallback.

## Mandatory Scope Statements

Every core doc must explicitly include:
- This is not locator-only; it is full Playwright flow intelligence.
- Pause/fix/resume from current state (not restart-from-zero).
- Real Playwright constructs are in scope: waits, assertions, URL/title checks, uploads, dropdowns, iframes, modal/dialog handling, auth/storage state reuse.
- Export contract is portable and consumable by downstream codegen/framework adapters.

## Standard Terminology (use consistently)

Use these exact terms across docs:
- **Step Graph**
- **Execution Engine**
- **Checkpoint**
- **Event Log**
- **Intervention Event**
- **Recovery Policy**
- **Locator Bundle** (primary + fallback + confidence)
- **Storage State Strategy**
- **Mode Switch** (Manual/LLM/Hybrid)

Avoid mixing synonyms for the same concept in a single document.

## Documentation Workflow

For each document:
1. Identify doc type and apply the matching template in `DOC_TEMPLATES.md`.
2. Populate required sections only; avoid optional fluff.
3. Add explicit assumptions and out-of-scope.
4. Add traceability section:
   - what comes from Hermes-Agent,
   - what comes from Playwright CLI,
   - what comes from `playwright-repo-test`,
   - what is net-new.
5. Run the checklist in `QUALITY_CHECKLIST.md` before finalizing.

## Required Document Set

Maintain this baseline set:
- `01-vision-and-scope.md`
- `02-system-composition.md`
- `03-functional-requirements.md`
- `04-execution-and-state-model.md`
- `05-locator-and-healing-strategy.md`
- `06-playwright-coverage-matrix.md`
- `07-security-and-guardrails.md`
- `08-benchmark-and-kpis.md`
- `09-roadmap-and-release-criteria.md`
- `10-open-questions-and-decisions.md`

## Content Rules

- Prefer concrete requirements over narrative.
- Each requirement must be testable.
- Keep sections short and scannable.
- Include edge cases and failure behavior, not just happy path.
- If a behavior is mode-dependent, specify behavior for all three modes.
- If uncertain, label as **Open Question** instead of guessing.

## Non-Negotiable Edge Cases

Ensure docs cover at least:
- iframe context switching
- modal/dialog listeners and recovery
- dynamic dropdown behavior
- upload + long-processing waits
- ref invalidation after page changes
- auth expiry and storage state reuse
- mid-run LLM toggle without state loss

## Supporting Files

- Templates: `DOC_TEMPLATES.md`
- Final validation: `QUALITY_CHECKLIST.md`

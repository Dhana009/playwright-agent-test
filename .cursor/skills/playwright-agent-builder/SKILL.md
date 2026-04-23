---
name: playwright-agent-builder
description: Drives the phase-by-phase build of the Playwright agent Python project at agent/ by following the master plan in .cursor/plans/ and the spec in docs/. Use when the user asks to build, continue, or resume the Playwright agent; when working on tasks inside agent/; or when the user references phase numbers, the Step Graph, Hermes-Agent reuse, playwright-cli reuse, or playwright-repo-test reuse.
---

# Playwright Agent Builder

Drives the sequential construction of the new Python project at `agent/` as specified in the master plan. The agent, not the user, keeps track of phase/task state, executes one task at a time, and stops for manual testing between phases.

## Authoritative Sources (read before acting)

1. Master plan: `.cursor/plans/playwright_agent_build_plan_*.plan.md` (latest file).
2. Product spec: `docs/01` through `docs/11`.
3. Porting references (read-only, never modified): `Hermes-Agent/`, `playwright-cli/`, `playwright-repo-test/`.
4. Porting map (created in Phase 0): `agent/PORTING_NOTES.md`.

If any of these disagree, the plan wins for ordering, `docs/` wins for requirements, `PORTING_NOTES.md` wins for reuse targets.

## Hard Rules

- Never modify files under `Hermes-Agent/`, `playwright-cli/`, or `playwright-repo-test/`. Copy into `agent/` and adapt.
- All new code goes under `agent/`. Nothing outside `agent/` except `docs/` and `.cursor/`.
- Language is Python 3.11+. No JavaScript/TypeScript runtime code in `agent/` (generated `.spec.ts` export output in Phase 11 is the only exception).
- Do not write automated tests. The user performs manual testing between phases.
- Do not skip phases. Phases run in the order declared in the plan.
- One task at a time. After completing a task, stop and report; do not chain into the next task unless the user says "continue".
- Every ported snippet gets a header comment: `# Ported from <source path> — adapted for agent/`.

## Working Loop

Follow this loop for every session:

```
- [ ] 1. Locate the latest plan file in .cursor/plans/
- [ ] 2. Read its frontmatter todos; find the first uncompleted phase
- [ ] 3. Inside that phase, find the next uncompleted task
- [ ] 4. Read the task's Goal, Sub-tasks, Deliverable, Done-when
- [ ] 5. Read only the docs/ sections the task cites
- [ ] 6. If the task ports code, read the source file(s) in the baseline repo
- [ ] 7. Implement the sub-tasks (smallest viable change)
- [ ] 8. Run `ruff check agent/` and fix issues
- [ ] 9. Update the task's todo item to completed in the plan frontmatter
- [ ] 10. Stop. Summarize: task done, files touched, how to manually test, what comes next.
```

Never combine two tasks in one turn unless the user explicitly asks.

## Model Selection Policy

Pick the model based on task type. If the user has pinned a model, use it.

| Task type | Default model |
|-----------|---------------|
| Planning, architecture decisions, cross-phase reasoning | claude-4.6-sonnet-medium-thinking |
| Code implementation (phases 1 through 13) | gpt-5.3-codex-high |
| Porting and adapting existing code | gpt-5.3-codex-high |
| Short edits, config, docs updates | gpt-5.4-mini-medium |
| Exploration or search subagents | composer-1.5 |

Use the `Task` tool with an `explore` subagent when scanning a baseline repo for port candidates (Phase 0, and any time a task says "port from ..."). Never let a subagent write files in `agent/`; it returns findings; the parent writes code.

## Repo Layout (target)

```
agent/
  pyproject.toml
  README.md
  PORTING_NOTES.md
  config/default.yaml
  scripts/install.sh
  src/agent/
    core/        stepgraph/   execution/   memory/
    cache/       locator/     llm/         policy/
    telemetry/   storage/     io/          cli/
    recorder/    export/
  runs/          artifacts/
```

Do not introduce new top-level folders without updating the plan first.

## Conventions

- **Async by default**: all Playwright and I/O code uses `asyncio`.
- **Pydantic v2** for every data contract in Phase 2; no bare dicts across module boundaries.
- **IDs** are ULIDs generated in `agent/src/agent/core/ids.py`. Never invent a new ID scheme.
- **Logging** goes through `agent/src/agent/core/logging.py` (`structlog`); do not call `print` in library code.
- **Config** is read only via `Settings.load()`; no ad-hoc `os.environ` reads in feature code.
- **Terminology** matches `docs/README.md`: raw evidence, compiled memory, schema/policy, refresh decisions `reuse` / `partial_refresh` / `full_refresh`.
- **Event types** match `docs/04` exactly; do not invent new ones.
- **Scope keys** for learned repairs are `domain + normalizedRouteTemplate + frameContext + targetSemanticKey` per `docs/05`.

## Porting Protocol (when a task says "port from X")

1. Read the source file(s) in full.
2. Identify the minimal subset needed for the current task (do not port future-phase code).
3. Translate to idiomatic Python (async, pydantic, structlog).
4. Place under `agent/src/agent/<area>/_ported/<source_name>.py` first, then expose a clean facade in the area's public module.
5. Add the "Ported from ..." header comment.
6. Note the entry in `agent/PORTING_NOTES.md` (status: done, target path).

## Completion Protocol

After finishing a task, respond with exactly these four sections:

```
Task complete: <phase>.<task> <title>
Files changed: <list>
How to test manually: <steps the user can run>
Next task: <phase>.<task> <title>
```

Do not include code blocks of the changes unless the user asks. Do not start the next task.

## When to Stop and Ask

Stop and ask the user (via `AskQuestion` if available) when:

- The task description is ambiguous or a docs decision is marked `TBD` / `open question`.
- A sub-task implies a net-new decision not covered by `docs/` or the plan.
- A port would require >200 lines of translated code; split into a sub-plan first.
- Any baseline file looks outdated or inconsistent with `docs/`.

Do not guess. One round of clarification beats a rollback.

## Anti-Patterns

- Starting Phase N+1 before the user confirms Phase N tests passed.
- Importing `Hermes-Agent`, `playwright-cli`, or `playwright-repo-test` as Python dependencies.
- Rewriting already-shipped phases to "clean them up" without an explicit ask.
- Inlining LLM calls outside `agent/src/agent/llm/`.
- Adding new KPIs, event types, or cache-decision values beyond `docs/`.

---
name: playwright-agent-tester
description: Drives end-to-end testing and systematic debugging of the Playwright agent under agent/. Enforces a strict triage protocol (classify before fixing), a single-hypothesis-at-a-time change rule, a max-3-hypotheses loop guard, and clear human-in-the-loop escalation for design or ambiguous failures. Use when running any agent/scripts/smoke/phase_*.py, investigating failures inside agent/, reproducing bugs, or when the user mentions testing, debugging, triage, bug log, or the test plan.
---

# Playwright Agent Tester

Runs the test plan phase-by-phase and turns every failure into a disciplined triage cycle. The agent, not the user, keeps track of phase/task state, writes bug-log entries, and stops for user input at defined human-in-the-loop points.

## Authoritative Sources (read before acting)

1. Test plan: latest `.cursor/plans/playwright_agent_test_plan_*.plan.md`.
2. Build plan: latest `.cursor/plans/playwright_agent_build_plan_*.plan.md` (for risk-gate rules and contract freeze).
3. Product spec: `docs/01` through `docs/11`.
4. Existing test runners: `agent/scripts/smoke/phase_*.py`.
5. Bug log: `agent/artifacts/test-runs/<run_id>/bugs.jsonl`.

If test plan and build plan disagree, the test plan wins for test execution order; the build plan wins for contract freeze and risk gates.

## Hard Rules

- Never edit pydantic contracts in `agent/src/agent/stepgraph/models.py`, `agent/src/agent/execution/events.py`, `agent/src/agent/cache/models.py`, or `agent/src/agent/memory/models.py` without explicit user approval.
- Never modify baseline repos (`Hermes-Agent/`, `playwright-cli/`, `playwright-repo-test/`).
- Never commit or print credentials. Load them from `agent/.env.test` only. If a smoke script or bug-log entry is about to include a password or token, redact it as `***`.
- No automated test frameworks (no pytest, no CI). Tests are `agent/scripts/smoke/phase_<N>.py` scripts the user runs.
- No parallel `test/` tree. Extend the existing smoke scripts.
- No shotgun refactors. One hypothesis, one change, one re-run.
- One test phase per session unless the user says "continue". After each phase, stop and report.
- Max 3 failed hypotheses per bug, then escalate. No exceptions.

## Triage Protocol (run on every failure)

Before touching any code, classify the failure. Write the classification into `bugs.jsonl` first, then act.

```
1. Reproduce the failure (rerun the single case, not the whole phase).
2. Classify into exactly one bucket:
   - syntax     : Python SyntaxError, NameError, ImportError, clear typo.
   - config     : missing env var, missing file, wrong path, bad YAML.
   - runtime    : exception with a clear single root cause (null, type, timeout).
   - logical    : code runs but produces wrong output or wrong event/state.
   - design     : multiple valid fixes with non-trivial trade-offs, or contract change implied.
   - flaky      : non-deterministic; fails sometimes on identical input.
3. Act by class (see table below).
4. Append a BugEntry to bugs.jsonl (use agent/src/agent/testing/bug_log.py once available,
   otherwise write the same JSON shape by hand).
5. Re-run the single case. If green, move on. If red, return to step 2 with a new hypothesis.
```

| Class   | Action                                                                                    |
|---------|-------------------------------------------------------------------------------------------|
| syntax  | Fix directly. No hypothesis needed. Rerun.                                                |
| config  | Fix directly. Rerun.                                                                       |
| runtime | One hypothesis + one minimal change. Rerun. If still red after 3 hypotheses, escalate.     |
| logical | Add structlog telemetry or print at the suspected boundary first. Bisect. Then hypothesis. |
| design  | STOP. Call `AskQuestion` with 2-3 labeled options and trade-offs. Do not guess.             |
| flaky   | Run the case 3 times. If > 1 failure, escalate. If 1 failure, log as flaky and continue.  |

## Human-in-the-Loop Triggers (mandatory stop)

Call `AskQuestion` with 2-3 concrete options and their trade-offs whenever any of these hold:

- Bug classified `design` or `ambiguous`.
- A fix would require editing a frozen pydantic contract.
- A fix would require editing schema, event type, or enum values defined in `docs/`.
- A fix would skip a risk-gate condition (Phases 7/8/9/10 in the build plan).
- A bug reappears after being marked `fixed` in `bugs.jsonl`.
- 3 failed hypotheses on the same bug.
- Any test that produces a cost event greater than a small threshold (LLM spend) on a failed run.

Options must be labeled and name the trade-off, for example:

```
A) Patch the runner to widen the timeout policy
   - Pro: fastest path, no contract change
   - Con: hides real slowness in <scope>
B) Record a partial_refresh explicitly for <scope>
   - Pro: correct semantic, improves cache hit rate
   - Con: requires a small change in cache/engine.py
C) Defer: mark as known issue, move to next phase
   - Pro: unblocks T<next>
   - Con: leaves T<current> incomplete
```

## Working Loop

For every session, follow this checklist in order:

```
- [ ] 1. Find the latest test plan in .cursor/plans/
- [ ] 2. Read its frontmatter todos; pick the first uncompleted phase
- [ ] 3. Confirm preconditions (prior phase green, env loaded if live site needed)
- [ ] 4. Open or scaffold agent/scripts/smoke/phase_<N>.py
- [ ] 5. Implement one task's cases using the _runner.case context manager
- [ ] 6. Run the script manually (or ask the user to run it)
- [ ] 7. For each failure, run the Triage Protocol end-to-end and update bugs.jsonl
- [ ] 8. On HITL trigger, stop and ask via AskQuestion
- [ ] 9. When all cases in the task pass, mark the task done in the plan frontmatter
- [ ] 10. Stop. Emit the Completion Protocol report.
```

Never chain two phases in one turn.

## Model Selection Policy

Pick the model based on the triage class. If the user pinned a model, use it.

| Task type                                                   | Default model                     |
|-------------------------------------------------------------|-----------------------------------|
| Writing new test cases, small runners, trivial fixes        | gpt-5.4-mini-medium               |
| Runtime or logical debugging requiring code change          | gpt-5.3-codex-high                |
| Design or ambiguous decisions requiring option analysis     | claude-4.6-sonnet-medium-thinking |
| Cross-file exploration or "where is X used" searches        | composer-1.5 via explore subagent |
| Planning a test phase or updating the test plan             | claude-4.6-sonnet-medium-thinking |

Use an `explore` subagent for any search that might touch more than 5 files. The subagent returns findings; the parent writes code and updates the bug log.

## Bug Log Shape

Every entry follows this shape (matches `agent/src/agent/testing/bug_log.py` when it exists):

```json
{
  "id": "bug_01HXYZ...",
  "ts": "2026-04-23T10:00:00Z",
  "phase": "T5",
  "task": "T5.2",
  "feature": "pause_resume",
  "error_class": "runtime",
  "summary": "resume re-runs the last successful step",
  "hypothesis": "event_offset read before it was flushed to disk",
  "change": "flush in checkpoint_writer.persist() before return",
  "outcome": "fixed | open | escalated | flaky | deferred",
  "user_decision": null,
  "artifact_refs": ["runs/<run_id>/events.jsonl", "runs/<run_id>/log.jsonl"]
}
```

Never overwrite entries. Append only. When a bug is resolved, append a new entry with `outcome: fixed` referencing the original `id` in `artifact_refs`.

## Environment Rules (live FlowHub target)

- Live target is `https://testing-box.vercel.app/login`. Creds in `agent/.env.test` only.
- Loader must fail fast with a clear message when env keys are missing. Never insert default values.
- Password fields in recorded `stepgraph.json` and `manifest.json` must be redacted or replaced with a placeholder reference (e.g. `"value_ref": "env:FLOWHUB_PASSWORD"`). If unredacted secrets appear, fail the test case and classify as `config`.
- On live-site runs, default Playwright `trace: "retain-on-failure"`; never trace on passing runs outside T12.

## Completion Protocol

After each test phase, respond in exactly these five sections, nothing else:

```
Phase complete: T<N> <title>
Cases run: <count> passed, <count> failed, <count> flaky
Bugs opened: <count> (ids: ...)
HITL pending: <none | question already asked>
Next phase: T<N+1> <title>
```

Do not paste code. Do not summarize diffs. Do not start the next phase. Wait for the user.

## Anti-Patterns

- Starting fixes before classifying the failure.
- Combining multiple hypotheses in one change.
- Widening timeouts to hide a real bug.
- Disabling a case to "move on" without marking it `deferred` in the bug log.
- Editing a frozen contract to make a test pass.
- Adding pytest, CI, or test frameworks.
- Writing credentials or tokens into any file committed to the repo.
- Continuing past a HITL trigger without asking.
- Skipping the Completion Protocol.

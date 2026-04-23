# Reference Sources

This document tracks external references that informed architecture, requirements, and policy decisions.

## User-Shared Primary References

### Core concepts (persistent compiled memory)
- Karpathy LLM Wiki gist: <https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f>
  - Influence: persistent knowledge layer, incremental maintenance, index/log pattern, linting concept.
  - Classification: **Informative** (conceptual framing).

### Code graph and compounding context tools
- code-review-graph: <https://github.com/Dhana009/code-review-graph>
  - Influence: compounding context, incremental updates, graph-backed retrieval, token-reduction framing.
  - Classification: **Informative** (pattern and architecture inspiration).

- graphify: <https://github.com/safishamsi/graphify>
  - Influence: persistent graph artifact, cache/update workflow, queryable compiled layer, source-vs-synthesis separation.
  - Classification: **Informative** (pattern and workflow inspiration).

## Program Baseline References (Implementation Reuse)

- Hermes-Agent (workspace local implementation context)
  - Influence: orchestration loop, memory/self-learning primitives, LLM decision layer.
  - Classification: **Normative** (direct reuse target).

- Playwright CLI: <https://github.com/microsoft/playwright-cli>
  - Influence: command/session model, snapshot/ref interaction, token-efficient operational patterns.
  - Classification: **Normative** (direct reuse target).

- playwright-repo-test (workspace local project baseline)
  - Influence: recorder/replay/heal mechanics, practical flow patterns, intervention behavior.
  - Classification: **Normative** (direct reuse target).

## Research Cycle References (External)

These are documented in detail in `research/research-cycle-1-findings.md` and follow-on cycle files.

- Playwright docs: codegen, trace viewer, MCP snapshots/config/security.
- Testim docs: pause/debug/record-at-position workflows.
- Stagehand and Skyvern architecture references.
- Prompt caching references (Anthropic/OpenAI).
- OWASP and MCP security best-practice references.

## Usage Rule

When a source materially influences a design decision, add:
- source URL,
- influenced document(s),
- influence type (normative/informative),
- short rationale.

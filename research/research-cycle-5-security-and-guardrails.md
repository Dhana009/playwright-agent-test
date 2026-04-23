# Research Cycle 5: Security and Guardrails

## Objective
- Define security controls for browser agents that process untrusted web content and can invoke high-impact tools.

## Threat model highlights
- Indirect prompt injection from web pages/documents.
- Tool abuse (exfiltration, destructive operations, privilege escalation).
- Session/context poisoning across long runs.
- Unsafe file access/upload behavior.

## High-priority controls
- Treat all external content as untrusted:
  - sanitize and boundary-mark before LLM consumption.
- Least privilege by default:
  - separate read-only vs write-capable tools.
  - scope tools per task/session.
- Human approval gates:
  - required for irreversible/high-impact actions.
- Strict tool parameter validation:
  - schema checks, allow-lists, path/domain restrictions, timeouts.
- Comprehensive audit:
  - log every tool invocation, approval decision, and state transition.

## File and browser safety controls
- Keep restricted file access as default.
- Never enable unrestricted file access in untrusted environments.
- Block `file://` navigation unless explicitly needed and approved.
- Enforce path traversal protections for any upload-related tools.
- Domain allow-list for external navigation and submission endpoints.

## Prompt-injection resilience model
- Prompt structure:
  - hard separation between system instructions and external data.
- Content defenses:
  - strip hidden/invisible and control-sequence payloads.
- Runtime defenses:
  - anomaly detection for suspicious action sequences.
  - circuit breaker on abnormal rejection or tool-call spikes.
- Recovery:
  - immediate pause + human review on suspected hijack patterns.

## Approval policy framework
- Classify every action by:
  - reversibility
  - impact
  - confidence
- Policy outcome:
  - auto-allow (low risk)
  - review (medium risk)
  - hard-approval required (high risk)
- Track approval fatigue:
  - auto-promote consistently safe patterns to reduce reviewer overload.

## MCP/security-specific implications
- Consider MCP server/tool metadata untrusted unless pinned and verified.
- Add consent UI for sensitive calls with full parameter preview.
- Implement TOFU/pinning for trusted servers where possible.
- Use short-lived credentials and rotate keys regularly.

## Security success criteria
- Zero unapproved high-risk side effects in benchmark runs.
- 100% auditability of tool decisions.
- Controlled false-positive rate for security interrupts.
- Demonstrable containment in injected-content test scenarios.

## Sources used in this cycle
- OWASP prompt injection and AI agent security cheat sheets
- MCP security best-practice references and tool safety docs
- Playwright MCP security/file-access guidance
- HITL approval gate operational design references

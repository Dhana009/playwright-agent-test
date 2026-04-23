# Security and Guardrails

## Threat Model

Primary risks:
- Prompt injection via untrusted page content.
- Unsafe tool invocation or parameter abuse.
- File path and data exfiltration risks.
- Session poisoning across long-running runs.

## Approval Policy

Every action is classified by impact and reversibility:
- **Auto-Allow**: low-risk, reversible actions.
- **Review**: medium-risk actions.
- **Hard Approval**: high-risk or irreversible actions.

High-risk actions (submit/delete/external post/auth mutations) require explicit operator approval.

Default Hard Approval action set (v1):
- Final form submit actions that commit server-side state.
- Destructive mutations (delete, irreversible update, bulk destructive operations).
- External post/send actions (email, webhook, third-party publish/dispatch).
- Auth and permission mutations (role changes, credential/session mutations).
- Local file uploads from user machine paths.

## File and Domain Restrictions

- Restrict file uploads to approved roots by default.
- Block unrestricted `file://` flows unless explicitly enabled.
- Enforce domain allowlist/denylist for navigation and submissions.
- Validate and normalize all file path inputs to prevent traversal issues.

## Tool Permission Boundaries

- Separate read-only capabilities from write-capable capabilities.
- Restrict tool scopes per run and per mode.
- Log all tool calls with actor, parameters, and decision path.

## Audit and Forensics

Audit log must capture:
- mode switches,
- approvals/rejections,
- tool invocations,
- intervention events,
- retries and failure classifications,
- checkpoint resume points.

All critical run decisions must be reconstructable from logs and artifacts.

## Unsafe Mode Policy

- Unsafe mode is explicitly opt-in and must show clear warning.
- Unsafe mode cannot be default.
- Unsafe mode usage is always logged and surfaced in run summary.

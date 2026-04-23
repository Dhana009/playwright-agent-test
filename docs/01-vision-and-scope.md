# Vision and Scope

## Problem

Teams lose time and reliability when browser automation requires repeated manual locator work, reruns from step zero, and fragile handling of waits, assertions, dialogs, frames, and auth state.

## Product Goal

Build a Playwright-native execution control plane that:
- converts user intent and operator actions into a robust Step Graph,
- executes with pause/fix/resume continuity,
- applies cache-first execution with explicit `reuse` / `partial_refresh` / `full_refresh` decisions,
- supports full Playwright behavior coverage for practical E2E flows,
- and produces portable artifacts for downstream code generation.

## Non-Goals

- Building a full no-code platform replacement in v1.
- Replacing Playwright core runtime with a custom browser engine.
- Prioritizing vision-first autonomy over deterministic execution and controllability.

## Operating Modes (Manual / LLM / Hybrid)

- **Manual Mode**: User drives recording/replay and deterministic repair without requiring LLM.
- **LLM-Orchestrated Mode**: Hermes-powered LLM is the decision-maker for planning, execution sequencing, and recovery.
- **Hybrid Mode**: User can turn LLM assistance on/off at any step without resetting run state.

## Success Metrics

- Token reduction per successful step and per completed flow.
- Higher flow completion with lower restart rate.
- Faster MTTR for failed steps with resume-from-checkpoint behavior.
- High reuse of learned locator repairs and cached context.
- Low contradiction drift via explicit conflict detection and resolver policy.

## Assumptions

- Hermes-Agent is reused for agent orchestration, memory, and learning loops.
- Playwright CLI is reused for proven command/session interaction patterns.
- `playwright-repo-test` provides practical baseline logic for record/replay/heal workflows.
- Runtime state follows three memory layers: raw evidence, compiled memory, and schema/policy.

## Out of Scope

- Autonomous multi-app orchestration in v1.
- Full enterprise governance features (SSO/RBAC/compliance workflows) in v1.
- Guaranteed Shadow DOM parity in v1 unless required by target applications.

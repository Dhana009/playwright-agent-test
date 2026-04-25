# Vision and Scope

## The Problem

Browser automation breaks at the wrong time and in the wrong way. Locators that work in DevTools fail in Playwright. Recordings drift when the app changes. When a step fails mid-flow you restart from scratch, losing all the work done before the failure. There is no way to pause at the failure, fix just that step, and continue.

## North Star

> Launch a browser. A panel rides along in the viewport corner. Pick any element, choose an action or assertion from a palette, validate it live, and record the step. When you're done recording, replay. If a step fails, the panel pauses at that step — not at step zero — and shows exactly what broke. Fix or force-fix that one step, then resume from where you left off.

## Product Goal

Build a Playwright-native recording and replay environment where:

- The **browser is the authoring surface** — a lightweight panel injected into the page is the primary UI, not a separate app or terminal window.
- **Pause/fix/resume** is the default failure path, not restart-from-scratch.
- **Locator generation is deterministic-first** — test-id → aria → role → CSS → xpath — with a cascade fallback and LLM as the last resort only.
- **Recordings are versioned** — you can save a subset of a recording as a named version without destroying the original.
- **LLM earns its cost** in exactly two places: (1) **force-fix healing** when all deterministic strategies fail, and (2) **intent compression** — describe a multi-step goal in English and get a draft recording to edit.

## Operating Modes

### Manual Mode (primary)

Picker-driven. You click Pick, hover an element, click it, choose an action (click / fill / check / upload / select / assert-visible / assert-text / assert-url / wait-for / …), validate live, and the step is added to the recording. All Playwright action and assertion coverage is surfaced as picker actions — no code writing.

### Hybrid Mode (Manual + LLM as assist)

Manual mode with two optional LLM assists available:

- **Force-fix**: invoked automatically when the deterministic locator cascade is exhausted. LLM receives the frozen DOM snippet and failed locator, returns a repair or a "stuck because…" explanation.
- **Intent compression**: the user describes a multi-step goal in English; LLM emits a draft step graph the user then edits in Manual mode.

LLM can be toggled on/off at any step without losing browser or checkpoint state.

### What We Dropped

**Pure LLM-Orchestrated Mode** is not shipped in v1. It duplicated Manual mode at higher token cost and added a separate UI surface for no practical gain. LLM is a compliment to the picker experience, not a parallel authoring path.

## Non-Goals (v1)

- Building a full no-code platform.
- Replacing Playwright's core runtime.
- Chrome extension or Electron host (panel is injected overlay in a Playwright-launched Chromium).
- Autonomous multi-app orchestration.
- Enterprise governance features (SSO/RBAC/compliance).
- Guaranteed Shadow DOM parity unless required by the target app.

## Success Metrics

- A user can record a 5-step flow, deliberately break one locator, replay, pause at the failure, force-fix, and resume — all from the panel — in under 2 minutes.
- Restart-from-zero behavior is rare: >90% of single-step failures are recoverable via fix/force-fix without full replay restart.
- Token spend per force-fix is bounded: LLM is called at most once per step failure after all deterministic strategies are exhausted.
- Versioned recordings: a user can save a named subset without altering the original.

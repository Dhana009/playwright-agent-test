# Playwright Agent Porting Notes

This file tracks source-to-target reuse mapping from baseline repositories into the new `agent/` project.

Status legend:
- `planned`: identified for a future phase
- `done`: copied and adapted into `agent/`
- `rejected`: intentionally not ported

## Phase 0.1 - Hermes-Agent Inventory

Scope reviewed:
- `Hermes-Agent/agent/`
- `Hermes-Agent/tools/`
- `Hermes-Agent/toolsets.py`
- `Hermes-Agent/model_tools.py`
- `Hermes-Agent/run_agent.py`
- `Hermes-Agent/cli.py`

Note: `Hermes-Agent/hermes/` is not present in this workspace snapshot. Equivalent cross-cutting modules appear under `hermes_cli`, `hermes_constants`, and `hermes_logging`.

| source path | key symbol(s) | category | target path in `agent/` | action | status | rationale |
| --- | --- | --- | --- | --- | --- | --- |
| `Hermes-Agent/run_agent.py` | `AIAgent`, `run_conversation`, `IterationBudget` | orchestration loop entry | `agent/src/agent/llm/orchestrator.py` | adapt | planned | Core plan/act/observe loop and stop-budget semantics map to Phase 9 orchestrator. |
| `Hermes-Agent/run_agent.py` | `tool_start_callback`, `tool_complete_callback`, `step_callback`, `stream_delta_callback` | telemetry hooks | `agent/src/agent/telemetry/hooks.py` | adapt | planned | Reuse callback contract for tool lifecycle and streaming visibility. |
| `Hermes-Agent/model_tools.py` | `get_tool_definitions`, `handle_function_call`, `coerce_tool_args` | tool-calling abstraction | `agent/src/agent/llm/model_tools.py` | adapt | planned | Minimal adapter between LLM tool calls and local tool registry dispatch. |
| `Hermes-Agent/tools/registry.py` | `ToolRegistry`, `ToolEntry`, `register`, `dispatch` | tool-calling abstraction | `agent/src/agent/execution/tool_registry.py` | adapt | planned | Strong typed registry pattern for Phase 3 tool surface execution. |
| `Hermes-Agent/toolsets.py` | `TOOLSETS`, `resolve_toolset`, `validate_toolset` | tool-calling abstraction | `agent/src/agent/execution/toolsets.py` | adapt | planned | Provides deterministic grouped enablement by mode and policy. |
| `Hermes-Agent/agent/memory_provider.py` | `MemoryProvider` | memory primitives | `agent/src/agent/memory/provider.py` | adapt | planned | Abstract provider contract aligns with Phase 8 memory layering. |
| `Hermes-Agent/agent/memory_manager.py` | `MemoryManager`, context assembly methods | memory primitives | `agent/src/agent/memory/manager.py` | adapt | planned | Centralized memory aggregation pattern useful for staged context builder. |
| `Hermes-Agent/tools/memory_tool.py` | memory read/write actions | memory primitives | `agent/src/agent/memory/file_memory.py` | adapt | planned | Useful baseline for append/update semantics and safe mutation workflow. |
| `Hermes-Agent/agent/transports/base.py` | `ProviderTransport` | provider adapters | `agent/src/agent/llm/transports/base.py` | adapt | planned | Clean abstraction boundary for provider-specific message transforms. |
| `Hermes-Agent/agent/transports/types.py` | `NormalizedResponse`, `ToolCall`, `Usage` | provider adapters | `agent/src/agent/llm/transports/types.py` | adapt | planned | Shared normalized response types avoid provider coupling in orchestrator. |
| `Hermes-Agent/agent/transports/__init__.py` | transport registration and lookup | provider adapters | `agent/src/agent/llm/transports/__init__.py` | adapt | planned | Registry pattern for selecting OpenAI/Anthropic/compatible adapters. |
| `Hermes-Agent/agent/anthropic_adapter.py` | anthropic conversion helpers | provider adapters | `agent/src/agent/llm/providers/anthropic.py` | adapt | planned | Reusable request/response normalization approach for Phase 9 provider layer. |
| `Hermes-Agent/agent/usage_pricing.py` | usage normalization and cost estimation | telemetry hooks | `agent/src/agent/telemetry/usage.py` | adapt | planned | Supports KPI and run summary cost fields in Phase 2 and Phase 12. |
| `Hermes-Agent/agent/trajectory.py` | trajectory persistence helpers | telemetry hooks | `agent/src/agent/telemetry/trajectory.py` | adapt | planned | Useful optional debugging artifact pattern with low complexity. |
| `Hermes-Agent/cli.py` | top-level CLI entry design | orchestration loop entry | `agent/src/agent/cli/__main__.py` | reject | rejected | Hermes CLI is tightly coupled to Fire/TUI flows; this project uses Typer CLI per plan. |
| `Hermes-Agent/tools/browser_tool.py` | browser tools via external runtime | tool-calling abstraction | N/A | reject | rejected | Browser stack differs from Playwright-native execution in this project. |

### Task 0.1 Deliverable Check

- Goal met: reusable Hermes modules for orchestration, tools, memory, providers, telemetry were inventoried.
- Done-when check: every reuse candidate currently selected has a target `agent/` path and action.
- Next in phase: Task 0.2 inventory for `playwright-cli`.

## Phase 0.2 - Playwright CLI Inventory

Scope reviewed:
- `playwright-cli/playwright-cli.js` (end to end)

Observation:
- `playwright-cli.js` is a thin bootstrap wrapper that delegates to `playwright-core/lib/tools/cli-client/program`.
- Command definitions are therefore delegated, not authored directly in this repository file.

Shared output conventions for delegated commands:
- **Client command JSON mode**: structured objects such as `{ session, pid, result }`, `{ closed: [...] }`, or `{ status: "closed" }` depending on command.
- **Daemon tool command JSON mode**: pass-through structured result from the underlying browser tool.
- **Error envelope**: structured error message in JSON mode and non-zero exit.

### Delegated command surface (resolved for planned parity)

| js command | inputs | output shape | target Python function sketch | phase fit | status | notes |
| --- | --- | --- | --- | --- | --- | --- |
| `open` | `url?`, `--browser`, `--config`, `--headed`, `--persistent`, `--profile` | `{ session, pid, result }` | `open_session(...)` in `execution/browser.py` | 3a | planned | Session bootstrap and first navigation. |
| `close` | none | `{ session, status }` | `close_session()` in `execution/browser.py` | 3a | planned | Session lifecycle close. |
| `list` | `--all?` | `{ browsers, servers?, channelSessions? }` | `list_sessions(all: bool)` | 3b | planned | Operational inventory command. |
| `close-all` | none | `{ closed: string[] }` | `close_all_sessions()` | 3b | planned | Bulk lifecycle cleanup. |
| `delete-data` | none | `{ session, deleted }` | `delete_session_data()` | 3b | planned | Destructive local data cleanup. |
| `kill-all` | none | `{ killed, pids }` | `kill_all_daemons()` | 3b | planned | Process-level cleanup, OS-sensitive. |
| `attach` | target? plus CDP/endpoint flags | `{ session, pid, endpoint?, result }` | `attach_session(...)` | 3b | planned | Advanced attach path; not on critical path. |
| `install` | `--skills?` | `{ installed: true }` or install logs | `install_workspace(skills: str|None)` | 3b | planned | Workspace/bootstrap concern, not runner core. |
| `install-browser` | browser install args | install output | `install_browsers(...)` | 3b | planned | Environment setup command. |
| `show` (dashboard) | `--port`, `--host`, `--kill`, `--annotate` | `{ session, pid }` or text logs | `show_dashboard(...)` | 3b | planned | Dashboard/diagnostic UX, defer. |
| `goto` | `url` | daemon tool result | `navigate(url: str)` | 3a | planned | Core navigation. |
| `go-back` | none | daemon tool result | `navigate_back()` | 3a | planned | Core navigation. |
| `go-forward` | none | daemon tool result | `navigate_forward()` | 3b | planned | Defer until extended nav parity. |
| `reload` | none | daemon tool result | `reload_page()` | 3b | planned | Useful but not blocking Phase 5 loop. |
| `click` | `target`, `button?`, `--modifiers?` | daemon tool result | `click(target, button=None, modifiers=None)` | 3a | planned | Core interaction. |
| `dblclick` | `target`, `button?`, `--modifiers?` | daemon tool result | `double_click(target, ...)` | 3b | planned | Extended interaction. |
| `drag` | `startTarget`, `endTarget` | daemon tool result | `drag(start_target, end_target)` | 3b | planned | Extended interaction. |
| `drop` | `target`, `--path*`, `--data*` | daemon tool result | `drop(target, paths=None, data=None)` | 3b | planned | Extended interaction/data drop. |
| `fill` | `target`, `text`, `--submit?` | daemon tool result | `fill(target, value, submit=False)` | 3a | planned | Core form action. |
| `type` | `text`, `--submit?` | daemon tool result | `type(text, submit=False)` | 3a | planned | Core typing action. |
| `hover` | `target` | daemon tool result | `hover(target)` | 3b | planned | Deferred by updated plan split. |
| `select` | `target`, `value` | daemon tool result | `select(target, value)` | 3b | planned | Deferred by updated plan split. |
| `upload` | `file` | daemon tool result | `upload(path)` | 3b | planned | Deferred by updated plan split. |
| `check` | `target` | daemon tool result | `check(target)` | 3b | planned | Extended interaction. |
| `uncheck` | `target` | daemon tool result | `uncheck(target)` | 3b | planned | Extended interaction. |
| `snapshot` | `target?`, `--filename?`, `--depth?` | daemon tool result with snapshot data | `snapshot(target=None, depth=None)` | 3a | planned | Critical ref/snapshot primitive. |
| `eval` | `func`, `target?`, `--filename?` | daemon tool result | `evaluate(js_func, target=None)` | 3b | planned | Powerful but policy-sensitive. |
| `dialog-accept` | `prompt?` | daemon tool result | `dialog_handle(accept=True, prompt_text=None)` | 3a | planned | Core dialog control. |
| `dialog-dismiss` | none | daemon tool result | `dialog_handle(accept=False)` | 3a | planned | Core dialog control. |
| `resize` | `width`, `height` | daemon tool result | `resize(width, height)` | 3b | planned | Viewport utility. |
| `press` | `key` | daemon tool result | `press(key)` | 3a | planned | Core keyboard control. |
| `keydown` | `key` | daemon tool result | `key_down(key)` | 3b | planned | Low-level keyboard API. |
| `keyup` | `key` | daemon tool result | `key_up(key)` | 3b | planned | Low-level keyboard API. |
| `mousemove` | `x`, `y` | daemon tool result | `mouse_move_xy(x, y)` | 3b | planned | Coordinate-level control. |
| `mousedown` | `button?` | daemon tool result | `mouse_down(button="left")` | 3b | planned | Coordinate-level control. |
| `mouseup` | `button?` | daemon tool result | `mouse_up(button="left")` | 3b | planned | Coordinate-level control. |
| `mousewheel` | `dx`, `dy` | daemon tool result | `mouse_wheel(dx, dy)` | 3b | planned | Extended interaction. |
| `screenshot` | `target?`, `--filename?`, `--full-page?` | daemon tool result | `screenshot(target=None, filename=None, full_page=False)` | 3b | planned | Observability/debug artifact. |
| `pdf` | `--filename?` | daemon tool result | `save_pdf(filename=None)` | 3b | planned | Extended export utility. |
| `tab-list` | none | daemon tool result | `tabs_list()` | 3a | planned | Core tab management for multi-page flows. |
| `tab-new` | `url?` | daemon tool result | `tabs_new(url=None)` | 3a | planned | Core tab management. |
| `tab-close` | `index?` | daemon tool result | `tabs_close(index=None)` | 3a | planned | Core tab management. |
| `tab-select` | `index` | daemon tool result | `tabs_select(index)` | 3a | planned | Core tab management. |
| `state-save` | `filename?` | daemon tool result | `save_storage_state(filename=None)` | 3b | planned | Storage/auth utility. |
| `state-load` | `filename` | daemon tool result | `load_storage_state(filename)` | 3b | planned | Storage/auth utility. |
| `cookie-list` | `--domain?`, `--path?` | daemon tool result | `cookies_list(domain=None, path=None)` | 3b | planned | Extended state introspection. |
| `cookie-get` | `name` | daemon tool result | `cookie_get(name)` | 3b | planned | Extended state introspection. |
| `cookie-set` | `name`, `value`, cookie flags | daemon tool result | `cookie_set(name, value, **opts)` | 3b | planned | Extended state mutation. |
| `cookie-delete` | cookie args | daemon tool result | `cookie_delete(...)` | 3b | planned | Extended state mutation. |
| `cookie-clear` | none | daemon tool result | `cookie_clear()` | 3b | planned | Extended state mutation. |
| `localstorage-list` | none | daemon tool result | `local_storage_list()` | 3b | planned | Extended state tooling. |
| `localstorage-get` | `key` | daemon tool result | `local_storage_get(key)` | 3b | planned | Extended state tooling. |
| `localstorage-set` | `key`, `value` | daemon tool result | `local_storage_set(key, value)` | 3b | planned | Extended state tooling. |
| `localstorage-delete` | `key` | daemon tool result | `local_storage_delete(key)` | 3b | planned | Extended state tooling. |
| `localstorage-clear` | none | daemon tool result | `local_storage_clear()` | 3b | planned | Extended state tooling. |
| `sessionstorage-list` | none | daemon tool result | `session_storage_list()` | 3b | planned | Extended state tooling. |
| `sessionstorage-get` | `key` | daemon tool result | `session_storage_get(key)` | 3b | planned | Extended state tooling. |
| `sessionstorage-set` | `key`, `value` | daemon tool result | `session_storage_set(key, value)` | 3b | planned | Extended state tooling. |
| `sessionstorage-delete` | `key` | daemon tool result | `session_storage_delete(key)` | 3b | planned | Extended state tooling. |
| `sessionstorage-clear` | none | daemon tool result | `session_storage_clear()` | 3b | planned | Extended state tooling. |
| `route` | `pattern` with response/header flags | daemon tool result | `route_set(pattern, **opts)` | 3b | planned | Network mocking support, policy-sensitive. |
| `route-list` | none | daemon tool result | `route_list()` | 3b | planned | Network route diagnostics. |
| `unroute` | `pattern?` | daemon tool result | `route_remove(pattern=None)` | 3b | planned | Network route teardown. |
| `network-state-set` | `online` or `offline` | daemon tool result | `network_state_set(state)` | 3b | planned | Test-mode network simulation. |
| `console` | `--min-level?`, `--clear?` | daemon tool result | `console_messages(min_level=None)` / `console_clear()` | 3b | planned | Deferred observability tooling. |
| `network` | filters, include flags, `--clear?` | daemon tool result | `network_requests(...)` / `network_clear()` | 3b | planned | Deferred observability tooling. |
| `tracing-start` | none | daemon tool result | `trace_start()` | 3b | planned | Deferred tracing utility. |
| `tracing-stop` | none | daemon tool result | `trace_stop()` | 3b | planned | Deferred tracing utility. |
| `video-start` | `filename?`, `--size?` | daemon tool result | `video_start(filename=None, size=None)` | 3b | planned | Deferred media capture utility. |
| `video-stop` | none | daemon tool result | `video_stop()` | 3b | planned | Deferred media capture utility. |
| `video-chapter` | `title`, `--description?`, `--duration?` | daemon tool result | `video_chapter(title, description=None, duration=None)` | 3b | planned | Deferred media annotation utility. |
| `run-code` | `code?`, `--filename?` | daemon tool result | `run_code(code=None, filename=None)` | 3b | planned | Arbitrary code execution, high risk. |
| `generate-locator` | `target` | daemon tool result | `generate_locator(target)` | 3b | planned | Dev-assist utility. |
| `highlight` | `target?`, `--hide?`, `--style?` | daemon tool result | `highlight(target=None, hide=False, style=None)` | 3b | planned | Visual debug overlay support. |
| `pause-at` | `file:line` style location | daemon tool result | `pause_at(location)` | 3b | planned | Debugger-related flow. |
| `resume` | none | daemon tool result | `resume()` | 3b | planned | Debugger-related flow. |
| `step-over` | none | daemon tool result | `step_over()` | 3b | planned | Debugger-related flow. |
| `config-print` | none (hidden) | daemon tool result | `config_print()` | 3b | planned | Hidden diagnostics. |
| `tray` | none (hidden) | daemon behavior/output | `tray()` | 3b | planned | Platform-specific helper. |

### Task 0.2 Deliverable Check

- Goal met: delegated command inventory captured with input/output shape expectations.
- Done-when check: each delegated command now has a Python target function sketch and phase fit.
- Next in phase: Task 0.3 inventory for `playwright-repo-test`.

## Phase 0.3 - playwright-repo-test Inventory

Scope reviewed:
- `playwright-repo-test/recorder2.js`
- `playwright-repo-test/lib/record.js`
- `playwright-repo-test/lib/replay.js`
- `playwright-repo-test/lib/execute.js`
- `playwright-repo-test/lib/locator/*`
- `playwright-repo-test/lib/browser/*`
- `playwright-repo-test/lib/llm/*`
- `playwright-repo-test/lib/sessions.js`
- `playwright-repo-test/lib/codegen.js`
- `playwright-repo-test/lib/autoheal.js`

| source path | key symbol(s) | behavior area | target path in `agent/` | action | phase relevance | status | rationale |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `playwright-repo-test/lib/locator/candidates.js` | `buildCandidates` | locator candidate generation | `agent/src/agent/locator/_ported/candidates.py` -> `agent/src/agent/locator/engine.py` | adapt | 3a | planned | Deterministic candidate strategy pipeline maps directly to locator bundle generation. |
| `playwright-repo-test/lib/locator/find.js` | `findBestLocator` | locator resolution and force-fix fallback | `agent/src/agent/locator/_ported/find.py` -> `agent/src/agent/locator/engine.py` | adapt | 3a, 5 | planned | Multi-pass locator probing closely matches force-fix requirements in manual mode. |
| `playwright-repo-test/lib/execute.js` | `executeStep` | replay step execution primitive | `agent/src/agent/execution/_ported/execute.py` -> `agent/src/agent/execution/tools.py` | adapt | 3a, 5 | planned | Step-mode dispatch pattern is a strong baseline for Phase 3 tool calls and runner behavior. |
| `playwright-repo-test/lib/replay.js` | `doReplay` | replay orchestration loop | `agent/src/agent/execution/_ported/replay.py` -> `agent/src/agent/execution/runner.py` | adapt | 5 | planned | Contains robust step loop and command flow that can be reworked into checkpointed Step Graph execution. |
| `playwright-repo-test/lib/replay.js` | `replay:force`, `replay:manual` flows | force-fix and manual-fix control flow | `agent/src/agent/cli/fix_cmd.py` and `agent/src/agent/execution/runner.py` | adapt | 5 | planned | Existing force/manual branching maps to planned operator intervention workflow. |
| `playwright-repo-test/lib/state.js` | shared command/capture state | recorder/replay state coordination | `agent/src/agent/execution/session_state.py` | redesign | 5, 6 | planned | Global mutable JS state should become explicit typed runtime state in Python. |
| `playwright-repo-test/lib/browser/inject.js` | `injectCapture` | recorder capture bridge | `agent/src/agent/recorder/_ported/inject.py` -> `agent/src/agent/recorder/recorder.py` | adapt | 6 | planned | Binding-based capture model is reusable, but should be adapted to headless/CLI-first control. |
| `playwright-repo-test/lib/record.js` | `doRecord`, step shaping helpers | recorder action-to-step transformation | `agent/src/agent/recorder/_ported/record.py` -> `agent/src/agent/recorder/recorder.py` | adapt | 6 | planned | Core logic for converting observed interactions to replayable steps is the primary reuse target. |
| `playwright-repo-test/lib/panel-api.js` | panel update helpers | manual review/recovery UI bridge | `agent/src/agent/cli/recovery_ui.py` (CLI prompts + events) | redesign | 5, 6 | planned | Browser-overlay API is UI-specific; behavior should be translated to Typer-based prompts and logs. |
| `playwright-repo-test/lib/browser/panel-script.js` | `window.__rec*` UI functions | recorder/replay in-page UI | N/A | reject | 6+ | rejected | Large in-page JS panel is out of scope for Python runtime and should not be ported directly. |
| `playwright-repo-test/lib/llm/index.js` | `healLocator`, `handleCmd`, assist functions | LLM repair hook | `agent/src/agent/llm/_ported/repo_test_llm.py` -> `agent/src/agent/llm/orchestrator.py` | adapt | 9 | planned | Good reference for heal/review assist patterns once LLM layer is introduced. |
| `playwright-repo-test/lib/llm/config.js` | provider config helpers | LLM provider/config management | `agent/src/agent/llm/config.py` | adapt | 9 | planned | Reusable config pattern; adapt to pydantic Settings + project policy model. |
| `playwright-repo-test/lib/locator/heal.js` | `llmHealLocator` | locator-healing API seam | `agent/src/agent/llm/heal.py` | adapt | 9 | planned | Useful thin adapter boundary between locator system and LLM-assisted repair logic. |
| `playwright-repo-test/lib/codegen.js` | `buildTest` | generated spec writer | `agent/src/agent/export/_ported/codegen.py` -> `agent/src/agent/export/spec_writer.py` | adapt | 11 | done | Ported deterministic Playwright test generation patterns into a Python codegen adapter and an async writer facade that emits `*.spec.ts`. |
| `playwright-repo-test/lib/autoheal.js` | `autoHeal` | post-failure auto-heal pipeline | `agent/src/agent/memory/autoheal.py` (or `agent/src/agent/locator/autoheal.py`) | adapt | 7+ | planned | Valuable reference for later cache/memory-informed repair workflows. |
| `playwright-repo-test/lib/sessions.js` | `saveSteps`, `loadSteps`, `saveSession` | session persistence | `agent/src/agent/storage/repos/step_graph.py` and `agent/src/agent/storage/files.py` | adapt | 4, 6 | planned | Session persistence patterns can map to run folders + SQLite-backed metadata. |
| `playwright-repo-test/lib/review.js` | `doReview` | post-record review and pruning | `agent/src/agent/recorder/review.py` | adapt | 6 | planned | Helpful optional review flow for cleaning captured steps before replay. |
| `playwright-repo-test/recorder2.js` | main entry and wiring | end-to-end record/replay shell | `agent/src/agent/cli/record_cmd.py` and `agent/src/agent/cli/run_cmd.py` | redesign | 5, 6 | planned | Valuable orchestration reference, but JS CLI/runtime details must be rewritten for Typer + Python async. |

### JS-only/UI pieces to redesign in Python

- Browser-injected panel UI (`lib/browser/panel-script.js`) should be replaced by CLI prompts and structured event output.
- `panel-api` calls that manipulate `window.__rec*` should become console/Typer interaction and event-log entries.
- Readline-centric terminal shortcuts in recorder/replay modules should be replaced with explicit CLI subcommands and prompts.

### Gaps and ambiguities captured in discovery

- `playwright-repo-test` does not provide robust Playwright storage-state save/load workflow; this remains net-new in `agent/` using Playwright Python APIs.
- Recorder currently depends heavily on injected bindings/panel flow; Phase 6 headless recorder requires an explicit adaptation strategy.
- Some locator execution paths evaluate dynamic expressions in JS style; Python port should prefer structured locator contracts over free-form evaluation.

### Task 0.3 Deliverable Check

- Goal met: recorder/replay/heal/storage/export assets from `playwright-repo-test` were mapped with adapt/copy/reject guidance.
- Done-when check: Phase 1-9 workstreams now have reuse references or explicit net-new guidance across Hermes, CLI, and repo-test sections.
- Phase status: Phase 0 discovery inventory is complete.

## Phase 3 Progress Log

| source path | key symbol(s) | target path in `agent/` | action | status | rationale |
| --- | --- | --- | --- | --- | --- |
| `playwright-cli/playwright-cli.js` (delegates to Playwright CLI client program) | snapshot/ref interaction model | `agent/src/agent/execution/snapshot.py` | adapt | done | Added accessibility snapshot capture (`ariaYaml`), deterministic `ContextFingerprint` generation, and `ref -> ElementHandle` resolution via frame-aware bindings for Phase 3.2. |
| `playwright-cli/playwright-cli.js`, `playwright-repo-test/lib/execute.js` | core runtime tool calls | `agent/src/agent/execution/tools.py` | adapt | done | Implemented typed async core tools (navigation, waits, interactions, assertions, dialog, frame enter/exit) with structured tool-call event emission and frame-aware target resolution for Phase 3.3. |
| `playwright-repo-test/lib/locator/candidates.js`, `playwright-repo-test/lib/locator/find.js` | deterministic locator candidates + probing | `agent/src/agent/locator/_ported/candidates.py`, `agent/src/agent/locator/_ported/find.py`, `agent/src/agent/locator/engine.py` | adapt | done | Added strategy-ordered candidate generation, live uniqueness/visibility probing, confidence scoring (`uniqueness`, `visibility/actionability`, `stability`, `history`, `freshness`), and `LocatorBundle` build/ranking APIs for Phase 3.4. |
| `playwright-cli/playwright-cli.js`, `playwright-repo-test/lib/execute.js` | extended interaction primitives | `agent/src/agent/execution/tools.py` | adapt | done | Added Phase 3b.1 interaction methods (`check`, `uncheck`, `select`, `upload`, `drag`, `hover`, `focus`) with typed results and tool-call event emission parity. |
| `playwright-cli/playwright-cli.js`, `playwright-repo-test/lib/execute.js` | extended assertion primitives | `agent/src/agent/execution/tools.py` | adapt | done | Added Phase 3b.2 assertion methods (`assert_value`, `assert_checked`, `assert_enabled`, `assert_hidden`, `assert_count`, `assert_in_viewport`) with typed assertion results and event emission parity. |
| `playwright-cli/playwright-cli.js`, `playwright-repo-test/lib/execute.js` | tabs and observability primitives | `agent/src/agent/execution/tools.py`, `agent/src/agent/execution/browser.py` | adapt | done | Added Phase 3b.3 methods (`tabs_list`, `tabs_select`, `tabs_close`, `console_messages`, `network_requests`, `screenshot`, `take_trace`) plus tab→context lookup support in `BrowserSession` for trace capture. |

## Phase 6.0 - Recorder Feasibility Spike

Spike artifact:
- `agent/scripts/spikes/recorder_spike.py`

Baseline references used for adaptation:
- `playwright-repo-test/lib/browser/inject.js`
- `playwright-repo-test/lib/record.js`
- `playwright-repo-test/recorder2.js`

Observed outcome (headed Playwright run):
- Captured one `fill` and one `click` from a live page via context-level `expose_binding` + `add_init_script` instrumentation.
- Emitted `page.on("framenavigated")` and `page.on("console")` events successfully during the same run.
- For each captured action, `LocatorEngine.build(..., force=True)` returned a valid `LocatorBundle` with stable primary selectors (`[data-testid="name-input"]`, `[data-testid="save-button"]`) and ranked fallbacks.

What works:
- Context-level injected capture hooks are sufficient for v1 semantic capture of click/fill on headed pages.
- Captured element descriptors (`tag/id/testid/aria/text/parents/xpath`) are enough input for current locator ranking.
- We can map capture payloads into a replay-oriented contract without CDP dependencies.

What does not work / limitations:
- `page.on(...)` events alone do not provide semantic interaction payloads (they are observability signals, not action-intent events).
- Raw DOM `input`/`click` streams do not encode higher-order intent (e.g., "submit", "search", or operator-selected mode) without extra recorder state.
- Programmatic automation can race binding delivery in edge cases; retaining an in-page event queue is a useful fallback for spike robustness.

Decision for Phase 6.1:
- **No CDP-level hooks are required** for initial click/fill capture.
- Prefer Playwright context/page instrumentation + explicit payload extraction.
- Use recorder-side intent state (CLI/UI mode selection) to prevent semantic loss when mapping low-level events to `Step` actions.

## Phase 6.1 - Headless Recorder Implementation

Implemented artifact:
- `agent/src/agent/recorder/recorder.py`

Baseline references adapted:
- `playwright-repo-test/lib/browser/inject.js` (context-level bindings + init script injection)
- `playwright-repo-test/lib/record.js` (capture event normalization and mode-driven interpretation)

Fixes applied for identified limitations:
- **`page.on(...)` semantic gap**: retained `page.on("framenavigated")` / `page.on("console")` as observability logs only; semantic capture now comes from injected DOM listeners producing structured recorder events.
- **raw DOM intent gap**: added a recorder intent resolver (`_resolve_intent`) that combines event type, target characteristics, and operator mode state (`set_operator_mode`) to derive replay actions (`click`, `fill`, `press`, assertion modes) plus semantic intent metadata.
- **binding race gap**: added an in-page durable queue (`window.__agentRecorderQueue`) with sequence IDs and a polling drain path (`_poll_inpage_queue`) while keeping binding callbacks as a fast path. Events are deduplicated by sequence before Step creation.

Recorder output behavior:
- Captured actions are converted to `Step` entries with locator bundles from `LocatorEngine`.
- Consecutive fill events for the same locator are coalesced to reduce noisy step graphs.
- On `stop()`, the recorder writes:
  - `runs/<run_id>/stepgraph.json`
  - `runs/<run_id>/manifest.json`
  and returns artifact metadata via `RecorderArtifact`.

## Phase 6.2 - Recorder CLI

Implemented artifact:
- `agent/src/agent/cli/record_cmd.py`

Baseline references adapted:
- `playwright-repo-test/recorder2.js` (interactive record session UX and stop control model)

What was implemented:
- Added Typer command `record` with required `--url`.
- Added optional `--storage-state` to load authenticated browser state.
- Added terminal hotkey stop flow (`--stop-key`, default `q`):
  - raw single-key capture on POSIX TTY terminals,
  - fallback to line input (`q` + Enter) for non-interactive terminals.
- Wired command to `StepGraphRecorder` start/stop lifecycle and printed artifact paths (`stepgraph.json`, `manifest.json`) for immediate replay via `agent run <stepgraph_path>`.

## Phase 9.1 - Provider Abstraction

Implemented artifacts:
- `agent/src/agent/llm/provider.py`
- `agent/src/agent/llm/openai.py`
- `agent/src/agent/llm/anthropic.py`
- `agent/src/agent/llm/openai_compatible.py`
- `agent/src/agent/llm/_ported/transports_base.py`
- `agent/src/agent/llm/_ported/transports_types.py`
- `agent/src/agent/llm/_ported/model_tools_utils.py`
- `agent/scripts/smoke/phase_9.py`

Baseline references adapted:
- `Hermes-Agent/agent/transports/base.py`
- `Hermes-Agent/agent/transports/types.py`
- `Hermes-Agent/agent/transports/chat_completions.py`
- `Hermes-Agent/agent/transports/anthropic.py`
- `Hermes-Agent/model_tools.py`

| source path | key symbol(s) | target path in `agent/` | action | status | rationale |
| --- | --- | --- | --- | --- | --- |
| `Hermes-Agent/agent/transports/base.py` | `ProviderTransport` abstraction boundary | `agent/src/agent/llm/_ported/transports_base.py`, `agent/src/agent/llm/provider.py` | adapt | done | Reused provider conversion/normalization seam for a stable `LLMProvider.chat(...)` API. |
| `Hermes-Agent/agent/transports/types.py` | normalized tool call and usage shapes | `agent/src/agent/llm/_ported/transports_types.py`, `agent/src/agent/llm/provider.py` | adapt | done | Preserved provider-agnostic response shape and usage accounting for downstream orchestrator/telemetry work. |
| `Hermes-Agent/agent/transports/chat_completions.py` | OpenAI-format message/tool handling and usage extraction | `agent/src/agent/llm/openai.py`, `agent/src/agent/llm/openai_compatible.py` | adapt | done | Added OpenAI-native and OpenAI-compatible adapters with shared transport normalization and tool-call extraction. |
| `Hermes-Agent/agent/transports/anthropic.py` | Anthropic message/tool conversion and stop-reason mapping | `agent/src/agent/llm/anthropic.py` | adapt | done | Added Anthropic adapter with OpenAI-style input compatibility and normalized tool-use output. |
| `Hermes-Agent/model_tools.py` | argument coercion and tool-argument hygiene patterns | `agent/src/agent/llm/_ported/model_tools_utils.py` | adapt | done | Reused coercion concepts to canonicalize provider tool-call arguments into deterministic JSON strings. |

## Phase 9.2 - Telemetry Hooks

Implemented artifacts:
- `agent/src/agent/llm/provider.py`
- `agent/src/agent/llm/openai.py`
- `agent/src/agent/llm/anthropic.py`
- `agent/src/agent/llm/openai_compatible.py`
- `agent/src/agent/storage/repos/telemetry.py`
- `agent/src/agent/telemetry/summary.py`
- `agent/scripts/smoke/phase_9.py`

Baseline references adapted:
- `Hermes-Agent/run_agent.py`
- `Hermes-Agent/agent/usage_pricing.py`

| source path | key symbol(s) | target path in `agent/` | action | status | rationale |
| --- | --- | --- | --- | --- | --- |
| `Hermes-Agent/run_agent.py` | callback-driven emission (`tool_start_callback`, `tool_complete_callback`, step/usage updates) | `agent/src/agent/llm/provider.py`, `agent/src/agent/llm/{openai,anthropic,openai_compatible}.py` | adapt | done | Added per-adapter LLM telemetry emission with explicit `callPurpose` and `contextTier` inputs at call time. |
| `Hermes-Agent/agent/usage_pricing.py` | normalized usage accounting pattern | `agent/src/agent/storage/repos/telemetry.py`, `agent/src/agent/telemetry/summary.py` | adapt | done | Added deterministic run-level aggregation over `LLMCall` token/cost/cache/latency fields and persisted `RunSummary` in run metadata. |

## Phase 9.3 - Staged Context Builder

Implemented artifacts:
- `agent/src/agent/llm/context.py`
- `agent/src/agent/llm/__init__.py`
- `agent/scripts/smoke/phase_9.py`

Baseline references adapted:
- None (net-new implementation aligned to `docs/03-functional-requirements.md` context-tier policy).

What was implemented:
- Added a reusable `StagedContextBuilder` with explicit Tier 0–3 builders:
  - Tier 0: `step` + `outcome`
  - Tier 1: Tier 0 + `scopedTarget`
  - Tier 2: Tier 1 + `history` + `contradictions`
  - Tier 3: Tier 2 + `fullSnapshot`
- Added `TokenPreflightEstimator` using `tiktoken` for deterministic preflight input/output token estimation.
- Added `ContextBuildResult` and `TokenPreflight` models for structured downstream use.
- Added `build_escalation_sequence(...)` to materialize tiered contexts up to a target tier.
- Extended Phase 9 smoke script to run context-builder checks and print tier/preflight signals before provider checks.

## Phase 9.4 - Orchestrator (Hermes-style loop)

Implemented artifacts:
- `agent/src/agent/llm/orchestrator.py`
- `agent/src/agent/llm/__init__.py`
- `agent/scripts/smoke/phase_9.py`

Baseline references adapted:
- `Hermes-Agent/run_agent.py`

| source path | key symbol(s) | target path in `agent/` | action | status | rationale |
| --- | --- | --- | --- | --- | --- |
| `Hermes-Agent/run_agent.py` | plan-act-observe loop, tool-call execution sequencing, retry/interrupt guard patterns | `agent/src/agent/llm/orchestrator.py` | adapt | done | Added a bounded async orchestration loop that dispatches Phase 3 tool calls, tracks no-progress retries, escalates context tiers, and tags calls with `plan`/`classification`/`repair`/`review`. |

What was implemented:
- Added `LLMOrchestrator.run_step(...)` with a Hermes-style loop for iterative LLM planning and tool execution.
- Added `OrchestratorConfig`, `OrchestratorResult`, and `ToolExecutionRecord` contracts.
- Added default Phase 3 tool definitions and runtime dispatcher wiring (`build_phase3_tool_definitions`, `build_phase3_tool_dispatcher`).
- Implemented tier escalation policy (`tier0 -> tier1 -> tier2 -> tier3`) gated by no-progress and escalation budgets.
- Implemented call-purpose tagging for telemetry (`plan`, `classification`, `repair`, `review`) on every provider call.
- Extended Phase 9 smoke script with a mock-provider orchestrator run so orchestration logic can be validated without external API keys.

## Phase 9.5 - Mode switch

Implemented artifacts:
- `agent/src/agent/core/mode.py`
- `agent/src/agent/cli/mode_cmd.py`
- `agent/scripts/smoke/phase_9.py`

Baseline references adapted:
- None (net-new implementation aligned to runtime mode-toggle requirements in docs).

What was implemented:
- Added `RuntimeMode` and `ModeController` in `core/mode.py` to switch between `manual|llm|hybrid` without mutating browser/session/checkpoint state.
- Added `RuntimeBinding` and `ModeSwitchResult` contracts to carry run/step/session context and switch metadata.
- Added `mode_switched` event emission through `CheckpointWriter` with payload fields (`previousMode`, `newMode`, `reason`, `runtimeStateReset=false`).
- Added `resolve_mode_for_run(...)` to recover the latest active mode from persisted run events.
- Added CLI command `agent mode set <manual|llm|hybrid>` in `cli/mode_cmd.py` with optional run-context flags to emit runtime mode-switch events.
- Extended Phase 9 smoke script with a mode-switch smoke check that verifies event emission and `runtime_state_reset=False`.

## Phase 11.3 - Optional Playwright spec generator

Implemented artifacts:
- `agent/src/agent/export/_ported/codegen.py`
- `agent/src/agent/export/_ported/__init__.py`
- `agent/src/agent/export/spec_writer.py`
- `agent/src/agent/export/__init__.py`

Baseline references adapted:
- `playwright-repo-test/lib/codegen.js`

What was implemented:
- Ported the core recorded-step to Playwright-test source generation flow into `build_playwright_test_source(...)`.
- Added deterministic locator fallback wiring in generated output via `LOCATOR_MAP` + `locatorFor(page, stepId)`.
- Added action/assertion rendering for common step actions (`navigate`, `click`, `fill`, `type`, `press`, `check`, `select`, `upload`, visibility/text/value/count assertions, etc.).
- Added async `PlaywrightSpecWriter` facade to load a run's `StepGraph` and emit `<run_id>.spec.ts` into the run folder by default.

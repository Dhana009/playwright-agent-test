# Playwright Coverage Matrix

## Fully Supported (v1)

- Click/fill/type/check/uncheck/select/upload/drag/hover/focus/press.
- Wait patterns: `waitForTimeout`, element visible, URL waits, title checks, load-state waits.
- Assertions: URL/title/visible/text/enabled/hidden/value/checked/count/in-viewport.
- Dialog handling (accept/dismiss workflows).
- iframe-targeted interactions.
- Session persistence via storage-state save/load.
- Record/replay/review/export baseline workflows.

## Partially Supported (v1)

- Complex multi-iframe nesting with frequent dynamic frame reloads.
- Highly dynamic text-only selector environments with weak accessibility metadata.
- App-specific custom widgets requiring extended action adapters.

## Deferred (v2+)

- Full Shadow DOM strategy with robust selector abstractions.
- Multi-app orchestration in a single run context.
- Broad visual/CUA-first exploration as a primary control mode.

## Known Limitations

- Locator confidence may degrade on highly unstable UI structures.
- Some mid-run environmental changes may still require operator intervention.
- Advanced cross-origin frame constraints depend on browser/runtime limits.

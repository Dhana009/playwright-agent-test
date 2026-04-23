from __future__ import annotations

# Ported from playwright-repo-test/lib/browser/inject.js — adapted for agent/
import asyncio
import json
from urllib.parse import quote

from agent.execution.browser import BrowserSession
from agent.locator.engine import LocatorEngine

_SAMPLE_HTML = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Recorder Spike</title>
  </head>
  <body>
    <main>
      <h1>Recorder Spike</h1>
      <label for="name-input">Name</label>
      <input
        id="name-input"
        name="name"
        data-testid="name-input"
        aria-label="Name"
        placeholder="Enter name"
      />
      <button
        id="save-button"
        data-testid="save-button"
        aria-label="Save"
        onclick="console.log('save-clicked')"
      >
        Save
      </button>
    </main>
  </body>
</html>
""".strip()

_CAPTURE_SCRIPT = """
(() => {
  if (window.__agent_recorder_spike_installed) return;
  window.__agent_recorder_spike_installed = true;
  window.__agentRecorderEvents = [];

  function toXPath(node) {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return '';
    if (node.id) return `//*[@id="${node.id}"]`;
    const parts = [];
    let current = node;
    while (current && current.nodeType === Node.ELEMENT_NODE) {
      let index = 1;
      let sibling = current.previousElementSibling;
      while (sibling) {
        if (sibling.tagName === current.tagName) index += 1;
        sibling = sibling.previousElementSibling;
      }
      const tag = current.tagName.toLowerCase();
      parts.unshift(index > 1 ? `${tag}[${index}]` : tag);
      current = current.parentElement;
    }
    return '/' + parts.join('/');
  }

  function parentTrail(node) {
    const parents = [];
    let current = node.parentElement;
    let depth = 0;
    while (current && current.tagName !== 'BODY' && depth < 4) {
      parents.push({
        tag: current.tagName.toLowerCase(),
        id: current.id || '',
        className: typeof current.className === 'string' ? current.className : '',
        testid:
          current.getAttribute('data-testid') ||
          current.getAttribute('data-test-id') ||
          current.getAttribute('data-qa') ||
          '',
      });
      current = current.parentElement;
      depth += 1;
    }
    return parents;
  }

  function collect(node) {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return null;
    const text = (node.innerText || node.textContent || '').trim().slice(0, 100);
    const testid =
      node.getAttribute('data-testid') ||
      node.getAttribute('data-test-id') ||
      node.getAttribute('data-qa') ||
      '';

    const attrs = {};
    for (const attr of node.attributes) {
      if (attr.name.startsWith('data-')) attrs[attr.name] = attr.value;
    }

    return {
      tag: node.tagName.toLowerCase(),
      id: node.id || '',
      className: typeof node.className === 'string' ? node.className : '',
      testid,
      text,
      placeholder: node.getAttribute('placeholder') || '',
      ariaLabel: node.getAttribute('aria-label') || '',
      role: node.getAttribute('role') || '',
      inputType: node.getAttribute('type') || '',
      name: node.getAttribute('name') || '',
      dataAttrs: attrs,
      siblingIndex: node.parentElement ? Array.from(node.parentElement.children).indexOf(node) : -1,
      parents: parentTrail(node),
      absoluteXPath: toXPath(node),
      frameContext: [],
      targetSemanticKey: [node.tagName.toLowerCase(), testid || node.id || node.getAttribute('name') || text]
        .filter(Boolean)
        .join(':'),
    };
  }

  document.addEventListener(
    'click',
    (event) => {
      const node = event.target instanceof Element ? event.target.closest('*') : null;
      const payload = collect(node);
      if (!payload) return;
      payload._event = 'click';
      payload._frameUrl = window.location.href;
      window.__agentRecorderEvents.push(payload);
      if (typeof window.__recClick === 'function') window.__recClick(payload);
    },
    true
  );

  document.addEventListener(
    'input',
    (event) => {
      const node = event.target instanceof Element ? event.target : null;
      if (!node) return;
      const payload = collect(node);
      if (!payload) return;
      payload.fillValue = 'value' in node ? node.value : (node.textContent || '').trim();
      payload._event = 'fill';
      payload._frameUrl = window.location.href;
      window.__agentRecorderEvents.push(payload);
      if (typeof window.__recFill === 'function') window.__recFill(payload);
    },
    true
  );
})();
""".strip()


def _to_data_url(html: str) -> str:
    return f"data:text/html;charset=utf-8,{quote(html)}"


async def main() -> None:
    session = BrowserSession(headless=False)
    locator_engine = LocatorEngine()
    captured_actions: list[dict[str, object]] = []

    await session.start()
    context_id, context = await session.new_context()
    page = await context.new_page()
    tab_id = session.get_tab_id(page)

    page.on(
        "framenavigated",
        lambda frame: print(
            json.dumps(
                {
                    "event": "framenavigated",
                    "contextId": context_id,
                    "tabId": tab_id,
                    "isMainFrame": frame == page.main_frame,
                    "url": frame.url,
                }
            )
        ),
    )
    page.on(
        "console",
        lambda message: print(
            json.dumps(
                {
                    "event": "console",
                    "type": message.type,
                    "text": message.text,
                }
            )
        ),
    )

    async def on_click(source, payload: dict[str, object]) -> None:
        payload["_event"] = "click"
        payload["_frameUrl"] = source.frame.url
        captured_actions.append(payload)

    async def on_fill(source, payload: dict[str, object]) -> None:
        payload["_event"] = "fill"
        payload["_frameUrl"] = source.frame.url
        payload["fillVal"] = payload.get("fillValue", "")
        captured_actions.append(payload)

    await context.expose_binding("__recClick", on_click)
    await context.expose_binding("__recFill", on_fill)
    await context.add_init_script(_CAPTURE_SCRIPT)

    await page.goto(_to_data_url(_SAMPLE_HTML), wait_until="domcontentloaded")
    await page.wait_for_function("window.__agent_recorder_spike_installed === true")
    await page.wait_for_timeout(250)

    # Simulate one fill and one click to validate capture semantics.
    await page.fill("[data-testid='name-input']", "Alice")
    await page.click("[data-testid='save-button']")
    await page.wait_for_timeout(500)

    if not captured_actions:
        fallback_actions = await page.evaluate("window.__agentRecorderEvents || []")
        for action in fallback_actions:
            captured_actions.append(action)

    for index, action in enumerate(captured_actions, start=1):
        event_name = str(action.get("_event", "unknown"))
        descriptor = {k: v for k, v in action.items() if not str(k).startswith("_")}
        try:
            bundle = await locator_engine.build(page, descriptor, force=True)
            bundle_payload = bundle.model_dump(by_alias=True)
        except Exception as exc:  # noqa: BLE001
            bundle_payload = {"error": str(exc)}

        print(
            json.dumps(
                {
                    "capturedIndex": index,
                    "event": event_name,
                    "frameUrl": action.get("_frameUrl"),
                    "descriptor": descriptor,
                    "locatorBundle": bundle_payload,
                },
                indent=2,
            )
        )

    await session.stop()


if __name__ == "__main__":
    asyncio.run(main())

"""Panel bridge — aiohttp WebSocket server connecting the injected panel JS to the Python runner."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Coroutine

from aiohttp import web
from playwright.async_api import Page

logger = logging.getLogger(__name__)

_PANEL_HTML = Path(__file__).parent / "web" / "panel.html"

# ── Message types (JS → Python) ────────────────────────────────────────────
MSG_PICK_START = "pick_start"
MSG_PICK_CANCEL = "pick_cancel"
MSG_VALIDATE_STEP = "validate_step"
MSG_APPEND_STEP = "append_step"
MSG_DELETE_STEP = "delete_step"
MSG_DUPLICATE_STEP = "duplicate_step"
MSG_REPLAY = "replay"
MSG_STOP_REPLAY = "stop_replay"
MSG_PAUSE_REQUEST = "pause_request"
MSG_RESUME = "resume"
MSG_FIX = "fix"
MSG_FORCE_FIX = "force_fix"
MSG_ACCEPT_LLM_REPAIR = "accept_llm_repair"
MSG_SAVE_VERSION = "save_version"
MSG_LIST_VERSIONS = "list_versions"
MSG_LOAD_VERSION = "load_version"
MSG_SET_LLM_MODE = "set_llm_mode"
MSG_START_RECORDING = "start_recording"
MSG_STOP_RECORDING = "stop_recording"
MSG_DELETE_VERSION = "delete_version"

# ── Message types (Python → JS) ────────────────────────────────────────────
MSG_PICK_RESULT = "pick_result"
MSG_VALIDATE_RESULT = "validate_result"
MSG_REPLAY_STEP_STATUS = "replay_step_status"
MSG_PAUSE = "pause"
MSG_FORCE_FIX_PROGRESS = "force_fix_progress"
MSG_VERSIONS_RESPONSE = "versions_response"
MSG_URL_CHANGED = "url_changed"
MSG_STEP_APPENDED = "step_appended"
MSG_LLM_STATUS = "llm_status"
MSG_RUN_COMPLETED = "run_completed"
MSG_RUN_ABORTED = "run_aborted"


MessageHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


class PanelBridge:
    """
    Manages the injected panel overlay and an aiohttp WebSocket + HTTP server.

    The panel HTML is served at GET /panel. WebSocket connections come in at GET /ws.
    Both run on the same aiohttp app on ws_port.
    """

    def __init__(
        self,
        page: Page,
        *,
        ws_port: int = 8766,
        llm_available: bool = False,
    ) -> None:
        self._page = page
        self._ws_port = ws_port
        self._llm_available = llm_available
        self._handlers: dict[str, list[MessageHandler]] = {}
        self._clients: set[web.WebSocketResponse] = set()
        self._runner: web.AppRunner | None = None
        self._connect_callback: MessageHandler | None = None

    def on_connect(self, callback: MessageHandler) -> None:
        """Register a callback to be called when a new WebSocket client connects."""
        self._connect_callback = callback

    @property
    def ws_port(self) -> int:
        return self._ws_port

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the combined HTTP+WS server and inject the panel into the page."""
        app = web.Application()
        app.router.add_get("/panel", self._handle_panel_http)
        app.router.add_get("/ws", self._handle_ws)
        app.router.add_get("/health", lambda r: web.Response(text="ok"))

        self._runner = web.AppRunner(app)
        await self._runner.setup()

        # Try the configured port, fall back to any free port
        port = self._ws_port
        for attempt in range(10):
            try:
                site = web.TCPSite(self._runner, "127.0.0.1", port)
                await site.start()
                self._ws_port = port
                break
            except OSError:
                port = self._ws_port + attempt + 1
        else:
            raise OSError(f"Could not bind panel server on ports {self._ws_port}–{port}")

        logger.info("panel_bridge_started port=%s", self._ws_port)

        await self._inject_panel()
        await self._setup_page_events()

    async def stop(self) -> None:
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        if self._runner:
            await self._runner.cleanup()

    # ── HTTP handler for panel.html ───────────────────────────────────────────

    async def _handle_panel_http(self, request: web.Request) -> web.Response:
        html = _PANEL_HTML.read_text(encoding="utf-8")
        # Inject the actual WS URL so the panel connects to the right port
        ws_url = f"ws://127.0.0.1:{self._ws_port}/ws"
        html = html.replace(
            "window.__agentPanelWsUrl || 'ws://127.0.0.1:8766/ws'",
            f"'{ws_url}'",
        )
        return web.Response(
            text=html,
            content_type="text/html",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    # ── WebSocket handler ─────────────────────────────────────────────────────

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._clients.add(ws)
        logger.info("panel_client_connected")

        # Send full state to newly connected client
        if self._connect_callback:
            try:
                await self._connect_callback({"type": "_connect", "payload": {}})
            except Exception as exc:
                logger.debug("connect_callback_error error=%s", str(exc))

        try:
            async for msg in ws:
                from aiohttp import WSMsgType
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    await self._dispatch(data)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        except Exception as exc:
            logger.debug("panel_ws_error error=%s", str(exc))
        finally:
            self._clients.discard(ws)
            logger.info("panel_client_disconnected")

        return ws

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})
        handlers = self._handlers.get(msg_type, [])
        logger.debug("panel_message_received type=%s has_handler=%s", msg_type, bool(handlers))
        if not handlers:
            logger.warning("panel_message_unhandled type=%s", msg_type)
            return
        for handler in handlers:
            try:
                await handler({"type": msg_type, "payload": payload})
            except Exception as exc:
                logger.error("panel_handler_error type=%s error=%s", msg_type, str(exc))

    # ── Panel injection ──────────────────────────────────────────────────────

    def _build_inject_js(self) -> str:
        panel_url = f"http://127.0.0.1:{self._ws_port}/panel"
        return f"""
(() => {{
  if (document.getElementById('__agent_panel_host')) return;

  var PANEL_W = 380;

  // Shift the page content left so the panel doesn't overlap it.
  // We write directly to document.documentElement style (highest specificity,
  // not affected by page stylesheets) instead of injecting a <style> tag.
  function __agentApplyMargin(w) {{
    document.documentElement.style.setProperty('margin-right', w + 'px', 'important');
    document.documentElement.style.setProperty('overflow-x', 'hidden', 'important');
    document.documentElement.style.setProperty('box-sizing', 'border-box', 'important');
  }}
  __agentApplyMargin(PANEL_W);

  const iframe = document.createElement('iframe');
  iframe.id = '__agent_panel_host';
  iframe.setAttribute('data-agent-panel', '1');
  iframe.src = '{panel_url}';
  iframe.style.cssText = [
    'position:fixed',
    'top:0',
    'right:0',
    'width:' + PANEL_W + 'px',
    'height:100%',
    'border:none',
    'z-index:2147483647',
    'box-shadow:-4px 0 24px rgba(0,0,0,0.5)',
  ].join(';');
  document.documentElement.appendChild(iframe);

  window.addEventListener('message', function(e) {{
    if (!e.data || e.data.type !== 'panel_resize') return;
    var w = e.data.width <= 20 ? e.data.width : Math.max(280, Math.min(600, e.data.width));
    iframe.style.width = w + 'px';
    __agentApplyMargin(w);
  }});
}})();
"""

    async def _inject_panel(self) -> None:
        """Inject panel iframe as a fixed overlay on the right side of the page."""
        inject_js = self._build_inject_js()
        pick_helper_js = self._build_pick_helper_js()

        # Runs on every new document load (handles navigation)
        await self._page.add_init_script(inject_js)
        await self._page.add_init_script(pick_helper_js)

        # Also re-evaluate after each main-frame navigation finishes loading
        async def _on_navigated(frame: Any) -> None:
            if frame == self._page.main_frame:
                try:
                    await self._page.evaluate(inject_js)
                    await self._page.evaluate(pick_helper_js)
                except Exception:
                    pass

        self._page.on("framenavigated", _on_navigated)

        # Inject into current page right now
        try:
            await self._page.evaluate(inject_js)
            await self._page.evaluate(pick_helper_js)
        except Exception as exc:
            logger.debug("panel_inject_eval_error error=%s", str(exc))

    def _build_pick_helper_js(self) -> str:
        """Inject standalone collectTarget + hover-outline pick helpers (no recorder needed)."""
        return """
(() => {
  if (window.__agentCollectTarget) return;  // recorder already injected it

  function toXPath(node) {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return '';
    if (node.id) return '//*[@id="' + node.id + '"]';
    const parts = [];
    let cur = node;
    while (cur && cur.nodeType === Node.ELEMENT_NODE) {
      let idx = 1, sib = cur.previousElementSibling;
      while (sib) { if (sib.tagName === cur.tagName) idx++; sib = sib.previousElementSibling; }
      const tag = cur.tagName.toLowerCase();
      parts.unshift(idx > 1 ? tag + '[' + idx + ']' : tag);
      cur = cur.parentElement;
    }
    return '/' + parts.join('/');
  }

  function parentTrail(node) {
    const out = []; let cur = node && node.parentElement; let depth = 0;
    while (cur && cur.tagName !== 'BODY' && depth < 4) {
      out.push({ tag: cur.tagName.toLowerCase(), id: cur.id || '',
        className: typeof cur.className === 'string' ? cur.className : '',
        testid: cur.getAttribute('data-testid') || cur.getAttribute('data-test-id') || cur.getAttribute('data-qa') || '' });
      cur = cur.parentElement; depth++;
    }
    return out;
  }

  function dataAttrs(node) {
    const attrs = {};
    if (!node || !node.attributes) return attrs;
    for (const attr of node.attributes) { if (attr.name.startsWith('data-')) attrs[attr.name] = attr.value; }
    return attrs;
  }

  function collectTarget(node) {
    if (!node || node.nodeType !== Node.ELEMENT_NODE) return null;
    const text = (node.innerText || node.textContent || '').trim().slice(0, 120);
    const testid = node.getAttribute('data-testid') || node.getAttribute('data-test-id') || node.getAttribute('data-qa') || '';
    return {
      tag: node.tagName.toLowerCase(), id: node.id || '',
      className: typeof node.className === 'string' ? node.className : '',
      testid, text,
      placeholder: node.getAttribute('placeholder') || '',
      ariaLabel: node.getAttribute('aria-label') || '',
      role: node.getAttribute('role') || '',
      inputType: node.getAttribute('type') || '',
      name: node.getAttribute('name') || '',
      dataAttrs: dataAttrs(node),
      siblingIndex: node.parentElement ? Array.from(node.parentElement.children).indexOf(node) : -1,
      parents: parentTrail(node),
      absoluteXPath: toXPath(node),
      frameContext: [],
      targetSemanticKey: [node.tagName.toLowerCase(), testid || node.id || node.getAttribute('name') || text].filter(Boolean).join(':'),
    };
  }

  window.__agentCollectTarget = collectTarget;

  // ── Hover outline for pick mode ──────────────────────────────────────────
  let __pickHoverEl = null;
  let __pickStyleInjected = false;

  function __ensurePickStyles() {
    if (__pickStyleInjected) return;
    __pickStyleInjected = true;
    const st = document.createElement('style');
    st.id = '__agent_pick_styles';
    st.textContent = `
      .__agent_pick_hover { outline: 2px solid #2979ff !important; outline-offset: 2px !important; background: rgba(41,121,255,0.08) !important; cursor: crosshair !important; }
      #__agent_pick_tip { position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:#0f172a;color:#e2e8f0;font:12px/1.35 ui-monospace,monospace;padding:6px 14px;border-radius:6px;z-index:2147483645;pointer-events:none;white-space:nowrap;max-width:92vw;overflow:hidden;text-overflow:ellipsis;border:1px solid #334155;box-shadow:0 4px 24px rgba(0,0,0,0.45); }
    `;
    (document.head || document.documentElement).appendChild(st);
    const tip = document.createElement('div');
    tip.id = '__agent_pick_tip';
    tip.style.display = 'none';
    (document.body || document.documentElement).appendChild(tip);
  }

  function __pickClear() {
    if (__pickHoverEl) { try { __pickHoverEl.classList.remove('__agent_pick_hover'); } catch(_) {} __pickHoverEl = null; }
    const tip = document.getElementById('__agent_pick_tip');
    if (tip) tip.style.display = 'none';
  }

  document.addEventListener('mouseover', (e) => {
    if (!window.__agentPickIntent || !window.__agentPickIntent.kind) { if (__pickHoverEl) __pickClear(); return; }
    __ensurePickStyles();
    if (__pickHoverEl && __pickHoverEl !== e.target) { try { __pickHoverEl.classList.remove('__agent_pick_hover'); } catch(_) {} }
    const el = e.target;
    if (!el || el.nodeType !== 1) return;
    const panel = document.getElementById('__agent_panel_host');
    if (panel && (el === panel || el.closest && el.closest('#__agent_panel_host'))) return;
    try { el.classList.add('__agent_pick_hover'); } catch(_) {}
    __pickHoverEl = el;
    const tip = document.getElementById('__agent_pick_tip');
    if (tip) {
      const tag = el.tagName.toLowerCase();
      const tid = el.getAttribute('data-testid');
      const label = el.getAttribute('aria-label');
      const txt = (el.innerText || el.textContent || '').trim().slice(0, 40);
      tip.textContent = '<' + tag + (tid ? '[data-testid="' + tid + '"]' : '') + (label ? '[aria-label="' + label + '"]' : '') + '>' + (txt ? ' "' + txt + '"' : '');
      tip.style.display = 'block';
    }
  }, true);

  document.addEventListener('mouseout', (e) => {
    if (__pickHoverEl === e.target) __pickClear();
  }, true);

  window.__agentEnsurePickUi = __ensurePickStyles;
})();
"""

    async def _setup_page_events(self) -> None:
        async def on_url_change(url: str) -> None:
            await self.send({"type": MSG_URL_CHANGED, "payload": {"url": url}})

        self._page.on("url", on_url_change)
        try:
            await self.send({"type": MSG_URL_CHANGED, "payload": {"url": self._page.url}})
        except Exception:
            pass

    # ── Public API ───────────────────────────────────────────────────────────

    def on(self, msg_type: str, handler: MessageHandler) -> None:
        self._handlers.setdefault(msg_type, []).append(handler)

    def off(self, msg_type: str, handler: MessageHandler) -> None:
        handlers = self._handlers.get(msg_type, [])
        if handler in handlers:
            handlers.remove(handler)

    async def send(self, msg: dict[str, Any]) -> None:
        raw = json.dumps(msg)
        dead: set[web.WebSocketResponse] = set()
        for ws in list(self._clients):
            try:
                await ws.send_str(raw)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    async def broadcast_step_status(self, step_id: str, status: str, error: str | None = None) -> None:
        await self.send({
            "type": MSG_REPLAY_STEP_STATUS,
            "payload": {"stepId": step_id, "status": status, "error": error},
        })

    async def broadcast_pick_result(self, descriptor: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
        await self.send({
            "type": MSG_PICK_RESULT,
            "payload": {"descriptor": descriptor, "candidates": candidates},
        })

    async def broadcast_validate_result(self, passed: bool, error: str | None = None, duration_ms: int = 0) -> None:
        await self.send({
            "type": MSG_VALIDATE_RESULT,
            "payload": {"passed": passed, "error": error, "durationMs": duration_ms},
        })

    async def broadcast_pause(self, step_id: str, reason: str = "") -> None:
        await self.send({
            "type": MSG_PAUSE,
            "payload": {"stepId": step_id, "reason": reason},
        })

    async def broadcast_force_fix_progress(
        self,
        stage: int,
        status: str,
        repaired: bool = False,
        locator: str | None = None,
        explanation: str | None = None,
    ) -> None:
        await self.send({
            "type": MSG_FORCE_FIX_PROGRESS,
            "payload": {
                "stage": stage,
                "status": status,
                "repaired": repaired,
                "locator": locator,
                "explanation": explanation,
            },
        })

    async def broadcast_versions(self, versions: list[dict[str, Any]]) -> None:
        await self.send({"type": MSG_VERSIONS_RESPONSE, "payload": {"versions": versions}})

    async def broadcast_step_appended(self, step: dict[str, Any]) -> None:
        await self.send({"type": MSG_STEP_APPENDED, "payload": {"step": step}})

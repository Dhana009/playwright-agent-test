"""LLM Assist — LLM-orchestrated repair loop for any stuck step.

The LLM is the brain:
  1. Gets full context: DOM, step, history of every attempt + exact error
  2. Gets framework fingerprint (React/Vue/Angular/FilePond/Dropzone/…)
  3. Gets a screenshot of the page (vision context — Anthropic/OpenAI both support it)
  4. Returns up to 3 ordered attempts — each with exact strategy + locator + js_code
  5. System tries them one by one; after each attempt it captures a DOM diff
  6. DOM diff lets the LLM (and system) self-judge: did the DOM change as expected?
  7. User only confirms visually when execution ran without error

Strategies the LLM can choose per attempt:
  - locator_fix        → change the locator, re-run the same action
  - file_chooser_click → click element to open file chooser, intercept it
  - set_input_files    → call setInputFiles directly on a file input
  - js_execute         → run custom JS snippet (LLM writes the complete code)
  - dispatch_events    → setInputFiles + dispatch change/input events
"""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

from playwright.async_api import Page

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_path_from_issue(text: str) -> str | None:
    """Extract the first absolute file path from free-form user text (handles %20 encoding)."""
    import re
    from urllib.parse import unquote
    if not text:
        return None
    for m in re.finditer(r'(/[^\s,]+)', text):
        candidate = m.group(1).rstrip(".,;:'\")>")
        decoded = unquote(candidate)
        last = decoded.split('/')[-1]
        if len(decoded) > 4 and ('.' in last or len(decoded.split('/')) > 3):
            return decoded
    return None


# ── Framework fingerprinting ──────────────────────────────────────────────────

_FRAMEWORK_JS = """() => {
    const out = { frameworks: [], uploadWidgets: [], hints: [] };

    // JS frameworks
    if (window.__REACT_DEVTOOLS_GLOBAL_HOOK__ || document.querySelector('[data-reactroot],[data-reactid]'))
        out.frameworks.push('React');
    if (window.Vue || document.querySelector('[data-v-]'))
        out.frameworks.push('Vue');
    if (window.angular || window.getAllAngularRootElements)
        out.frameworks.push('Angular');
    if (window.Svelte || document.querySelector('[class*="svelte-"]'))
        out.frameworks.push('Svelte');

    // Upload widgets
    if (window.FilePond || document.querySelector('.filepond--root'))
        out.uploadWidgets.push('FilePond');
    if (window.Dropzone || document.querySelector('.dropzone'))
        out.uploadWidgets.push('Dropzone.js');
    if (document.querySelector('[class*="uppy"]'))
        out.uploadWidgets.push('Uppy');
    if (document.querySelector('[class*="dropzone"],[class*="drop-zone"],[class*="upload-zone"]'))
        out.uploadWidgets.push('custom-dropzone');
    if (document.querySelector('[class*="filedrop"],[class*="file-drop"]'))
        out.uploadWidgets.push('custom-filedrop');

    // Hidden file inputs (useful to know even if main element is a div)
    const fileInputs = document.querySelectorAll('input[type=file]');
    if (fileInputs.length) {
        out.hints.push(`${fileInputs.length} <input type=file> found (${[...fileInputs].map(i => i.id || i.name || i.className.slice(0,30) || 'unnamed').join(', ')})`);
    }

    // FilePond API hint
    if (window.FilePond) {
        const fpEl = document.querySelector('.filepond--root');
        if (fpEl) {
            const pond = window.FilePond.find(fpEl);
            if (pond) out.hints.push('FilePond instance accessible via FilePond.find(el).addFile(file)');
        }
    }

    // Dropzone API hint
    if (window.Dropzone) {
        const dzEl = document.querySelector('.dropzone');
        if (dzEl && dzEl.dropzone) out.hints.push('Dropzone instance on .dropzone element: el.dropzone.addFile(file)');
    }

    // React fiber — can fire synthetic events that React actually hears
    const anyEl = document.querySelector('[data-reactroot] *,[data-reactid] *');
    if (anyEl) {
        const fiberKey = Object.keys(anyEl).find(k => k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance'));
        if (fiberKey) out.hints.push('React fiber accessible — use ReactTestUtils.Simulate or nativeEvent.__reactFiber for synthetic events');
    }

    return out;
}"""


async def detect_frameworks(page: Page) -> dict[str, Any]:
    """Return detected JS frameworks, upload widgets, and API hints."""
    try:
        result = await page.evaluate(_FRAMEWORK_JS)
        if isinstance(result, dict):
            return result
    except Exception as exc:
        logger.debug("framework_detect_error error=%s", exc)
    return {"frameworks": [], "uploadWidgets": [], "hints": []}


def _format_framework_block(fw: dict[str, Any]) -> str:
    parts: list[str] = []
    frameworks = fw.get("frameworks") or []
    widgets = fw.get("uploadWidgets") or []
    hints = fw.get("hints") or []
    if frameworks:
        parts.append(f"JS framework(s): {', '.join(frameworks)}")
    if widgets:
        parts.append(f"Upload widget(s): {', '.join(widgets)}")
    for h in hints:
        parts.append(f"API hint: {h}")
    if not parts:
        return "(no framework signals detected)"
    return "\n".join(parts)


# ── Screenshot (vision context) ───────────────────────────────────────────────

async def capture_screenshot_b64(page: Page) -> str | None:
    """Capture a full-page screenshot as base64 PNG. Returns None on failure."""
    try:
        png_bytes = await page.screenshot(full_page=False, type="png")
        return base64.b64encode(png_bytes).decode("ascii")
    except Exception as exc:
        logger.debug("screenshot_error error=%s", exc)
        return None


def _supports_vision(llm_provider: Any) -> bool:
    """Check if the provider/model is known to support image input."""
    name = getattr(llm_provider, "provider_name", "").lower()
    model = getattr(llm_provider, "default_model", "").lower()
    if name == "anthropic":
        return True  # all claude models support vision
    if name == "openai":
        # gpt-4o, gpt-4-turbo, gpt-4-vision, o1, o3 support vision; gpt-3.5 does not
        return any(m in model for m in ("gpt-4o", "gpt-4-turbo", "gpt-4v", "o1", "o3", "o4"))
    return False


def _build_messages_with_vision(
    system_prompt: str,
    screenshot_b64: str | None,
    llm_provider: Any,
) -> list[dict[str, Any]]:
    """Build the messages list, attaching the screenshot as an image block if supported."""
    if not screenshot_b64 or not _supports_vision(llm_provider):
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Analyse and return the JSON now."},
        ]

    provider_name = getattr(llm_provider, "provider_name", "").lower()

    if provider_name == "anthropic":
        user_content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": screenshot_b64,
                },
            },
            {"type": "text", "text": "This is a screenshot of the page right now. Use it together with the DOM to understand the actual UI state. Analyse and return the JSON now."},
        ]
    else:
        # OpenAI vision format
        user_content = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{screenshot_b64}", "detail": "low"},
            },
            {"type": "text", "text": "This is a screenshot of the page right now. Use it together with the DOM to understand the actual UI state. Analyse and return the JSON now."},
        ]

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


# ── DOM snapshot & diff ───────────────────────────────────────────────────────

_DOM_SNAPSHOT_JS = """(selector) => {
    function clean(el, maxLen) {
        if (!el) return '';
        const c = el.cloneNode(true);
        c.querySelectorAll('script,style,noscript,svg,img').forEach(n => n.remove());
        c.querySelectorAll('*').forEach(n => {
            const cls = n.getAttribute('class');
            if (cls && cls.length > 80) n.setAttribute('class', cls.slice(0, 80) + '…');
            const sty = n.getAttribute('style');
            if (sty && sty.length > 60) n.setAttribute('style', '…');
        });
        return c.outerHTML.slice(0, maxLen);
    }
    try {
        let el = null;
        try { el = document.querySelector(selector); } catch(_) {}
        if (!el) {
            // snapshot a wider region
            const body = document.body;
            return body ? clean(body, 3000) : '(body missing)';
        }
        // Walk up 3 levels for context
        let ctx = el;
        for (let i = 0; i < 3 && ctx.parentElement && ctx.parentElement.tagName !== 'BODY'; i++) {
            ctx = ctx.parentElement;
        }
        return clean(ctx, 3000);
    } catch(e) { return 'snapshot error: ' + String(e); }
}"""


async def snapshot_dom(page: Page, selector: str) -> str:
    """Capture a clean DOM snapshot around selector. Used for before/after diff."""
    try:
        result = await page.evaluate(_DOM_SNAPSHOT_JS, selector)
        return str(result or "").strip()[:3000]
    except Exception as exc:
        return f"(snapshot failed: {exc})"


def _dom_diff_summary(before: str, after: str) -> dict[str, Any]:
    """
    Compare before/after DOM snapshots.
    Returns { changed: bool, addedSignals: [...], removedSignals: [...], summary: str }
    """
    if before == after:
        return {"changed": False, "addedSignals": [], "removedSignals": [], "summary": "DOM unchanged after execution."}

    # Detect meaningful signals added/removed
    SIGNALS = [
        r'filename|file-?name',
        r'upload.*success|success.*upload',
        r'preview|thumbnail',
        r'progress|percent',
        r'error|warning|alert',
        r'remove|delete|clear',
        r'checkmark|check|tick',
        r'\.pdf|\.png|\.jpg|\.csv|\.xlsx|\.docx',
    ]

    added: list[str] = []
    removed: list[str] = []

    before_lower = before.lower()
    after_lower = after.lower()

    for sig in SIGNALS:
        in_before = bool(re.search(sig, before_lower))
        in_after = bool(re.search(sig, after_lower))
        if in_after and not in_before:
            added.append(sig)
        elif in_before and not in_after:
            removed.append(sig)

    # Count element differences (rough)
    before_tags = re.findall(r'<[a-zA-Z][^>]*>', before)
    after_tags = re.findall(r'<[a-zA-Z][^>]*>', after)
    tag_delta = len(after_tags) - len(before_tags)

    summary_parts = ["DOM changed after execution."]
    if added:
        summary_parts.append(f"New signals: {added}")
    if removed:
        summary_parts.append(f"Gone signals: {removed}")
    if tag_delta > 0:
        summary_parts.append(f"+{tag_delta} new elements appeared.")
    elif tag_delta < 0:
        summary_parts.append(f"{abs(tag_delta)} elements removed.")

    return {
        "changed": True,
        "addedSignals": added,
        "removedSignals": removed,
        "tagDelta": tag_delta,
        "summary": " ".join(summary_parts),
    }


# ── DOM capture (for LLM context) ────────────────────────────────────────────

async def capture_focused_dom(page: Page, locator: str) -> str:
    """Capture real DOM around the target element. Returns cleaned outerHTML up to 3000 chars."""
    try:
        snippet = await page.evaluate("""(selector) => {
            function clean(el, maxLen) {
                if (!el) return '';
                const c = el.cloneNode(true);
                c.querySelectorAll('script,style,noscript,svg,img').forEach(n => n.remove());
                c.querySelectorAll('*').forEach(n => {
                    const cls = n.getAttribute('class');
                    if (cls && cls.length > 60) n.setAttribute('class', cls.slice(0, 60) + '…');
                    const sty = n.getAttribute('style');
                    if (sty && sty.length > 60) n.setAttribute('style', '…');
                    ['nitro-lazy-src','nitro-lazy-empty','data-nitro-empty-id','decoding','loading','src'].forEach(a => {
                        const v = n.getAttribute(a);
                        if (v && v.length > 40) n.removeAttribute(a);
                    });
                });
                return c.outerHTML.slice(0, maxLen);
            }
            try {
                let el = null;
                try { el = document.querySelector(selector); } catch(_) {}
                if (!el) return `(selector "${selector}" not found in DOM)`;
                const elHtml = clean(el, 2000);
                const parent = el.parentElement;
                if (parent && parent.tagName !== 'BODY' && parent.tagName !== 'HTML') {
                    const parentHtml = clean(parent, 3000);
                    if (parentHtml.length < 3000) return parentHtml;
                }
                return elHtml;
            } catch(e) { return 'DOM capture error: ' + String(e); }
        }""", locator)
        result = str(snippet or "").strip()
        if result and not result.startswith("(") and not result.startswith("DOM capture error"):
            return result[:3000]
        return result or "(DOM unavailable)"
    except Exception as exc:
        return f"(DOM capture failed: {exc})"


# ── History block ─────────────────────────────────────────────────────────────

def _build_history_block(iterations: list[dict[str, Any]]) -> str:
    if not iterations:
        return ""
    lines = ["━━━ EXECUTION HISTORY (every attempt so far) ━━━"]
    for it in iterations:
        num = it.get("iteration", "?")
        strategy = it.get("strategy", "")
        locator = it.get("locator", "")
        outcome = it.get("outcome", "pending")
        error = it.get("error", "")
        exec_detail = it.get("exec_detail", "")
        dom_diff = it.get("domDiff") or {}

        lines.append(f"\n  Attempt #{num}:")
        if strategy:
            lines.append(f"    strategy   : {strategy}")
        if locator:
            lines.append(f"    locator    : {locator!r}")
        lines.append(f"    outcome    : {outcome}")
        if error:
            lines.append(f"    error      : {error[:200]}")
        if exec_detail:
            lines.append(f"    detail     : {exec_detail[:200]}")
        if dom_diff:
            lines.append(f"    dom_changed: {dom_diff.get('changed', False)}")
            lines.append(f"    dom_summary: {dom_diff.get('summary', '')[:200]}")
        if outcome == "user_failed":
            lines.append("    → User confirmed: code ran without error but result did NOT appear on the page")

    lines.append("\n  ↳ Every attempt above failed. You MUST try something meaningfully different.")
    return "\n".join(lines)


# ── Main LLM round ────────────────────────────────────────────────────────────

async def run_llm_assist_iteration(
    page: Page,
    *,
    step: dict[str, Any],
    issue: str,
    picked_locator: str | None,
    iteration_history: list[dict[str, Any]],
    llm_provider: Any,
) -> dict[str, Any]:
    """
    One LLM round. Collects DOM + frameworks + screenshot, then asks the LLM to return
    up to 3 ordered repair attempts.

    Returns:
      {
        iteration: int,
        diagnosis: str,
        attempts: [{ strategy, locator, js_code, rationale }, ...],
        capturedDom: str,
        frameworks: dict,
        screenshotB64: str | None,
      }
    """
    iteration_num = len(iteration_history) + 1
    action = step.get("action", "")
    original_locator = step.get("locator", "")
    params = step.get("params") or {}

    dom_anchor = picked_locator or original_locator or "body"

    # Collect all context in parallel
    import asyncio

    async def _no_screenshot() -> None:
        return None

    focused_dom, frameworks, screenshot_b64 = await asyncio.gather(
        capture_focused_dom(page, dom_anchor),
        detect_frameworks(page),
        capture_screenshot_b64(page) if _supports_vision(llm_provider) else _no_screenshot(),
    )

    history_block = _build_history_block(iteration_history)
    framework_block = _format_framework_block(frameworks)

    file_path_val = ""
    if action in ("upload", "upload_file"):
        fp = params.get("path") or params.get("file_paths") or params.get("text") or params.get("value") or ""
        file_path_val = fp[0] if isinstance(fp, list) and fp else str(fp)
        # Fallback: extract from issue text if params had nothing
        if not file_path_val and issue:
            file_path_val = _extract_path_from_issue(issue) or ""

    param_parts = []
    for k in ("text", "value", "key", "url", "path", "file_paths"):
        v = params.get(k)
        if v:
            param_parts.append(f"{k}={str(v)[:80]!r}")
    param_summary = ", ".join(param_parts) if param_parts else "(none)"

    # Detect whether the original locator targets a non-input element (div, button, span, etc.)
    # This is the key signal for strategy selection.
    orig_is_clickable_widget = bool(
        original_locator
        and not original_locator.strip().lower().startswith("input")
        and "input[type" not in original_locator.lower()
        and original_locator.strip().lstrip("#.[").split("[")[0].split(":")[0] not in ("input",)
    )

    upload_knowledge = ""
    if action in ("upload", "upload_file"):
        upload_knowledge = f"""
━━━ UPLOAD CONTEXT ━━━
File to upload: {file_path_val!r}
Variable `filePath` in js_execute is pre-set to this value.

━━━ STRATEGY DECISION TREE (follow this exactly) ━━━

STEP 1 — Look at the original locator: {original_locator!r}
  {"→ It is a DIV/BUTTON/SPAN widget, NOT an <input type=file>." if orig_is_clickable_widget else "→ It may be a real file input — check DOM to confirm."}

STEP 2 — Choose your primary strategy based on what the locator targets:

  If locator targets a DIV, BUTTON, SPAN, or any non-input element:
    → Use "file_chooser_click" FIRST.
      It clicks the element, intercepts the OS file chooser dialog that the widget opens, and sets the file.
      This is correct for any custom upload widget (React, Vue, etc.) that opens a native file dialog on click.
    → Only use "set_input_files" / "js_execute" as FALLBACK if file_chooser_click timed out (no dialog opened).

  If locator targets <input type=file> directly:
    → Use "set_input_files" FIRST — most reliable.
    → Fallback: "dispatch_events" (setInputFiles + fire change/input events).

  If file_chooser_click timed out (no OS dialog opened):
    → The widget does NOT open a native dialog.
    → Use "js_execute" with DataTransfer + drop event simulation.

STEP 3 — Framework API (overrides everything if detected):
  • FilePond detected → FilePond.find(el).addFile(filePath)
  • Dropzone.js detected → el.dropzone.addFile(new File([...], name))

STEP 4 — If DOM unchanged after set_input_files → the hidden input is not wired to the component.
  → Switch to file_chooser_click on the visible widget div instead.
"""

    non_upload_knowledge = ""
    if action not in ("upload", "upload_file"):
        non_upload_knowledge = """
━━━ AVAILABLE STRATEGIES ━━━
  "locator_fix" — target a different element, re-run the same action
  "js_execute"  — write and run custom JS in the page (write the complete code body)
"""

    vision_note = ""
    if screenshot_b64 and _supports_vision(llm_provider):
        vision_note = "\n[Screenshot of current page state is attached as an image for your visual analysis]\n"

    system_prompt = f"""You are an expert Playwright automation engineer fixing a broken recorded step.
You have three sources of ground truth: the DOM, the framework fingerprint, and a screenshot.{vision_note}
━━━ BROKEN STEP ━━━
  action          : {action}
  original locator: {original_locator!r}
  params          : {param_summary}

━━━ USER'S DESCRIPTION ━━━
{issue.strip() or "(none provided)"}

━━━ PAGE FRAMEWORK FINGERPRINT ━━━
{framework_block}

━━━ LIVE DOM (around the target element) ━━━
{focused_dom}

{history_block}
{upload_knowledge}{non_upload_knowledge}
━━━ DOM DIFF INTELLIGENCE ━━━
After each attempt the system captures a before/after DOM diff and adds it to the history above.
"dom_changed: False" means the attempt had zero visible effect on the page — try something fundamentally different.
"dom_changed: True" with relevant signals means the attempt had an effect — if user said it failed visually,
the right element may have been targeted but the wrong event/method was used.

━━━ YOUR JOB ━━━
1. Read the framework fingerprint — if FilePond/Dropzone/Uppy is detected, use its JavaScript API directly.
2. Look at the DOM and screenshot together — find the actual interactive element.
3. Read the history carefully — every failed attempt tells you what NOT to do.
4. Give up to 3 ordered repair attempts. The system tries them one by one and stops at the first success.
   All failures (with exact errors + DOM diffs) will come back to you for the next round.

━━━ EXACT OUTPUT FORMAT ━━━
Return ONLY valid JSON, exactly this shape:

{{
  "diagnosis": "<one sentence: root cause of the failure>",
  "attempts": [
    {{
      "strategy": "<file_chooser_click|set_input_files|dispatch_events|js_execute|locator_fix>",
      "locator": "<exact CSS selector or XPath from the DOM above>",
      "js_code": "<complete JS code body — only for js_execute, null otherwise>",
      "rationale": "<one line: why this specific attempt should work>"
    }}
  ]
}}

RULES:
• attempts array: 1–3 items, ordered best→fallback
• locator: MUST be a real selector visible in the DOM above — never invent one
• Each attempt must use a DIFFERENT strategy or locator than prior history entries
• js_execute code: complete self-contained body, variable `filePath` is pre-set, throw on failure
• Never repeat a strategy+locator combo that already appears in history with an error
• If dom_changed was False for set_input_files on a hidden input → the hidden input is not wired to the component.
  Switch to file_chooser_click on the VISIBLE clickable widget (div/button) instead.
• If dom_changed was False for an attempt AND it is not a file_chooser_click → change strategy entirely
• For DIV/BUTTON upload widgets: file_chooser_click is almost always the correct first attempt
• If FilePond/Dropzone detected and js_execute → use their JS API, not DataTransfer simulation
"""

    try:
        messages = _build_messages_with_vision(system_prompt, screenshot_b64, llm_provider)
        response = await llm_provider.chat(messages, max_tokens=1800)
        raw = (response.content or "").strip()
        logger.info("llm_assist_raw iteration=%d raw=%r", iteration_num, raw[:600])
        result = _extract_json(raw)

        attempts = result.get("attempts") or []
        # Back-compat: LLM might still return single strategy/locator shape
        if not attempts and result.get("strategy"):
            attempts = [{
                "strategy": result.get("strategy"),
                "locator": result.get("locator"),
                "js_code": result.get("js_code"),
                "rationale": result.get("plan") or result.get("rationale") or "",
            }]

        clean_attempts = []
        for a in attempts[:3]:
            if not isinstance(a, dict):
                continue
            clean_attempts.append({
                "strategy": (a.get("strategy") or "locator_fix").strip(),
                "locator": (a.get("locator") or "").strip() or None,
                "js_code": a.get("js_code") or None,
                "rationale": (a.get("rationale") or "")[:300],
            })

        if not clean_attempts:
            clean_attempts = [{
                "strategy": "locator_fix",
                "locator": original_locator or None,
                "js_code": None,
                "rationale": "fallback: retry original locator",
            }]

        # System override: if the original locator is a non-input widget (div/button/span)
        # and file_chooser_click hasn't been tried yet in history, inject it as attempt #1.
        # This prevents the LLM from wasting attempts on set_input_files against hidden inputs
        # that are not wired to the component's upload handler.
        already_tried_fcc = any(
            it.get("strategy") == "file_chooser_click" for it in iteration_history
        )
        if (
            orig_is_clickable_widget
            and action in ("upload", "upload_file")
            and not already_tried_fcc
            and not any(a.get("strategy") == "file_chooser_click" for a in clean_attempts)
        ):
            clean_attempts.insert(0, {
                "strategy": "file_chooser_click",
                "locator": original_locator,
                "js_code": None,
                "rationale": "system: widget is a div/button — file_chooser_click is the correct first approach",
            })
            clean_attempts = clean_attempts[:3]  # keep max 3

        return {
            "iteration": iteration_num,
            "diagnosis": (result.get("diagnosis") or "")[:400],
            "attempts": clean_attempts,
            "capturedDom": focused_dom[:1200],
            "frameworks": frameworks,
            "screenshotB64": None,  # don't store full b64 in state — just used for LLM call
        }
    except Exception as exc:
        logger.error("llm_assist_error iteration=%d error=%s", iteration_num, str(exc))
        return {
            "iteration": iteration_num,
            "diagnosis": f"LLM call failed: {exc}",
            "attempts": [{
                "strategy": "locator_fix",
                "locator": original_locator or None,
                "js_code": None,
                "rationale": "fallback after LLM error",
            }],
            "capturedDom": focused_dom[:1200],
            "frameworks": frameworks,
            "screenshotB64": None,
        }


# ── Strategy execution ────────────────────────────────────────────────────────

async def execute_llm_strategy(
    page: Page,
    *,
    strategy: str,
    locator: str,
    js_code: str | None,
    action: str,
    params: dict[str, Any],
    tool_runtime: Any,
    tab_id: str,
    timeout_ms: float = 20_000,
    dom_snapshot_selector: str = "body",
) -> dict[str, Any]:
    """
    Execute one attempt. Captures DOM before/after and returns a diff.

    Returns:
      { success, error, exec_detail, upload_method, domDiff }
    """
    # Pre-flight: catch empty filePath before burning an LLM attempt on it
    file_path = ""
    if action in ("upload", "upload_file"):
        fp = params.get("path") or params.get("file_paths") or params.get("text") or params.get("value") or ""
        file_path = fp[0] if isinstance(fp, list) and fp else str(fp)
        if not file_path or not file_path.strip():
            return {
                "success": False,
                "error": "filePath is empty — no file path configured for this upload step",
                "exec_detail": "pre-flight check failed",
                "upload_method": "",
                "domDiff": {"changed": False, "summary": "skipped — pre-flight failed"},
            }

    # Snapshot DOM before
    dom_before = await snapshot_dom(page, dom_snapshot_selector)

    try:
        result = await _run_strategy(
            page=page,
            strategy=strategy,
            locator=locator,
            js_code=js_code,
            action=action,
            params=params,
            file_path=file_path,
            tool_runtime=tool_runtime,
            tab_id=tab_id,
            timeout_ms=timeout_ms,
        )
    except Exception as exc:
        result = {"success": False, "error": str(exc)[:300], "exec_detail": "", "upload_method": ""}

    # Snapshot DOM after and compute diff
    dom_after = await snapshot_dom(page, dom_snapshot_selector)
    dom_diff = _dom_diff_summary(dom_before, dom_after)
    result["domDiff"] = dom_diff

    logger.info(
        "llm_assist_exec strategy=%r success=%s dom_changed=%s dom_summary=%r",
        strategy, result.get("success"), dom_diff.get("changed"), dom_diff.get("summary", "")[:80],
    )
    return result


async def _run_strategy(
    *,
    page: Page,
    strategy: str,
    locator: str,
    js_code: str | None,
    action: str,
    params: dict[str, Any],
    file_path: str,
    tool_runtime: Any,
    tab_id: str,
    timeout_ms: float,
) -> dict[str, Any]:
    from pathlib import Path
    from urllib.parse import unquote

    if strategy == "file_chooser_click":
        loc = page.locator(locator).first
        fc_timeout = min(timeout_ms, 20_000.0)
        click_timeout = min(timeout_ms, 15_000.0)
        async with page.expect_file_chooser(timeout=fc_timeout) as fc_info:
            await loc.scroll_into_view_if_needed(timeout=click_timeout)
            await loc.click(timeout=click_timeout)
        chooser = await fc_info.value
        normalized = str(Path(unquote(file_path)).expanduser())
        await chooser.set_files(normalized)
        return {"success": True, "error": None, "exec_detail": f"file_chooser on {locator!r}", "upload_method": "fileChooser"}

    if strategy == "set_input_files":
        normalized = str(Path(unquote(file_path)).expanduser())
        loc = page.locator(locator).first
        await loc.set_input_files(normalized, timeout=timeout_ms)
        return {"success": True, "error": None, "exec_detail": f"setInputFiles on {locator!r}", "upload_method": "setInputFiles"}

    if strategy == "dispatch_events":
        normalized = str(Path(unquote(file_path)).expanduser())
        loc = page.locator(locator).first
        await loc.set_input_files(normalized, timeout=timeout_ms)
        try:
            await loc.dispatch_event("change")
            await loc.dispatch_event("input")
        except Exception:
            pass
        return {"success": True, "error": None, "exec_detail": f"setInputFiles+dispatch on {locator!r}", "upload_method": "dispatch_events"}

    if strategy == "js_execute":
        if not js_code:
            return {"success": False, "error": "js_execute chosen but no js_code provided", "exec_detail": "", "upload_method": ""}
        fp_normalized = str(Path(unquote(file_path)).expanduser()) if file_path else ""
        result = await page.evaluate(f"""async (filePath) => {{
            try {{
                {js_code}
                return {{ ok: true }};
            }} catch(e) {{ return {{ ok: false, error: String(e) }}; }}
        }}""", fp_normalized)
        ok = isinstance(result, dict) and result.get("ok")
        err = result.get("error") if isinstance(result, dict) else str(result)
        return {
            "success": bool(ok),
            "error": None if ok else err,
            "exec_detail": "js_execute ran",
            "upload_method": "js_execute",
        }

    if strategy == "locator_fix":
        if action in ("upload", "upload_file"):
            normalized = str(Path(unquote(file_path)).expanduser()) if file_path else file_path
            result = await tool_runtime.upload(
                tab_id=tab_id, target=locator, file_paths=normalized, timeout_ms=timeout_ms,
            )
            method = (result.details or {}).get("uploadMethod", "") if result else ""
            return {"success": True, "error": None, "exec_detail": f"locator_fix upload: {locator!r}", "upload_method": method}
        elif action == "click":
            await tool_runtime.click(tab_id=tab_id, target=locator, timeout_ms=timeout_ms)
        elif action == "fill":
            text = params.get("text") or params.get("value") or ""
            await tool_runtime.fill(tab_id=tab_id, target=locator, text=text, timeout_ms=timeout_ms)
        elif action == "type":
            text = params.get("text") or params.get("value") or ""
            await tool_runtime.type(tab_id=tab_id, target=locator, text=text, timeout_ms=timeout_ms)
        else:
            await tool_runtime.click(tab_id=tab_id, target=locator, timeout_ms=timeout_ms)
        return {"success": True, "error": None, "exec_detail": f"locator_fix {action}: {locator!r}", "upload_method": ""}

    return {"success": False, "error": f"Unknown strategy: {strategy!r}", "exec_detail": "", "upload_method": ""}


# ── JSON extraction ───────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    try:
        result = json.loads(stripped)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    stripped = re.sub(r'```(?:json)?\s*', '', stripped).strip().rstrip('`').strip()
    try:
        result = json.loads(stripped)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    match = re.search(r'\{.*\}', stripped, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group(0))
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
    return {"diagnosis": text[:300], "attempts": [], "strategy": "locator_fix", "locator": None, "js_code": None}

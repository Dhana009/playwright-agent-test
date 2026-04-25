"""LLM Assist — LLM-orchestrated repair loop for any stuck step.

The LLM is the brain:
  1. Gets full context: DOM, step, history of every attempt + exact error
  2. Returns up to 3 ordered attempts — each with exact strategy + locator + js_code
  3. System tries them one by one immediately, no user interaction
  4. First one that succeeds wins; all failures are fed back to the next LLM call
  5. User only judges at the end: worked visually or still broken?

Strategies the LLM can choose per attempt:
  - locator_fix        → change the locator, re-run the same action
  - file_chooser_click → click element to open file chooser, intercept it
  - set_input_files    → call setInputFiles directly on a file input
  - js_execute         → run custom JS snippet (LLM writes the complete code)
  - dispatch_events    → setInputFiles + dispatch change/input events
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from playwright.async_api import Page

logger = logging.getLogger(__name__)


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
            logger.info("llm_assist_dom_captured locator=%r length=%d", locator, len(result))
            return result[:3000]
        logger.warning("llm_assist_dom_missing locator=%r result=%r", locator, result[:120])
        return result or "(DOM unavailable)"
    except Exception as exc:
        return f"(DOM capture failed: {exc})"


def _build_history_block(iterations: list[dict[str, Any]]) -> str:
    """Build the history block from previous LLM rounds (each round may have sub-attempts)."""
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

        lines.append(f"\n  Attempt #{num}:")
        if strategy:
            lines.append(f"    strategy  : {strategy}")
        if locator:
            lines.append(f"    locator   : {locator!r}")
        lines.append(f"    outcome   : {outcome}")
        if error:
            lines.append(f"    error     : {error[:200]}")
        if exec_detail:
            lines.append(f"    detail    : {exec_detail[:200]}")
        if outcome == "user_failed":
            lines.append("    → User confirmed: code ran without error but the expected result did NOT happen on the page")

    lines.append("\n  ↳ Every attempt above has been tried and failed. You MUST try something different.")
    return "\n".join(lines)


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
    One LLM round. LLM analyses the situation and returns up to 3 ordered attempts.
    System will try each attempt automatically until one succeeds or all fail.

    Returns:
      {
        iteration: int,
        diagnosis: str,
        attempts: [
          { strategy, locator, js_code, rationale },
          ...
        ],
        capturedDom: str,
      }
    """
    iteration_num = len(iteration_history) + 1
    action = step.get("action", "")
    original_locator = step.get("locator", "")
    params = step.get("params") or {}

    dom_anchor = picked_locator or original_locator or "body"
    focused_dom = await capture_focused_dom(page, dom_anchor)
    history_block = _build_history_block(iteration_history)

    file_path_val = ""
    if action in ("upload", "upload_file"):
        fp = params.get("path") or params.get("file_paths") or params.get("text") or params.get("value") or ""
        file_path_val = fp[0] if isinstance(fp, list) and fp else str(fp)

    param_parts = []
    for k in ("text", "value", "key", "url", "path", "file_paths"):
        v = params.get(k)
        if v:
            param_parts.append(f"{k}={str(v)[:80]!r}")
    param_summary = ", ".join(param_parts) if param_parts else "(none)"

    upload_knowledge = ""
    if action in ("upload", "upload_file"):
        upload_knowledge = f"""
━━━ UPLOAD SYSTEM KNOWLEDGE ━━━
File to upload: {file_path_val!r}

Available strategies:
  "file_chooser_click" — click the element, intercept the OS file dialog, set file
  "set_input_files"    — call Playwright setInputFiles on a real <input type=file>
  "dispatch_events"    — setInputFiles + fire change/input events via JS
  "js_execute"         — write custom JS (DataTransfer, drag-drop, etc.)
  "locator_fix"        — re-run the standard upload action with a different locator

For js_execute: variable `filePath` is already set to {file_path_val!r}.
If no <input type=file> in DOM → use file_chooser_click or js_execute with DataTransfer.
If file_chooser_click timed out → the element doesn't open a real OS dialog → use js_execute.
"""

    non_upload_knowledge = ""
    if action not in ("upload", "upload_file"):
        non_upload_knowledge = """
━━━ AVAILABLE STRATEGIES ━━━
  "locator_fix" — target a different element, re-run the same action
  "js_execute"  — write and run custom JS in the page (you write the complete code body)
"""

    system_prompt = f"""You are an expert Playwright automation engineer fixing a broken recorded step.

━━━ BROKEN STEP ━━━
  action          : {action}
  original locator: {original_locator!r}
  params          : {param_summary}

━━━ USER'S DESCRIPTION ━━━
{issue.strip() or "(none provided)"}

━━━ LIVE DOM (real DOM on the page right now) ━━━
{focused_dom}

{history_block}
{upload_knowledge}{non_upload_knowledge}
━━━ YOUR JOB ━━━
Analyse the DOM and history. Diagnose WHY it is failing.
Then give up to 3 ordered repair attempts — best guess first.
The system will try them one by one automatically, in order, and stop at the first success.
All failures will be reported back to you with exact error messages for the next round.

━━━ EXACT OUTPUT FORMAT ━━━
Return ONLY valid JSON, exactly this shape:

{{
  "diagnosis": "<one sentence: root cause>",
  "attempts": [
    {{
      "strategy": "<file_chooser_click|set_input_files|dispatch_events|js_execute|locator_fix>",
      "locator": "<exact CSS selector or XPath from the DOM above>",
      "js_code": "<complete JS code body — only for js_execute, null otherwise>",
      "rationale": "<one line: why this specific attempt should work>"
    }},
    {{
      "strategy": "...",
      "locator": "...",
      "js_code": null,
      "rationale": "..."
    }}
  ]
}}

RULES:
• attempts array must have 1–3 items, ordered best→fallback
• locator MUST be a real selector visible in the DOM above — never invent one
• Each attempt must use a DIFFERENT strategy or DIFFERENT locator than previous attempts in history
• For js_execute: write complete self-contained JS body. Variable `filePath` is pre-set.
  Throw on failure so the system catches it. No outer function wrapper needed.
• If history shows a strategy+locator combo already failed with a specific error → do NOT repeat it
• If file_chooser_click timed out → the element doesn't open a real OS dialog → use js_execute
• Think step by step before writing your JSON
"""

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Analyse and return the JSON now."},
        ]
        response = await llm_provider.chat(messages, max_tokens=1500)
        raw = (response.content or "").strip()
        logger.info("llm_assist_raw iteration=%d raw=%r", iteration_num, raw[:600])
        result = _extract_json(raw)

        attempts = result.get("attempts") or []
        # Back-compat: LLM might still return single strategy/locator
        if not attempts and result.get("strategy"):
            attempts = [{
                "strategy": result.get("strategy"),
                "locator": result.get("locator"),
                "js_code": result.get("js_code"),
                "rationale": result.get("plan") or "",
            }]

        # Sanitise each attempt
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

        return {
            "iteration": iteration_num,
            "diagnosis": (result.get("diagnosis") or "")[:400],
            "attempts": clean_attempts,
            "capturedDom": focused_dom[:1200],
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
        }


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
) -> dict[str, Any]:
    """
    Execute one attempt. Returns:
      { success, error, exec_detail, upload_method }
    """
    file_path = ""
    if action in ("upload", "upload_file"):
        fp = params.get("path") or params.get("file_paths") or params.get("text") or params.get("value") or ""
        file_path = fp[0] if isinstance(fp, list) and fp else str(fp)

    try:
        if strategy == "file_chooser_click":
            loc = page.locator(locator).first
            fc_timeout = min(timeout_ms, 20_000.0)
            click_timeout = min(timeout_ms, 15_000.0)
            async with page.expect_file_chooser(timeout=fc_timeout) as fc_info:
                await loc.scroll_into_view_if_needed(timeout=click_timeout)
                await loc.click(timeout=click_timeout)
            chooser = await fc_info.value
            from pathlib import Path
            from urllib.parse import unquote
            normalized = str(Path(unquote(file_path)).expanduser())
            await chooser.set_files(normalized)
            return {"success": True, "error": None, "exec_detail": f"file_chooser on {locator!r}", "upload_method": "fileChooser"}

        elif strategy == "set_input_files":
            from pathlib import Path
            from urllib.parse import unquote
            normalized = str(Path(unquote(file_path)).expanduser())
            loc = page.locator(locator).first
            await loc.set_input_files(normalized, timeout=timeout_ms)
            return {"success": True, "error": None, "exec_detail": f"setInputFiles on {locator!r}", "upload_method": "setInputFiles"}

        elif strategy == "dispatch_events":
            from pathlib import Path
            from urllib.parse import unquote
            normalized = str(Path(unquote(file_path)).expanduser())
            loc = page.locator(locator).first
            await loc.set_input_files(normalized, timeout=timeout_ms)
            try:
                await loc.dispatch_event("change")
                await loc.dispatch_event("input")
            except Exception:
                pass
            return {"success": True, "error": None, "exec_detail": f"setInputFiles+dispatch on {locator!r}", "upload_method": "dispatch_events"}

        elif strategy == "js_execute":
            if not js_code:
                return {"success": False, "error": "js_execute strategy but no js_code provided", "exec_detail": "", "upload_method": ""}
            from pathlib import Path
            from urllib.parse import unquote
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

        elif strategy == "locator_fix":
            if action in ("upload", "upload_file"):
                from pathlib import Path
                from urllib.parse import unquote
                normalized = str(Path(unquote(file_path)).expanduser()) if file_path else file_path
                result = await tool_runtime.upload(
                    tab_id=tab_id,
                    target=locator,
                    file_paths=normalized,
                    timeout_ms=timeout_ms,
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

        else:
            return {"success": False, "error": f"Unknown strategy: {strategy!r}", "exec_detail": "", "upload_method": ""}

    except Exception as exc:
        return {"success": False, "error": str(exc)[:300], "exec_detail": "", "upload_method": ""}


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

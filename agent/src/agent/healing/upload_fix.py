"""LLM-powered upload fix: called per user iteration, accumulates context across calls."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import Page

logger = logging.getLogger(__name__)


@dataclass
class UploadFixIteration:
    iteration: int
    strategy: str
    explanation: str
    outcome: str | None = None  # "worked" | "failed" | None (pending)
    error: str | None = None


@dataclass
class UploadFixState:
    """Accumulated context for one upload step across multiple user-triggered iterations."""
    step_id: str
    locator: str
    file_path: str
    iterations: list[UploadFixIteration] = field(default_factory=list)
    dom_snapshot: str = ""


async def capture_upload_area_dom(page: Page, locator: str) -> str:
    """Capture the DOM around the upload target element — up to 5 levels of context."""
    try:
        snippet = await page.evaluate("""(selector) => {
            function clean(el, max) {
                if (!el) return '';
                const c = el.cloneNode(true);
                c.querySelectorAll('script,style,noscript,svg').forEach(n => n.remove());
                return c.outerHTML.slice(0, max);
            }
            let el = null;
            try { el = document.querySelector(selector); } catch(_) {}
            if (!el) {
                // Try input[type=file] anywhere near visible upload UIs
                const candidates = document.querySelectorAll(
                    'input[type=file],[data-testid*=upload],[data-testid*=drop],[class*=upload],[class*=dropzone]'
                );
                if (candidates.length) el = candidates[0];
            }
            if (!el) return document.body ? clean(document.body, 3000) : '';
            // Walk up 5 levels for full upload widget context
            let ctx = el;
            for (let i = 0; i < 5 && ctx.parentElement && ctx.parentElement.tagName !== 'BODY'; i++) {
                ctx = ctx.parentElement;
            }
            return clean(ctx, 6000);
        }""", locator)
        return str(snippet or "").strip()
    except Exception as exc:
        logger.debug("upload_dom_capture_error error=%s", str(exc))
        return ""


def _build_iteration_history(state: UploadFixState) -> str:
    if not state.iterations:
        return ""
    lines = ["## Prior iteration history (oldest first)"]
    for it in state.iterations:
        outcome = it.outcome or "not yet run"
        lines.append(f"\n### Iteration {it.iteration}")
        lines.append(f"Strategy proposed: {it.strategy}")
        lines.append(f"Explanation: {it.explanation}")
        lines.append(f"Outcome: {outcome}" + (f" — error: {it.error}" if it.error else ""))
    return "\n".join(lines)


async def run_upload_fix_iteration(
    page: Page,
    *,
    state: UploadFixState,
    llm_provider: Any,
) -> dict[str, Any]:
    """
    One LLM iteration for upload fix. Returns:
      { strategy, explanation, error }
    The caller is responsible for executing the strategy and recording outcome.
    """
    iteration_num = len(state.iterations) + 1

    # Refresh DOM snapshot each iteration — page may have changed
    dom_snapshot = await capture_upload_area_dom(page, state.locator)
    state.dom_snapshot = dom_snapshot

    history_block = _build_iteration_history(state)

    file_name = Path(state.file_path).name if state.file_path else ""

    system_prompt = f"""You are an expert Playwright automation engineer specialising in file upload UI patterns.

SITUATION:
A recorded test step for action=upload ran and Playwright reported success (no exception),
but the file was NOT actually uploaded — the UI did not change to show the file.

YOUR JOB:
Analyse the upload widget DOM and propose one concrete Playwright strategy to actually upload the file.

FILE TO UPLOAD:
  path: {state.file_path!r}
  name: {file_name!r}

RECORDED LOCATOR (may be wrong or pointing to wrong element):
  {state.locator!r}

DOM AROUND THE UPLOAD AREA:
{dom_snapshot[:4500] or "(unavailable)"}

{history_block}

RULES:
1. Output ONLY a JSON object with exactly these keys:
   - "strategy": one of: "set_input_files_direct" | "file_chooser_click" | "drag_and_drop_dataTransfer" | "js_dispatch_change" | "js_set_files_and_dispatch" | "other"
   - "locator": the exact CSS/XPath selector to target (the real <input type=file> or the dropzone)
   - "explanation": 1-2 sentences describing what is wrong and why this strategy will fix it
   - "js_code": for strategy="other" or "js_dispatch_change"/"js_set_files_and_dispatch", a self-contained JS snippet (receives filePath as argument); omit or null for other strategies
2. Prefer these strategies in order:
   a) If a hidden <input type=file> exists: target it directly with set_input_files_direct
   b) If clicking triggers a file chooser dialog: use file_chooser_click
   c) If it is a drag-and-drop zone with no <input type=file>: use drag_and_drop_dataTransfer
   d) If the input exists but needs events fired after: use js_set_files_and_dispatch
3. If iteration history shows a strategy already failed, choose a different one.
4. Be specific about which exact element to target.
"""

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Analyse the DOM and return the JSON strategy object now."},
        ]
        response = await llm_provider.chat(messages, max_tokens=1024)
        raw = response.content or ""
        logger.debug("upload_fix_llm_raw iteration=%d length=%d", iteration_num, len(raw))
        result = _extract_json(raw)
        return {
            "iteration": iteration_num,
            "strategy": result.get("strategy", "other"),
            "locator": result.get("locator", state.locator),
            "explanation": result.get("explanation", raw[:300]),
            "js_code": result.get("js_code") or None,
        }
    except Exception as exc:
        logger.error("upload_fix_llm_error iteration=%d error=%s", iteration_num, str(exc))
        return {
            "iteration": iteration_num,
            "strategy": "other",
            "locator": state.locator,
            "explanation": f"LLM call failed: {exc}",
            "js_code": None,
        }


async def execute_upload_fix_strategy(
    page: Page,
    *,
    strategy: str,
    locator: str,
    file_path: str,
    js_code: str | None,
    timeout_ms: float = 15_000,
) -> tuple[bool, str | None]:
    """
    Execute the LLM-proposed upload strategy.
    Returns (success, error_message).
    """
    normalized = str(Path(file_path).expanduser())

    try:
        if strategy == "set_input_files_direct":
            loc = page.locator(locator).first
            await loc.set_input_files(normalized, timeout=timeout_ms)
            return True, None

        elif strategy == "file_chooser_click":
            fc_timeout = min(timeout_ms, 20_000.0)
            click_timeout = min(timeout_ms, 15_000.0)
            loc = page.locator(locator).first
            async with page.expect_file_chooser(timeout=fc_timeout) as fc_info:
                await loc.scroll_into_view_if_needed(timeout=click_timeout)
                await loc.click(timeout=click_timeout)
            chooser = await fc_info.value
            await chooser.set_files(normalized)
            return True, None

        elif strategy == "drag_and_drop_dataTransfer":
            # Simulate dropping a file onto a dropzone using DataTransfer API
            result = await page.evaluate("""async ([selector, path]) => {
                const el = document.querySelector(selector);
                if (!el) return { ok: false, error: 'element not found: ' + selector };
                try {
                    const resp = await fetch('file://' + path).catch(() => null);
                    // DataTransfer with file path can't cross origin — fire dragover+drop events
                    // so the app's own handler processes it. The file contents aren't available
                    // this way; this only works for apps that intercept drop and open a file picker.
                    const dt = new DataTransfer();
                    el.dispatchEvent(new DragEvent('dragenter', { bubbles: true, dataTransfer: dt }));
                    el.dispatchEvent(new DragEvent('dragover', { bubbles: true, dataTransfer: dt }));
                    el.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: dt }));
                    return { ok: true };
                } catch(e) { return { ok: false, error: String(e) }; }
            }""", [locator, normalized])
            ok = isinstance(result, dict) and result.get("ok")
            err = result.get("error") if isinstance(result, dict) else str(result)
            return ok, None if ok else err

        elif strategy in ("js_dispatch_change", "js_set_files_and_dispatch", "other"):
            if js_code:
                result = await page.evaluate(f"""async (filePath) => {{
                    try {{
                        {js_code}
                        return {{ ok: true }};
                    }} catch(e) {{ return {{ ok: false, error: String(e) }}; }}
                }}""", normalized)
                ok = isinstance(result, dict) and result.get("ok")
                err = result.get("error") if isinstance(result, dict) else str(result)
                return ok, None if ok else err
            # Fallback: try set_input_files on locator
            loc = page.locator(locator).first
            await loc.set_input_files(normalized, timeout=timeout_ms)
            return True, None

        else:
            return False, f"Unknown strategy: {strategy}"

    except Exception as exc:
        return False, str(exc)


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
    return {"strategy": "other", "explanation": text[:400]}

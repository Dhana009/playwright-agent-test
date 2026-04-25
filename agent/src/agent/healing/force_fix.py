"""Force-fix cascade: 4-stage deterministic + LLM healing for failing steps."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from playwright.async_api import Page

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, str, bool, str | None, str | None], Coroutine[Any, Any, None]]


@dataclass
class ForceFixResult:
    repaired: bool
    locator: str | None = None
    stage: int = 0
    explanation: str | None = None
    candidates_tried: list[str] = field(default_factory=list)


async def verify_llm_connection(llm_provider: Any) -> tuple[bool, str]:
    """
    Send a minimal probe message to confirm the LLM API key + endpoint actually work.
    Returns (success, message).
    """
    try:
        response = await llm_provider.chat(
            [{"role": "user", "content": "Reply with the single word: ready"}],
            max_tokens=10,
        )
        text = (response.content or "").strip().lower()
        if text:
            return True, f"Connected · model responded: {(response.content or '').strip()[:40]}"
        return False, "API responded but returned empty content"
    except Exception as exc:
        err = str(exc)
        # Surface the most useful part of the error message
        if "api_key" in err.lower() or "authentication" in err.lower() or "401" in err:
            return False, "Authentication failed — check your API key"
        if "429" in err or "rate_limit" in err.lower():
            return False, "Rate limited — try again in a moment"
        if "model" in err.lower() and "not found" in err.lower():
            return False, f"Model not found — check model name"
        if "connect" in err.lower() or "network" in err.lower() or "timeout" in err.lower():
            return False, "Network error — check your connection"
        return False, f"LLM error: {err[:120]}"


async def run_force_fix_cascade(
    page: Page,
    *,
    step_id: str,
    action: str,
    primary_selector: str,
    fallback_selectors: list[str],
    target_descriptor: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    llm_provider: Any | None = None,
    on_progress: ProgressCallback | None = None,
) -> ForceFixResult:
    """
    4-stage force-fix cascade.

    Stage 1: Retry primary selector with a brief wait (transient timing issues).
    Stage 2: Try fallback selectors from the locator bundle.
    Stage 3: Generate semantic alternates from the target descriptor (testid, aria, role, text, placeholder, name, id).
    Stage 4: LLM — sends full DOM context + all failed selectors → returns ranked candidates, probes each.
    """

    async def notify(
        stage: int,
        status: str,
        repaired: bool = False,
        locator: str | None = None,
        explanation: str | None = None,
    ) -> None:
        if on_progress:
            await on_progress(stage, status, repaired, locator, explanation)

    tried: list[str] = [primary_selector] if primary_selector else []

    # ── Stage 1: retry primary with wait ─────────────────────────────────
    await notify(1, "running")
    await asyncio.sleep(0.5)
    if primary_selector and await _probe_selector(page, primary_selector):
        await notify(1, "pass", repaired=True, locator=primary_selector)
        return ForceFixResult(repaired=True, locator=primary_selector, stage=1)
    # Try again after a longer wait (element might be loading)
    await asyncio.sleep(1.0)
    if primary_selector and await _probe_selector(page, primary_selector):
        await notify(1, "pass", repaired=True, locator=primary_selector, explanation="Resolved after waiting for page load")
        return ForceFixResult(repaired=True, locator=primary_selector, stage=1)
    await notify(1, "fail")

    # ── Stage 2: fallback selectors ───────────────────────────────────────
    await notify(2, "running")
    for selector in fallback_selectors:
        if not selector or not selector.strip():
            continue
        if selector not in tried:
            tried.append(selector)
        if await _probe_selector(page, selector):
            await notify(2, "pass", repaired=True, locator=selector)
            return ForceFixResult(repaired=True, locator=selector, stage=2, candidates_tried=tried)
    await notify(2, "fail")

    # ── Stage 3: semantic alternates ──────────────────────────────────────
    await notify(3, "running")
    if target_descriptor:
        alternates = _generate_alternates(target_descriptor)
        for selector in alternates:
            if not selector:
                continue
            if selector not in tried:
                tried.append(selector)
            if await _probe_selector(page, selector):
                await notify(3, "pass", repaired=True, locator=selector)
                return ForceFixResult(repaired=True, locator=selector, stage=3, candidates_tried=tried)
    await notify(3, "fail")

    # ── Stage 4: LLM ─────────────────────────────────────────────────────
    if llm_provider is None:
        logger.info("force_fix_no_llm step_id=%s", step_id)
        return ForceFixResult(repaired=False, stage=3, candidates_tried=tried)

    await notify(4, "running")
    try:
        dom_snippet = await _capture_dom_snippet(page, primary_selector, target_descriptor)
        llm_result = await _ask_llm_repair(
            llm_provider=llm_provider,
            failed_locator=primary_selector,
            already_tried=tried,
            action=action,
            dom_snippet=dom_snippet,
            target_descriptor=target_descriptor,
            params=params or {},
        )

        candidates = llm_result.get("candidates") or []
        # Support both {"locator": "..."} and {"candidates": [...]} shapes
        if not candidates and llm_result.get("locator"):
            candidates = [llm_result["locator"]]
        explanation = llm_result.get("explanation", "")

        working_locator: str | None = None
        for candidate in candidates:
            if not candidate or not isinstance(candidate, str):
                continue
            candidate = candidate.strip()
            if candidate not in tried:
                tried.append(candidate)
            if await _probe_selector(page, candidate):
                working_locator = candidate
                break

        if working_locator:
            await notify(4, "pass", repaired=True, locator=working_locator, explanation=explanation)
            return ForceFixResult(repaired=True, locator=working_locator, stage=4, explanation=explanation, candidates_tried=tried)

        # None of the LLM candidates worked
        best_candidate = candidates[0] if candidates else None
        fail_msg = explanation or "LLM suggestions did not match any visible element on the page."
        if best_candidate:
            fail_msg = f"Best candidate '{best_candidate}' not found. {fail_msg}"
        await notify(4, "fail", locator=best_candidate, explanation=fail_msg)
        return ForceFixResult(repaired=False, stage=4, locator=best_candidate, explanation=fail_msg, candidates_tried=tried)

    except Exception as exc:
        logger.error("force_fix_llm_error step_id=%s error=%s", step_id, str(exc))
        err_msg = f"LLM call failed: {exc}"
        await notify(4, "fail", explanation=err_msg)
        return ForceFixResult(repaired=False, stage=4, candidates_tried=tried)


async def _probe_selector(page: Page, selector: str) -> bool:
    """Return True if the selector finds at least one visible, actionable element."""
    try:
        loc = page.locator(selector)
        count = await loc.count()
        if count == 0:
            return False
        # Check the first match — visible is enough
        return await loc.first.is_visible()
    except Exception:
        return False


def _generate_alternates(descriptor: dict[str, Any]) -> list[str]:
    """
    Generate semantic alternate selectors in priority order:
    data-testid → aria-label → role+text → placeholder → name → id → text → tag+text
    """
    alternates: list[str] = []
    tag = descriptor.get("tag", "").lower()
    text = (descriptor.get("text") or "").strip()
    aria_label = descriptor.get("ariaLabel") or descriptor.get("aria_label") or ""
    role = descriptor.get("role") or ""
    placeholder = descriptor.get("placeholder") or ""
    name = descriptor.get("name") or ""
    el_id = descriptor.get("id") or ""
    testid = descriptor.get("testid") or ""

    # 1. data-testid (most stable)
    if testid:
        alternates.append(f'[data-testid="{testid}"]')
        alternates.append(f'[data-test-id="{testid}"]')
        alternates.append(f'[data-qa="{testid}"]')

    # 2. aria-label
    if aria_label:
        alternates.append(f'[aria-label="{aria_label}"]')
        if tag:
            alternates.append(f'{tag}[aria-label="{aria_label}"]')

    # 3. role + accessible name
    if role and aria_label:
        alternates.append(f'[role="{role}"][aria-label="{aria_label}"]')
    if role and text and len(text) > 1:
        safe_text = text[:60]
        alternates.append(f'[role="{role}"]:has-text("{safe_text}")')

    # 4. Playwright getBy-style (most readable and stable)
    if tag in ("button", "a") and text and len(text) > 1:
        alternates.append(f'{tag}:has-text("{text[:60]}")')
        alternates.append(f'text="{text[:60]}"')
    elif text and len(text) > 2:
        alternates.append(f'text="{text[:60]}"')

    # 5. placeholder (great for inputs)
    if placeholder:
        alternates.append(f'[placeholder="{placeholder}"]')
        if tag:
            alternates.append(f'{tag}[placeholder="{placeholder}"]')

    # 6. name attribute
    if name:
        alternates.append(f'[name="{name}"]')
        if tag:
            alternates.append(f'{tag}[name="{name}"]')

    # 7. id
    if el_id:
        safe_id = re.sub(r'[^\w-]', '', el_id)
        if safe_id:
            alternates.append(f'#{safe_id}')
            alternates.append(f'[id="{el_id}"]')

    return alternates


async def _capture_dom_snippet(
    page: Page,
    failed_selector: str,
    descriptor: dict[str, Any] | None = None,
) -> str:
    """
    Capture rich DOM context for LLM repair:
    - Try to find the element by its failed selector first
    - If not found, try to find it by text/aria-label from the descriptor
    - Fall back to a broad body slice
    - Returns up to 6000 chars of clean HTML (attributes preserved, scripts stripped)
    """
    try:
        snippet = await page.evaluate("""([selector, descriptor]) => {
            function cleanHtml(el, maxLen) {
                if (!el) return '';
                // Clone to avoid mutating live DOM
                const clone = el.cloneNode(true);
                // Remove script/style/noscript noise
                clone.querySelectorAll('script,style,noscript,svg').forEach(n => n.remove());
                return clone.outerHTML.slice(0, maxLen);
            }

            // Try primary selector
            let el = null;
            try { el = document.querySelector(selector); } catch(_) {}

            // Try text-based fallback from descriptor
            if (!el && descriptor) {
                const text = (descriptor.text || '').trim().slice(0, 50);
                const ariaLabel = descriptor.ariaLabel || descriptor.aria_label || '';
                const testid = descriptor.testid || '';
                if (testid) {
                    try { el = document.querySelector('[data-testid="' + testid + '"]'); } catch(_) {}
                }
                if (!el && ariaLabel) {
                    try { el = document.querySelector('[aria-label="' + ariaLabel + '"]'); } catch(_) {}
                }
                if (!el && text) {
                    // Walk all elements looking for text match
                    const all = document.querySelectorAll('button,a,input,select,textarea,label,[role]');
                    for (const node of all) {
                        if ((node.innerText || node.textContent || '').trim().startsWith(text.slice(0, 20))) {
                            el = node; break;
                        }
                    }
                }
            }

            if (el) {
                // Get 3 levels up for context
                let ctx = el;
                for (let i = 0; i < 3 && ctx.parentElement && ctx.parentElement.tagName !== 'BODY'; i++) {
                    ctx = ctx.parentElement;
                }
                return cleanHtml(ctx, 6000);
            }

            // Last resort: first 4000 chars of body
            return document.body ? cleanHtml(document.body, 4000) : '';
        }""", [failed_selector, descriptor or {}])
        return str(snippet or "").strip()
    except Exception as exc:
        logger.debug("dom_snippet_error error=%s", str(exc))
        return ""


def _extract_llm_json(text: str) -> dict[str, Any]:
    """Robustly extract a JSON object from an LLM response, handling markdown fences."""
    stripped = text.strip()

    # Direct parse
    try:
        result = json.loads(stripped)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Strip markdown code fences
    stripped = re.sub(r'```(?:json)?\s*', '', stripped).strip().rstrip('`').strip()
    try:
        result = json.loads(stripped)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Greedy JSON object regex
    match = re.search(r'\{.*\}', stripped, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group(0))
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    return {"candidates": [], "explanation": text[:600]}


async def _ask_llm_repair(
    llm_provider: Any,
    *,
    failed_locator: str,
    already_tried: list[str],
    action: str,
    dom_snippet: str,
    target_descriptor: dict[str, Any] | None,
    params: dict[str, Any],
) -> dict[str, Any]:
    """
    Ask the LLM to repair a failing locator.

    The prompt gives full context: what failed, what was already tried,
    the target element description, and the live DOM. The LLM returns
    an ordered list of candidate selectors to try.
    """
    desc_parts = []
    if target_descriptor:
        for key in ("tag", "text", "ariaLabel", "role", "placeholder", "testid", "name", "id"):
            val = target_descriptor.get(key) or target_descriptor.get(key.lower()) or ""
            if val:
                desc_parts.append(f"  {key}: {val!r}")
    desc_block = "\n".join(desc_parts) if desc_parts else "  (no descriptor available)"

    param_block = ""
    if params:
        relevant = {k: v for k, v in params.items() if v and k in ("text", "value", "key", "url")}
        if relevant:
            param_block = f"\nAction params: {json.dumps(relevant)}"

    tried_block = "\n".join(f"  - {s}" for s in already_tried if s) or "  (none)"

    prompt = f"""You are a senior Playwright test automation engineer. A recorded browser step has broken because its locator no longer finds the element.

## What failed
Action: {action}{param_block}
Failed selector: {failed_locator!r}

## Target element (recorded properties)
{desc_block}

## Selectors already tried (all failed)
{tried_block}

## Live DOM around the target area
```html
{dom_snippet[:5000]}
```

## Your task
Examine the DOM above and identify the element that best matches the target description.
Return a JSON object with:
- "candidates": an ordered array of up to 5 Playwright selector strings, best first
- "explanation": a short explanation of what changed and why your top candidate should work

Selector priority (use the first one that applies):
1. data-testid / data-test-id / data-qa attribute
2. aria-label attribute
3. role + accessible name: role("button", {{ name: "Submit" }})
4. Playwright text locator: text="Submit"
5. placeholder attribute for inputs
6. name attribute
7. CSS id selector: #element-id
8. Scoped CSS (last resort, avoid generic class names)
9. XPath (absolute last resort only)

IMPORTANT:
- Do NOT repeat selectors from the "already tried" list
- Do NOT use CSS class names that look auto-generated (hash-like, e.g. "sc-abc123", "css-xyz")
- Prefer selectors that uniquely identify ONE element
- Return ONLY the JSON object, no markdown, no explanation outside the JSON

Example response:
{{"candidates": ["[data-testid=\\"submit-btn\\"]", "button:has-text(\\"Submit\\")", "[aria-label=\\"Submit form\\"]"], "explanation": "The button's class changed but data-testid is still present in the DOM."}}"""

    try:
        messages = [{"role": "user", "content": prompt}]
        response = await llm_provider.chat(messages, max_tokens=8192)
        response_text = response.content or ""
        logger.debug("llm_repair_raw_response length=%d", len(response_text))
        result = _extract_llm_json(response_text)
        # Normalize: if LLM returned {"locator": ...} shape, convert to candidates list
        if "locator" in result and "candidates" not in result:
            loc = result.get("locator")
            result["candidates"] = [loc] if loc else []
        return result
    except Exception as exc:
        logger.error("llm_repair_error error=%s", str(exc))
        return {"candidates": [], "explanation": f"LLM call failed: {exc}"}

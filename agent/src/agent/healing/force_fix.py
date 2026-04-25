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

ProgressCallback = Callable[[int, str, bool, str | None, str | None, dict[str, Any] | None], Coroutine[Any, Any, None]]


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
            return False, "Model not found — check model name"
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
    user_hint: str | None = None,
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
        meta: dict[str, Any] | None = None,
    ) -> None:
        if on_progress:
            await on_progress(stage, status, repaired, locator, explanation, meta)

    tried: list[str] = [primary_selector] if primary_selector else []
    deterministic_passes: list[tuple[int, str]] = []

    # ── Stage 1: retry primary with wait ─────────────────────────────────
    await notify(
        1,
        "running",
        explanation="Trying current locator with short retry window.",
        meta={"activeCandidate": primary_selector or "", "stageAttempt": 1},
    )
    stage1_passed = False
    await asyncio.sleep(0.5)
    if primary_selector and await _probe_selector(page, primary_selector):
        stage1_passed = True
        deterministic_passes.append((1, primary_selector))
        await notify(
            1,
            "pass",
            locator=primary_selector,
            explanation="Primary locator matched. Continuing to evaluate stronger deterministic options.",
        )
    if not stage1_passed:
        # Try again after a longer wait (element might be loading)
        await asyncio.sleep(1.0)
        if primary_selector and await _probe_selector(page, primary_selector):
            stage1_passed = True
            deterministic_passes.append((1, primary_selector))
            await notify(
                1,
                "pass",
                locator=primary_selector,
                explanation="Primary locator matched after waiting. Continuing to evaluate stronger deterministic options.",
            )
    if not stage1_passed:
        await notify(1, "fail")

    # ── Stage 2: fallback selectors ───────────────────────────────────────
    await notify(
        2,
        "running",
        explanation=f"Trying fallback locators ({len([s for s in fallback_selectors if s])} candidates).",
    )
    stage2_passed = False
    for idx, selector in enumerate(fallback_selectors, start=1):
        if not selector or not selector.strip():
            continue
        await notify(
            2,
            "running",
            explanation=f"Fallback {idx}: trying {selector[:140]}",
            meta={"activeCandidate": selector, "stageAttempt": idx},
        )
        if selector not in tried:
            tried.append(selector)
        if await _probe_selector(page, selector):
            stage2_passed = True
            if (2, selector) not in deterministic_passes:
                deterministic_passes.append((2, selector))
            await notify(2, "pass", locator=selector)
    if not stage2_passed:
        await notify(2, "fail")

    # ── Stage 3: semantic alternates ──────────────────────────────────────
    await notify(3, "running", explanation="Generating semantic alternates from descriptor.")
    stage3_passed = False
    if target_descriptor:
        alternates = _generate_alternates(target_descriptor)
        for idx, selector in enumerate(alternates, start=1):
            if not selector:
                continue
            await notify(
                3,
                "running",
                explanation=f"Alternate {idx}: trying {selector[:140]}",
                meta={"activeCandidate": selector, "stageAttempt": idx},
            )
            if selector not in tried:
                tried.append(selector)
            if await _probe_selector(page, selector):
                stage3_passed = True
                if (3, selector) not in deterministic_passes:
                    deterministic_passes.append((3, selector))
                await notify(3, "pass", locator=selector)
    if not stage3_passed:
        await notify(3, "fail")

    if deterministic_passes:
        best_stage, best_locator = _select_best_deterministic_candidate(deterministic_passes)
        best_score = _selector_stability_score(best_locator)
        await notify(
            best_stage,
            "pass",
            repaired=True,
            locator=best_locator,
            explanation=(
                "Evaluated stages 1-3 and selected the strongest deterministic locator. "
                f"stability_score={best_score}"
            ),
            meta={"selectedBy": "deterministic_scoring", "stabilityScore": best_score},
        )
        return ForceFixResult(
            repaired=True,
            locator=best_locator,
            stage=best_stage,
            explanation=f"Selected best deterministic locator (score={best_score}).",
            candidates_tried=tried,
        )

    # ── Stage 4: LLM ─────────────────────────────────────────────────────
    if llm_provider is None:
        logger.info("force_fix_no_llm step_id=%s", step_id)
        explanation = (
            "Auto-fix tried waiting, fallback selectors, and semantic alternates, "
            "but none matched a visible element. LLM repair is not configured, so "
            "there is no stage 4 diagnosis available. Configure an LLM or use Manual fix."
        )
        await notify(
            4,
            "fail",
            explanation=explanation,
            meta={"llmCalled": False, "verificationStatus": "not_run"},
        )
        return ForceFixResult(repaired=False, stage=4, explanation=explanation, candidates_tried=tried)

    await notify(4, "running", meta={"llmCalled": True, "verificationStatus": "running"})
    try:
        dom_snippet = await _capture_dom_snippet(page, primary_selector, target_descriptor)
        dom_diagnosis = await _diagnose_dom_presence(page, primary_selector, target_descriptor)
        llm_request = {
            "action": action,
            "failedLocator": primary_selector,
            "alreadyTried": [s for s in tried if s][:24],
            "targetDescriptor": target_descriptor or {},
            "params": params or {},
            "userHint": (user_hint or "")[:1200],
            "domSnippetPreview": (dom_snippet or "")[:1600],
            "domDiagnosis": dom_diagnosis,
        }
        input_summary = {
            "action": action,
            "failedLocator": primary_selector,
            "alreadyTriedCount": len([s for s in tried if s]),
            "descriptorKeys": sorted((target_descriptor or {}).keys()),
            "domChars": len(dom_snippet or ""),
            "hasUserHint": bool(user_hint and user_hint.strip()),
            "domStatus": dom_diagnosis.get("status", "inconclusive"),
        }
        await notify(
            4,
            "running",
            meta={
                "llmCalled": True,
                "verificationStatus": "running",
                "inputSummary": input_summary,
                "domDiagnosis": dom_diagnosis,
                "llmRequest": llm_request,
            },
            explanation="Calling LLM with DOM context and prior attempts.",
        )
        llm_result = await _ask_llm_repair(
            llm_provider=llm_provider,
            failed_locator=primary_selector,
            already_tried=tried,
            action=action,
            dom_snippet=dom_snippet,
            target_descriptor=target_descriptor,
            params=params or {},
            user_hint=user_hint,
            dom_diagnosis=dom_diagnosis,
        )

        candidates = llm_result.get("candidates") or []
        # Support both {"locator": "..."} and {"candidates": [...]} shapes
        if not candidates and llm_result.get("locator"):
            candidates = [llm_result["locator"]]
        explanation = llm_result.get("explanation", "")
        llm_response = {
            "candidateCount": len([c for c in candidates if isinstance(c, str) and c.strip()]),
            "topCandidates": [c for c in candidates if isinstance(c, str) and c.strip()][:5],
            "explanation": str(explanation or "")[:280],
            "domStatus": llm_result.get("domStatus") or dom_diagnosis.get("status", "inconclusive"),
            "rawResponsePreview": str(llm_result.get("_raw_response_preview") or "")[:1200],
        }

        working_locator: str | None = None
        for idx, candidate in enumerate(candidates, start=1):
            if not candidate or not isinstance(candidate, str):
                continue
            candidate = candidate.strip()
            await notify(
                4,
                "running",
                explanation=f"LLM candidate {idx}: verifying {candidate[:140]}",
                meta={
                    "llmCalled": True,
                    "verificationStatus": "running",
                    "inputSummary": input_summary,
                    "stageAttempt": idx,
                    "activeCandidate": candidate,
                    "domDiagnosis": dom_diagnosis,
                    "llmRequest": llm_request,
                    "llmResponse": llm_response,
                },
            )
            if candidate not in tried:
                tried.append(candidate)
            if await _probe_selector(page, candidate):
                working_locator = candidate
                break

        if working_locator:
            await notify(
                4,
                "pass",
                repaired=True,
                locator=working_locator,
                explanation=explanation,
                meta={
                    "llmCalled": True,
                    "verificationStatus": "verified",
                    "inputSummary": input_summary,
                    "domDiagnosis": dom_diagnosis,
                    "llmRequest": llm_request,
                    "llmResponse": llm_response,
                },
            )
            return ForceFixResult(repaired=True, locator=working_locator, stage=4, explanation=explanation, candidates_tried=tried)

        # None of the LLM candidates worked
        best_candidate = candidates[0] if candidates else None
        fail_msg = explanation or "LLM suggestions did not match any visible element on the page."
        if dom_diagnosis.get("status") == "likely_missing":
            fail_msg = (
                "Element appears missing from current DOM: "
                + str(dom_diagnosis.get("message") or "").strip()
            )
        if best_candidate:
            fail_msg = f"Best candidate '{best_candidate}' not found. {fail_msg}"
        await notify(
            4,
            "fail",
            locator=best_candidate,
            explanation=fail_msg,
            meta={
                "llmCalled": True,
                "verificationStatus": "not_verified",
                "inputSummary": input_summary,
                "domDiagnosis": dom_diagnosis,
                "llmRequest": llm_request,
                "llmResponse": llm_response,
            },
        )
        return ForceFixResult(repaired=False, stage=4, locator=best_candidate, explanation=fail_msg, candidates_tried=tried)

    except Exception as exc:
        logger.error("force_fix_llm_error step_id=%s error=%s", step_id, str(exc))
        err_msg = f"LLM call failed: {exc}"
        await notify(
            4,
            "fail",
            explanation=err_msg,
            meta={"llmCalled": True, "verificationStatus": "error"},
        )
        return ForceFixResult(repaired=False, stage=4, candidates_tried=tried)


async def _probe_selector(page: Page, selector: str) -> bool:
    """Return True if the selector finds exactly one visible element (unique match)."""
    try:
        loc = page.locator(selector)
        count = await loc.count()
        if count == 0:
            return False
        if count > 1:
            # Non-unique selector — skip it, it could click the wrong element
            logger.debug("probe_not_unique selector=%r count=%d", selector, count)
            return False
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

    # 8. XPath — last resort before LLM
    if text and len(text) > 1:
        safe_text = text[:60].replace("'", "\\'")
        if tag:
            alternates.append(f"//{tag}[normalize-space(.)='{safe_text}']")
            alternates.append(f"//{tag}[contains(.,'{safe_text[:30]}')]")
        else:
            alternates.append(f"//*[normalize-space(.)='{safe_text}']")
    if aria_label:
        safe_aria = aria_label.replace("'", "\\'")
        xp_tag = tag or "*"
        alternates.append(f"//{xp_tag}[@aria-label='{safe_aria}']")

    return alternates


def _selector_stability_score(selector: str) -> int:
    s = (selector or "").strip().lower()
    if not s:
        return -100
    score = 0
    if "data-testid" in s or "data-test-id" in s or "data-qa" in s:
        score += 60
    if "aria-label" in s:
        score += 45
    if "[role=" in s or "role(" in s:
        score += 35
    if "[name=" in s:
        score += 25
    if "[placeholder=" in s:
        score += 22
    if s.startswith("#") or "[id=" in s:
        score += 18
    if "has-text(" in s or s.startswith('text="'):
        score += 16
    if s.startswith("//") or s.startswith("xpath="):
        score -= 18
    if "nth-child" in s or "nth=" in s:
        score -= 22
    if re.search(r"\.[a-z0-9]{6,}$", s):
        score -= 10
    score -= min(len(s) // 40, 8)
    return score


def _select_best_deterministic_candidate(passes: list[tuple[int, str]]) -> tuple[int, str]:
    # De-duplicate by selector and keep earliest stage occurrence.
    first_stage_by_selector: dict[str, int] = {}
    for stage, selector in passes:
        if selector not in first_stage_by_selector:
            first_stage_by_selector[selector] = stage
    best_stage = 0
    best_selector = ""
    best_score = -10_000
    for selector, stage in first_stage_by_selector.items():
        score = _selector_stability_score(selector)
        # Tie-breaker: prefer earlier stage if score equal.
        if score > best_score or (score == best_score and (best_stage == 0 or stage < best_stage)):
            best_score = score
            best_stage = stage
            best_selector = selector
    return best_stage, best_selector


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


async def _diagnose_dom_presence(
    page: Page,
    failed_selector: str,
    descriptor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Conservative DOM diagnosis to avoid false 'element missing' alarms."""
    try:
        result = await page.evaluate("""([selector, descriptor]) => {
            const out = {
                status: "inconclusive",
                selectorMatches: 0,
                strongSignalMatches: 0,
                textSignalMatches: 0,
                hasDescriptorSignals: false,
                message: "",
            };
            const safeCount = (sel) => {
                if (!sel) return 0;
                try { return document.querySelectorAll(sel).length; } catch (_) { return 0; }
            };

            out.selectorMatches = safeCount(selector);
            const testid = (descriptor && (descriptor.testid || descriptor.testId)) || "";
            const aria = (descriptor && (descriptor.ariaLabel || descriptor.aria_label)) || "";
            const elId = (descriptor && descriptor.id) || "";
            const name = (descriptor && descriptor.name) || "";
            const text = ((descriptor && descriptor.text) || "").trim();
            const hasSignals = Boolean(testid || aria || elId || name || text);
            out.hasDescriptorSignals = hasSignals;

            let strong = 0;
            if (testid) {
                strong += safeCount('[data-testid="' + testid + '"]');
                strong += safeCount('[data-test-id="' + testid + '"]');
                strong += safeCount('[data-qa="' + testid + '"]');
            }
            if (aria) strong += safeCount('[aria-label="' + aria + '"]');
            if (elId) strong += safeCount('[id="' + elId + '"]');
            if (name) strong += safeCount('[name="' + name + '"]');
            out.strongSignalMatches = strong;

            let textMatches = 0;
            if (text.length >= 3) {
                const wanted = text.slice(0, 40).toLowerCase();
                const nodes = document.querySelectorAll('button,a,input,select,textarea,label,[role]');
                for (const node of nodes) {
                    const t = (node.innerText || node.textContent || '').trim().toLowerCase();
                    if (t && t.includes(wanted)) textMatches += 1;
                }
            }
            out.textSignalMatches = textMatches;

            if (out.selectorMatches > 0 || out.strongSignalMatches > 0 || out.textSignalMatches > 0) {
                out.status = "present_or_shifted";
                out.message = "Some target signals are still present in DOM, but locator strategy likely changed.";
                return out;
            }

            if (hasSignals) {
                out.status = "likely_missing";
                out.message = "No target signals found in current DOM. The expected element is likely not rendered on this page/state.";
                return out;
            }

            out.status = "inconclusive";
            out.message = "Target descriptor is sparse; cannot confidently determine DOM absence.";
            return out;
        }""", [failed_selector, descriptor or {}])
        if isinstance(result, dict):
            return result
        return {"status": "inconclusive", "message": "DOM diagnosis unavailable."}
    except Exception as exc:
        logger.debug("dom_diagnosis_error error=%s", str(exc))
        return {"status": "inconclusive", "message": "DOM diagnosis failed."}


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
    user_hint: str | None = None,
    dom_diagnosis: dict[str, Any] | None = None,
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
    hint_block = f"\n## User context\n{user_hint.strip()}\n" if user_hint and user_hint.strip() else ""

    context_payload = {
        "action": action,
        "failed_selector": failed_locator,
        "action_params": {k: v for k, v in params.items() if v and k in ("text", "value", "key", "url")},
        "target_descriptor": target_descriptor or {},
        "already_tried_selectors": [s for s in already_tried if s],
        "user_hint": (user_hint or "").strip(),
        "dom_diagnosis": dom_diagnosis or {},
        "dom_snippet": dom_snippet[:5000],
    }
    context_json = json.dumps(context_payload, ensure_ascii=True, indent=2)
    system_prompt = f"""You are a senior Playwright locator-repair engine.
Your job: propose safe, unique selectors for a broken recorded step, using the full context below.

Rules:
1) Never repeat selectors from already_tried_selectors.
2) Prefer stable selectors in this order:
   data-testid/data-test-id/data-qa -> aria-label -> role+name -> text -> placeholder -> name -> id -> scoped CSS -> XPath.
3) Avoid hash-like CSS classes and fragile nth-child chains unless absolutely necessary.
4) If the DOM evidence indicates the target element is missing, return no candidates and explain that honestly.
5) Output ONLY a JSON object with keys:
   - candidates: string[] (max 5)
   - explanation: string
   - domStatus: one of "present_or_shifted", "likely_missing", "inconclusive"

Context:
{context_json}
"""

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Return the JSON object now."},
        ]
        response = await llm_provider.chat(messages, max_tokens=8192)
        response_text = response.content or ""
        logger.debug("llm_repair_raw_response length=%d", len(response_text))
        result = _extract_llm_json(response_text)
        # Normalize: if LLM returned {"locator": ...} shape, convert to candidates list
        if "locator" in result and "candidates" not in result:
            loc = result.get("locator")
            result["candidates"] = [loc] if loc else []
        if "domStatus" not in result:
            result["domStatus"] = (dom_diagnosis or {}).get("status", "inconclusive")
        result["_raw_response_preview"] = response_text[:2000]
        return result
    except Exception as exc:
        logger.error("llm_repair_error error=%s", str(exc))
        return {"candidates": [], "explanation": f"LLM call failed: {exc}"}

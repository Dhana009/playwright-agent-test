from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlparse

from playwright.async_api import Frame, Page

from agent.cache.models import CacheDecision, CacheRecord, ContextFingerprint
from agent.core.logging import get_logger
from agent.execution.browser import BrowserSession, BrowserSessionError
from agent.storage.repos.cache import CacheRepository

_DYNAMIC_SEGMENT_RE = re.compile(r"^\d+$|^[0-9a-f]{8,}$|^[0-9a-f-]{12,}$", re.IGNORECASE)

CacheTelemetryEmitter = Callable[[CacheRecord], Awaitable[None] | None]


@dataclass(frozen=True)
class CacheDecisionResult:
    decision: CacheDecision
    fingerprint: ContextFingerprint
    reasons: list[str]
    previous_fingerprint: ContextFingerprint | None
    fingerprint_matched: bool


class CacheEngine:
    def __init__(
        self,
        browser_session: BrowserSession,
        *,
        cache_repo: CacheRepository | None = None,
        telemetry_emitter: CacheTelemetryEmitter | None = None,
    ) -> None:
        self._logger = get_logger(__name__)
        self._browser_session = browser_session
        self._cache_repo = cache_repo or CacheRepository()
        self._telemetry_emitter = telemetry_emitter

    async def decide(
        self,
        *,
        run_id: str,
        step_id: str,
        tab_id: str,
        target_selectors: list[str] | None = None,
        stale_ref_detected: bool = False,
    ) -> CacheDecisionResult:
        selectors = _normalize_selectors(target_selectors)
        current_fingerprint = await self._capture_current_fingerprint(
            tab_id=tab_id,
            target_selectors=selectors,
        )
        previous_record = await self._cache_repo.load_latest(run_id=run_id, step_id=step_id)
        previous_fingerprint = previous_record.fingerprint if previous_record is not None else None

        decision, reasons, fingerprint_matched = self._classify_decision(
            current=current_fingerprint,
            previous=previous_fingerprint,
            stale_ref_detected=stale_ref_detected,
        )
        record = CacheRecord(
            runId=run_id,
            stepId=step_id,
            fingerprint=current_fingerprint.model_dump(mode="python", by_alias=True),
            decision=decision,
            decisionReasons=reasons,
        )
        await self._cache_repo.save(record)
        await _maybe_await(self._telemetry_emitter, record)

        self._logger.info(
            "cache_decision",
            run_id=run_id,
            step_id=step_id,
            tab_id=tab_id,
            decision=decision.value,
            reasons=reasons,
            fingerprint_matched=fingerprint_matched,
            stale_ref_detected=stale_ref_detected,
            route_template=current_fingerprint.route_template,
            dom_hash=current_fingerprint.dom_hash,
            frame_hash=current_fingerprint.frame_hash,
            modal_state=current_fingerprint.modal_state,
            previous_fingerprint=(
                previous_fingerprint.model_dump(mode="python", by_alias=False)
                if previous_fingerprint is not None
                else None
            ),
        )
        return CacheDecisionResult(
            decision=decision,
            fingerprint=current_fingerprint,
            reasons=reasons,
            previous_fingerprint=previous_fingerprint,
            fingerprint_matched=fingerprint_matched,
        )

    def _classify_decision(
        self,
        *,
        current: ContextFingerprint,
        previous: ContextFingerprint | None,
        stale_ref_detected: bool,
    ) -> tuple[CacheDecision, list[str], bool]:
        if previous is None:
            return CacheDecision.FULL_REFRESH, ["no_cached_fingerprint"], False

        full_refresh_reasons: list[str] = []
        partial_refresh_reasons: list[str] = []

        if stale_ref_detected:
            full_refresh_reasons.append("stale_ref_locator_mismatch")

        if current.route_template != previous.route_template:
            full_refresh_reasons.append("route_changed")

        if current.frame_hash != previous.frame_hash:
            full_refresh_reasons.append("frame_structure_changed")

        if current.modal_state != previous.modal_state:
            partial_refresh_reasons.append("modal_state_changed")

        if current.dom_hash != previous.dom_hash:
            partial_refresh_reasons.append("dom_mutation_in_target_scope")

        if full_refresh_reasons:
            return CacheDecision.FULL_REFRESH, full_refresh_reasons + partial_refresh_reasons, False

        if partial_refresh_reasons:
            return CacheDecision.PARTIAL_REFRESH, partial_refresh_reasons, False

        return CacheDecision.REUSE, ["fingerprint_match"], True

    async def _capture_current_fingerprint(
        self,
        *,
        tab_id: str,
        target_selectors: list[str],
    ) -> ContextFingerprint:
        page = self._browser_session.get_tab(tab_id)
        if page is None:
            msg = f"Unknown tab id: {tab_id}"
            raise BrowserSessionError(msg)

        route_template = _normalize_route_template(page.url)
        frame_hash = self._hash_text(self._build_frame_signature(page))
        modal_state = await self._compute_modal_state(page)
        dom_hash = await self._compute_dom_hash(page, target_selectors)
        return ContextFingerprint(
            routeTemplate=route_template,
            domHash=dom_hash,
            frameHash=frame_hash,
            modalState=modal_state,
        )

    async def _compute_modal_state(self, page: Page) -> str:
        has_modal = await page.evaluate(
            """
            () => !!document.querySelector(
                'dialog[open], [role="dialog"], [role="alertdialog"], [aria-modal="true"]'
            )
            """
        )
        return "modal_open" if bool(has_modal) else "modal_closed"

    async def _compute_dom_hash(self, page: Page, target_selectors: list[str]) -> str:
        if not target_selectors:
            signature = await page.evaluate(
                """
                () => {
                  const root = document.body || document.documentElement;
                  const text = (root?.innerText || "").replace(/\\s+/g, " ").trim().slice(0, 4000);
                  return {
                    title: document.title || "",
                    text,
                    elementCount: document.querySelectorAll("*").length,
                    interactiveCount: document.querySelectorAll(
                      'a, button, input, select, textarea, [role], dialog, [aria-modal="true"]'
                    ).length
                  };
                }
                """
            )
            return self._hash_text(json.dumps(signature, sort_keys=True))

        scoped_signatures: list[dict[str, Any]] = []
        for selector in target_selectors:
            scoped_signatures.append(await self._build_selector_signature(page, selector))
        return self._hash_text(json.dumps(scoped_signatures, sort_keys=True))

    async def _build_selector_signature(self, page: Page, selector: str) -> dict[str, Any]:
        locator = page.locator(selector)
        try:
            count = await locator.count()
            if count == 0:
                return {"selector": selector, "count": 0, "samples": []}

            samples = await locator.evaluate_all(
                """
                (nodes) => nodes.slice(0, 3).map((node) => {
                  const style = window.getComputedStyle(node);
                  const rect = node.getBoundingClientRect();
                  const visible =
                    style.visibility !== "hidden" &&
                    style.display !== "none" &&
                    rect.width > 0 &&
                    rect.height > 0;
                  const text = (node.textContent || "").replace(/\\s+/g, " ").trim().slice(0, 200);
                  return {
                    tag: node.tagName.toLowerCase(),
                    role: node.getAttribute("role"),
                    ariaLabel: node.getAttribute("aria-label"),
                    text,
                    visible,
                    disabled: node.hasAttribute("disabled"),
                  };
                })
                """
            )
            return {"selector": selector, "count": count, "samples": samples}
        except Exception as exc:
            return {"selector": selector, "count": -1, "error": str(exc)}

    def _build_frame_signature(self, page: Page) -> str:
        signatures: list[str] = []
        for frame in self._iter_frames(page.main_frame):
            frame_id = self._browser_session.get_frame_id(frame) or "untracked"
            frame_path = self._browser_session.get_frame_path(frame)
            signatures.append(f"{'>'.join(frame_path)}|{frame_id}|{frame.url}")
        signatures.sort()
        return "||".join(signatures)

    def _iter_frames(self, frame: Frame) -> list[Frame]:
        frames = [frame]
        for child in frame.child_frames:
            frames.extend(self._iter_frames(child))
        return frames

    def _hash_text(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _maybe_await(
    emitter: CacheTelemetryEmitter | None,
    record: CacheRecord,
) -> None:
    if emitter is None:
        return
    maybe_awaitable = emitter(record)
    if isinstance(maybe_awaitable, Awaitable):
        await maybe_awaitable


def _normalize_selectors(selectors: list[str] | None) -> list[str]:
    if not selectors:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for selector in selectors:
        if not isinstance(selector, str):
            continue
        value = selector.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _normalize_route_template(url: str) -> str:
    parsed = urlparse(url)
    path_segments = [segment for segment in parsed.path.split("/") if segment]
    normalized_segments: list[str] = []
    for segment in path_segments:
        normalized_segments.append(":param" if _DYNAMIC_SEGMENT_RE.match(segment) else segment)

    normalized_path = "/" + "/".join(normalized_segments) if normalized_segments else "/"
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if not query_pairs:
        return normalized_path

    query_template = "&".join(sorted(f"{key}=*" for key, _ in query_pairs))
    return f"{normalized_path}?{query_template}"

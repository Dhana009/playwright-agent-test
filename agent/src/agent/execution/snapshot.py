# Ported from playwright-cli/playwright-cli.js — adapted for agent/
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlparse

import yaml
from playwright.async_api import ElementHandle, Frame, Page
from pydantic import BaseModel, ConfigDict, Field

from agent.cache.models import ContextFingerprint
from agent.core.logging import get_logger
from agent.execution.browser import BrowserSession, BrowserSessionError

_REF_ATTRIBUTE = "data-agent-ref"
_DYNAMIC_SEGMENT_RE = re.compile(r"^\d+$|^[0-9a-f]{8,}$|^[0-9a-f-]{12,}$", re.IGNORECASE)


class SnapshotElement(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    ref: str
    frame_path: list[str] = Field(default_factory=list, alias="framePath")
    tag: str
    role: str | None = None
    name: str | None = None
    text: str | None = None
    visible: bool = False


class SnapshotResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    tab_id: str = Field(alias="tabId")
    page_url: str = Field(alias="pageUrl")
    page_title: str = Field(alias="pageTitle")
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC), alias="capturedAt")
    aria_yaml: str = Field(alias="ariaYaml")
    elements: list[SnapshotElement] = Field(default_factory=list)
    fingerprint: ContextFingerprint


@dataclass(frozen=True)
class _RefBinding:
    tab_id: str
    frame_path: list[str]
    selector: str


class SnapshotEngine:
    def __init__(self, browser_session: BrowserSession) -> None:
        self._logger = get_logger(__name__)
        self._browser_session = browser_session
        self._ref_bindings: dict[str, _RefBinding] = {}

    async def capture_snapshot(self, tab_id: str, *, max_elements_per_frame: int = 500) -> SnapshotResult:
        page = self._browser_session.get_tab(tab_id)
        if page is None:
            msg = f"Unknown tab id: {tab_id}"
            raise BrowserSessionError(msg)

        self._ref_bindings.clear()
        elements: list[SnapshotElement] = []
        frame_signatures: list[str] = []
        signature_chunks: list[str] = []
        modal_open = False
        next_ref_counter = 1

        for frame in self._iter_frames(page.main_frame):
            frame_id = self._browser_session.get_frame_id(frame)
            if frame_id is None:
                continue

            frame_path = self._browser_session.get_frame_path(frame)
            frame_signatures.append(f"{'>'.join(frame_path)}|{frame.url}")

            payload = await frame.evaluate(
                """
                ({ attrName, refPrefix, startIndex, maxElements }) => {
                  const root = document.body || document.documentElement;
                  if (!root) {
                    return { elements: [], nextIndex: startIndex, hasModal: false };
                  }

                  for (const existing of root.querySelectorAll(`[${attrName}]`)) {
                    existing.removeAttribute(attrName);
                  }

                  const result = [];
                  let index = startIndex;
                  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);

                  let node = walker.currentNode;
                  while (node && result.length < maxElements) {
                    if (node instanceof Element) {
                      const ref = `${refPrefix}_${index}`;
                      index += 1;
                      node.setAttribute(attrName, ref);

                      const style = window.getComputedStyle(node);
                      const rect = node.getBoundingClientRect();
                      const visible =
                        style.visibility !== "hidden" &&
                        style.display !== "none" &&
                        rect.width > 0 &&
                        rect.height > 0;

                      const textRaw = (node.textContent || "").replace(/\\s+/g, " ").trim();
                      const text = textRaw.length > 160 ? textRaw.slice(0, 160) : textRaw;
                      const label = node.getAttribute("aria-label");
                      const title = node.getAttribute("title");
                      const alt = node.getAttribute("alt");
                      const name = (label || title || alt || text || "").slice(0, 120);

                      result.push({
                        ref,
                        tag: node.tagName.toLowerCase(),
                        role: node.getAttribute("role"),
                        name: name || null,
                        text: text || null,
                        visible
                      });
                    }
                    node = walker.nextNode();
                  }

                  const hasModal = !!root.querySelector(
                    'dialog[open], [role="dialog"], [role="alertdialog"], [aria-modal="true"]'
                  );

                  return {
                    elements: result,
                    nextIndex: index,
                    hasModal
                  };
                }
                """,
                {
                    "attrName": _REF_ATTRIBUTE,
                    "refPrefix": frame_id,
                    "startIndex": next_ref_counter,
                    "maxElements": max_elements_per_frame,
                },
            )

            next_ref_counter = int(payload["nextIndex"])
            modal_open = modal_open or bool(payload["hasModal"])

            for item in payload["elements"]:
                ref = str(item["ref"])
                element = SnapshotElement(
                    ref=ref,
                    framePath=frame_path,
                    tag=str(item["tag"]),
                    role=item.get("role"),
                    name=item.get("name"),
                    text=item.get("text"),
                    visible=bool(item.get("visible", False)),
                )
                elements.append(element)
                self._ref_bindings[ref] = _RefBinding(
                    tab_id=tab_id,
                    frame_path=frame_path,
                    selector=f'[{_REF_ATTRIBUTE}="{ref}"]',
                )
                signature_chunks.append(
                    f"{ref}|{element.tag}|{element.role or ''}|{element.name or ''}|{element.text or ''}|{int(element.visible)}"
                )

        aria_yaml = await self._capture_aria_snapshot(page)
        signature_chunks.append(aria_yaml)
        dom_hash = self._hash_text("||".join(signature_chunks))
        frame_hash = self._hash_text("||".join(sorted(frame_signatures)))
        route_template = _normalize_route_template(page.url)

        fingerprint = ContextFingerprint(
            routeTemplate=route_template,
            domHash=dom_hash,
            frameHash=frame_hash,
            modalState="modal_open" if modal_open else "modal_closed",
        )
        snapshot = SnapshotResult(
            tabId=tab_id,
            pageUrl=page.url,
            pageTitle=await page.title(),
            ariaYaml=aria_yaml,
            elements=elements,
            fingerprint=fingerprint,
        )
        self._logger.info(
            "snapshot_captured",
            tab_id=tab_id,
            element_count=len(elements),
            route_template=route_template,
            dom_hash=dom_hash,
            frame_hash=frame_hash,
            modal_state=fingerprint.modal_state,
        )
        return snapshot

    async def resolve_ref(self, ref: str) -> ElementHandle | None:
        binding = self._ref_bindings.get(ref)
        if binding is None:
            return None

        frame = self._browser_session.resolve_frame_path(binding.frame_path)
        if frame is None:
            return None
        return await frame.query_selector(binding.selector)

    def get_ref_binding(self, ref: str) -> tuple[str, list[str], str] | None:
        binding = self._ref_bindings.get(ref)
        if binding is None:
            return None
        return binding.tab_id, list(binding.frame_path), binding.selector

    async def _capture_aria_snapshot(self, page: Page) -> str:
        try:
            return await page.locator("body").aria_snapshot()
        except Exception:
            try:
                accessibility_tree = await page.accessibility.snapshot(interesting_only=False)
                if accessibility_tree is None:
                    return ""
                return yaml.safe_dump(accessibility_tree, sort_keys=False)
            except Exception as exc:
                self._logger.warning(
                    "snapshot_aria_capture_failed",
                    tab_id=self._browser_session.get_tab_id(page),
                    error=str(exc),
                )
                return ""

    def _iter_frames(self, root: Frame) -> list[Frame]:
        frames: list[Frame] = [root]
        for child in root.child_frames:
            frames.extend(self._iter_frames(child))
        return frames

    def _hash_text(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


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

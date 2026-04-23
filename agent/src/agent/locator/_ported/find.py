# Ported from playwright-repo-test/lib/locator/find.js — adapted for agent/
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from playwright.async_api import Frame, Page

from agent.locator._ported.candidates import PortedCandidate, build_candidates


@dataclass(frozen=True)
class CandidateProbe:
    candidate: PortedCandidate
    selector: str
    total_count: int
    visible_count: int
    actionable: bool
    used_first: bool = False
    forced: bool = False


async def find_best_candidate(
    scope: Page | Frame,
    target: dict[str, Any],
    *,
    force: bool = False,
) -> CandidateProbe | None:
    candidates = build_candidates(target)
    valid: list[CandidateProbe] = []

    for candidate in candidates:
        probe = await probe_candidate(scope, candidate, force=force)
        if probe is not None:
            valid.append(probe)

    if valid:
        return sorted(valid, key=lambda item: item.candidate.priority)[0]

    if not force:
        return None

    for candidate in candidates:
        locator = scope.locator(candidate.selector)
        total = await locator.count()
        if total == 0:
            continue

        actionable = await _is_actionable(locator.first if total > 1 else locator)
        return CandidateProbe(
            candidate=candidate,
            selector=candidate.selector if total == 1 else f"{candidate.selector} >> nth=0",
            total_count=total,
            visible_count=0,
            actionable=actionable,
            used_first=total > 1,
            forced=True,
        )

    return None


async def probe_candidate(
    scope: Page | Frame,
    candidate: PortedCandidate,
    *,
    force: bool = False,
) -> CandidateProbe | None:
    locator = scope.locator(candidate.selector)
    total = await locator.count()
    if total == 0:
        return None

    visible_count = await _count_visible(locator, total)
    if visible_count == 1:
        visible_locator = await _first_visible_locator(locator, total)
        if visible_locator is None:
            return None
        return CandidateProbe(
            candidate=candidate,
            selector=candidate.selector,
            total_count=total,
            visible_count=visible_count,
            actionable=await _is_actionable(visible_locator),
            used_first=False,
            forced=False,
        )

    if visible_count > 1 and force:
        visible_locator = await _first_visible_locator(locator, total)
        if visible_locator is None:
            return None
        return CandidateProbe(
            candidate=candidate,
            selector=f"{candidate.selector} >> nth=0",
            total_count=total,
            visible_count=visible_count,
            actionable=await _is_actionable(visible_locator),
            used_first=True,
            forced=False,
        )

    return None


async def _count_visible(locator, total_count: int) -> int:
    visible_count = 0
    sample_size = min(total_count, 25)
    for index in range(sample_size):
        item = locator.nth(index)
        if await item.is_visible():
            visible_count += 1
    if total_count > sample_size:
        remaining = total_count - sample_size
        visible_count += remaining
    return visible_count


async def _first_visible_locator(locator, total_count: int):
    sample_size = min(total_count, 25)
    for index in range(sample_size):
        item = locator.nth(index)
        if await item.is_visible():
            return item
    if total_count > sample_size:
        return locator.first
    return None


async def _is_actionable(locator) -> bool:
    try:
        return await locator.is_enabled()
    except Exception:
        return False

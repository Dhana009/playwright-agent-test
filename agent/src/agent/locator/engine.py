from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from playwright.async_api import Frame, Page
from pydantic import BaseModel, ConfigDict, Field

from agent.locator._ported.candidates import PortedCandidate, build_candidates
from agent.locator._ported.find import find_best_candidate, probe_candidate
from agent.stepgraph.models import LocatorBundle


class LocatorResolutionError(RuntimeError):
    pass


class TargetDescriptor(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    tag: str = "div"
    id: str | None = None
    class_name: str | None = Field(default=None, alias="className")
    testid: str | None = None
    text: str | None = None
    placeholder: str | None = None
    aria_label: str | None = Field(default=None, alias="ariaLabel")
    role: str | None = None
    input_type: str | None = Field(default=None, alias="inputType")
    name: str | None = None
    data_attrs: dict[str, str] = Field(default_factory=dict, alias="dataAttrs")
    sibling_index: int = Field(default=-1, alias="siblingIndex")
    parents: list[dict[str, Any]] = Field(default_factory=list)
    absolute_xpath: str | None = Field(default=None, alias="absoluteXPath")
    frame_context: list[str] = Field(default_factory=list, alias="frameContext")
    target_semantic_key: str | None = Field(default=None, alias="targetSemanticKey")


class RankedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    selector: str
    strategy: str
    label: str
    priority: float
    confidence_score: float = Field(alias="confidenceScore", ge=0.0, le=1.0)
    total_count: int = Field(alias="totalCount", ge=0)
    visible_count: int = Field(alias="visibleCount", ge=0)
    actionable: bool
    used_first: bool = Field(default=False, alias="usedFirst")
    forced: bool = False


@dataclass(frozen=True)
class _ScoreInputs:
    uniqueness: float
    visibility_actionability: float
    stability: float
    prior_success_history: float
    freshness: float
    forced_penalty: float
    used_first_penalty: float


class LocatorEngine:
    def __init__(self, *, max_fallbacks: int = 6) -> None:
        self._max_fallbacks = max_fallbacks

    def build_candidates(self, target: TargetDescriptor | Mapping[str, Any]) -> list[PortedCandidate]:
        descriptor = _to_descriptor(target)
        return build_candidates(descriptor.model_dump(by_alias=True))

    async def rank_candidates(
        self,
        scope: Page | Frame,
        target: TargetDescriptor | Mapping[str, Any],
        *,
        history_scores: Mapping[str, float] | None = None,
        freshness_scores: Mapping[str, float] | None = None,
        force: bool = False,
    ) -> list[RankedCandidate]:
        descriptor = _to_descriptor(target)
        candidates = self.build_candidates(descriptor)

        ranked: list[RankedCandidate] = []
        for candidate in candidates:
            probe = await probe_candidate(scope, candidate, force=force)
            if probe is None:
                continue

            confidence = self.score_candidate(
                candidate=candidate,
                total_count=probe.total_count,
                visible_count=probe.visible_count,
                actionable=probe.actionable,
                used_first=probe.used_first,
                forced=probe.forced,
                prior_success_history=_lookup_score(history_scores, probe.selector),
                freshness=_lookup_score(freshness_scores, probe.selector),
            )
            ranked.append(
                RankedCandidate(
                    selector=probe.selector,
                    strategy=candidate.strategy,
                    label=candidate.label,
                    priority=candidate.priority,
                    confidenceScore=confidence,
                    totalCount=probe.total_count,
                    visibleCount=probe.visible_count,
                    actionable=probe.actionable,
                    usedFirst=probe.used_first,
                    forced=probe.forced,
                )
            )

        if ranked:
            return sorted(
                ranked,
                key=lambda item: (-item.confidence_score, item.priority),
            )

        if force:
            forced_probe = await find_best_candidate(scope, descriptor.model_dump(by_alias=True), force=True)
            if forced_probe:
                confidence = self.score_candidate(
                    candidate=forced_probe.candidate,
                    total_count=forced_probe.total_count,
                    visible_count=forced_probe.visible_count,
                    actionable=forced_probe.actionable,
                    used_first=forced_probe.used_first,
                    forced=forced_probe.forced,
                    prior_success_history=_lookup_score(history_scores, forced_probe.selector),
                    freshness=_lookup_score(freshness_scores, forced_probe.selector),
                )
                return [
                    RankedCandidate(
                        selector=forced_probe.selector,
                        strategy=forced_probe.candidate.strategy,
                        label=forced_probe.candidate.label,
                        priority=forced_probe.candidate.priority,
                        confidenceScore=confidence,
                        totalCount=forced_probe.total_count,
                        visibleCount=forced_probe.visible_count,
                        actionable=forced_probe.actionable,
                        usedFirst=forced_probe.used_first,
                        forced=forced_probe.forced,
                    )
                ]

        return []

    def score_candidate(
        self,
        *,
        candidate: PortedCandidate,
        total_count: int,
        visible_count: int,
        actionable: bool,
        used_first: bool,
        forced: bool,
        prior_success_history: float,
        freshness: float,
    ) -> float:
        if total_count <= 0:
            return 0.0

        uniqueness = _uniqueness_score(total_count)
        visibility_actionability = _visibility_actionability_score(visible_count, actionable)
        stability = _stability_score(candidate)

        inputs = _ScoreInputs(
            uniqueness=uniqueness,
            visibility_actionability=visibility_actionability,
            stability=stability,
            prior_success_history=_clamp01(prior_success_history),
            freshness=_clamp01(freshness),
            forced_penalty=0.08 if forced else 0.0,
            used_first_penalty=0.06 if used_first else 0.0,
        )

        confidence = (
            0.32 * inputs.uniqueness
            + 0.24 * inputs.visibility_actionability
            + 0.20 * inputs.stability
            + 0.12 * inputs.prior_success_history
            + 0.12 * inputs.freshness
        )
        confidence -= inputs.forced_penalty
        confidence -= inputs.used_first_penalty
        return _clamp01(round(confidence, 4))

    async def build(
        self,
        scope: Page | Frame,
        target: TargetDescriptor | Mapping[str, Any],
        *,
        history_scores: Mapping[str, float] | None = None,
        freshness_scores: Mapping[str, float] | None = None,
        force: bool = False,
    ) -> LocatorBundle:
        descriptor = _to_descriptor(target)
        ranked = await self.rank_candidates(
            scope,
            descriptor,
            history_scores=history_scores,
            freshness_scores=freshness_scores,
            force=force,
        )
        if not ranked:
            raise LocatorResolutionError("No valid locator candidates were found for target.")

        primary = ranked[0]
        fallback_selectors = [item.selector for item in ranked[1 : self._max_fallbacks + 1]]
        reasoning_hint = _reasoning_hint(primary, ranked)
        return LocatorBundle(
            primarySelector=primary.selector,
            fallbackSelectors=fallback_selectors,
            confidenceScore=primary.confidence_score,
            reasoningHint=reasoning_hint,
            frameContext=list(descriptor.frame_context),
        )

    async def build_ranked_bundles(
        self,
        scope: Page | Frame,
        target: TargetDescriptor | Mapping[str, Any],
        *,
        history_scores: Mapping[str, float] | None = None,
        freshness_scores: Mapping[str, float] | None = None,
        force: bool = False,
    ) -> list[LocatorBundle]:
        descriptor = _to_descriptor(target)
        ranked = await self.rank_candidates(
            scope,
            descriptor,
            history_scores=history_scores,
            freshness_scores=freshness_scores,
            force=force,
        )
        bundles: list[LocatorBundle] = []
        for index, item in enumerate(ranked):
            fallbacks = [candidate.selector for candidate in ranked[index + 1 : index + 1 + self._max_fallbacks]]
            bundles.append(
                LocatorBundle(
                    primarySelector=item.selector,
                    fallbackSelectors=fallbacks,
                    confidenceScore=item.confidence_score,
                    reasoningHint=_reasoning_hint(item, ranked),
                    frameContext=list(descriptor.frame_context),
                )
            )
        return bundles


def _to_descriptor(target: TargetDescriptor | Mapping[str, Any]) -> TargetDescriptor:
    if isinstance(target, TargetDescriptor):
        return target
    return TargetDescriptor.model_validate(target)


def _lookup_score(scores: Mapping[str, float] | None, selector: str) -> float:
    if not scores:
        return 0.5
    return _clamp01(scores.get(selector, 0.5))


def _uniqueness_score(total_count: int) -> float:
    if total_count == 1:
        return 1.0
    if total_count <= 3:
        return 0.68
    if total_count <= 6:
        return 0.5
    return 0.3


def _visibility_actionability_score(visible_count: int, actionable: bool) -> float:
    if visible_count <= 0:
        return 0.2 if actionable else 0.1
    if visible_count == 1:
        return 1.0 if actionable else 0.85
    return 0.65 if actionable else 0.45


def _stability_score(candidate: PortedCandidate) -> float:
    stable_by_strategy = {
        "testid": 0.96,
        "scoped_chain": 0.93,
        "aria_label": 0.86,
        "label": 0.84,
        "role_name": 0.8,
        "placeholder": 0.74,
        "stable_text": 0.68,
        "scoped_css": 0.58,
        "xpath_nth_fallback": 0.45,
    }
    return stable_by_strategy.get(candidate.strategy, 0.5)


def _reasoning_hint(primary: RankedCandidate, ranked: list[RankedCandidate]) -> str:
    alternatives = ", ".join(item.strategy for item in ranked[1:4]) or "none"
    return (
        f"strategy={primary.strategy}; confidence={primary.confidence_score:.2f}; "
        f"visible={primary.visible_count}; actionable={str(primary.actionable).lower()}; "
        f"alternatives={alternatives}"
    )


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))

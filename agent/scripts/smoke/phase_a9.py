from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
FIXTURE_CASSETTES = Path(__file__).resolve().parents[1] / "fixtures" / "llm_cassettes"
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(SCRIPT_DIR))

from agent.core.ids import generate_run_id  # noqa: E402
from agent.core.mode import ModeController, RuntimeBinding, RuntimeMode  # noqa: E402
from agent.execution.events import EventType  # noqa: E402
from agent.llm._ported import (  # noqa: E402
    NormalizedTransportResponse,
    TransportUsage,
    build_tool_call,
)
from agent.llm.orchestrator import LLMOrchestrator, OrchestratorConfig  # noqa: E402
from agent.llm.provider import (  # noqa: E402
    LLMProvider,
    LLMResponse,
    LLMToolCall,
    LLMUsage,
    build_llm_call,
    to_llm_response,
)
from agent.storage.repos.events import EventRepository  # noqa: E402
from agent.storage.repos.telemetry import TelemetryRepository  # noqa: E402
from agent.telemetry.models import CallPurpose, ContextTier  # noqa: E402
from _runner import SmokeRunner  # noqa: E402


def _load_cassette(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalized_from_cassette_response(response: dict[str, Any]) -> NormalizedTransportResponse:
    raw_tools = response.get("tool_calls") or []
    tool_calls = None
    if raw_tools:
        tool_calls = [
            build_tool_call(
                id=tc.get("id"),
                name=tc["name"],
                arguments=tc.get("arguments", {}),
            )
            for tc in raw_tools
        ]
    usage_payload = response.get("usage") or {}
    usage = TransportUsage(
        prompt_tokens=int(usage_payload.get("prompt_tokens", 0)),
        completion_tokens=int(usage_payload.get("completion_tokens", 0)),
        total_tokens=int(usage_payload.get("total_tokens", 0)),
        cached_tokens=int(usage_payload.get("cached_tokens", 0)),
        cache_write_tokens=int(usage_payload.get("cache_write_tokens", 0)),
    )
    return NormalizedTransportResponse(
        content=response.get("content"),
        tool_calls=tool_calls,
        finish_reason=str(response.get("finish_reason", "stop")),
        usage=usage,
        provider_data=None,
    )


class CassetteReplayProvider(LLMProvider):
    """Replays committed JSON cassettes; records telemetry like production adapters."""

    def __init__(
        self,
        cassette: dict[str, Any],
        *,
        telemetry_repository: TelemetryRepository | None = None,
    ) -> None:
        self._cassette = cassette
        self._telemetry_repository = telemetry_repository

    @property
    def provider_name(self) -> str:
        return str(self._cassette["provider"])

    @property
    def default_model(self) -> str:
        return str(self._cassette["model"])

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
        call_purpose: CallPurpose = CallPurpose.PLAN,
        context_tier: ContextTier = ContextTier.TIER_0,
        escalation_path: list[ContextTier] | None = None,
        preflight_input_tokens: int = 0,
        preflight_output_tokens: int = 0,
        est_cost: float = 0.0,
        actual_cost: float | None = None,
        no_progress_retry: bool = False,
    ) -> LLMResponse:
        _ = (messages, tools, temperature, max_tokens, timeout_seconds, metadata)
        resolved_model = model or self.default_model
        normalized = _normalized_from_cassette_response(self._cassette["response"])
        llm_response = to_llm_response(
            provider=self.provider_name,
            model=resolved_model,
            normalized=normalized,
        )
        if run_id is None:
            return llm_response

        start = time.perf_counter()
        latency_ms = int((time.perf_counter() - start) * 1000)
        llm_call = build_llm_call(
            run_id=run_id,
            step_id=step_id,
            provider=self.provider_name,
            model=resolved_model,
            call_purpose=call_purpose,
            context_tier=context_tier,
            escalation_path=escalation_path,
            input_tokens=llm_response.usage.prompt_tokens,
            output_tokens=llm_response.usage.completion_tokens,
            preflight_input_tokens=preflight_input_tokens,
            preflight_output_tokens=preflight_output_tokens,
            cache_read=llm_response.usage.cached_tokens,
            cache_write=llm_response.usage.cache_write_tokens,
            est_cost=est_cost,
            actual_cost=actual_cost,
            latency_ms=latency_ms,
            no_progress_retry=no_progress_retry,
        )
        run_summary = None
        if self._telemetry_repository is not None:
            run_summary = await self._telemetry_repository.record_llm_call(llm_call)
        return llm_response.model_copy(update={"llm_call": llm_call, "run_summary": run_summary})


class _TierEscalationMockProvider(LLMProvider):
    @property
    def provider_name(self) -> str:
        return "tier_escalation_mock"

    @property
    def default_model(self) -> str:
        return "mock-tier-model"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
        call_purpose: CallPurpose = CallPurpose.PLAN,
        context_tier: ContextTier = ContextTier.TIER_0,
        escalation_path: list[ContextTier] | None = None,
        preflight_input_tokens: int = 0,
        preflight_output_tokens: int = 0,
        est_cost: float = 0.0,
        actual_cost: float | None = None,
        no_progress_retry: bool = False,
    ) -> LLMResponse:
        _ = (
            messages,
            tools,
            temperature,
            max_tokens,
            timeout_seconds,
            metadata,
            run_id,
            step_id,
            escalation_path,
            preflight_input_tokens,
            preflight_output_tokens,
            est_cost,
            actual_cost,
            no_progress_retry,
        )
        m = model or self.default_model
        usage = LLMUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5)
        if context_tier in (ContextTier.TIER_0, ContextTier.TIER_1):
            return LLMResponse(
                provider=self.provider_name,
                model=m,
                content="",
                finish_reason="stop",
                usage=usage,
            )
        if call_purpose == CallPurpose.REVIEW:
            return LLMResponse(
                provider=self.provider_name,
                model=m,
                content="Review complete.",
                finish_reason="stop",
                usage=usage,
            )
        return LLMResponse(
            provider=self.provider_name,
            model=m,
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                LLMToolCall(
                    id="mock_click",
                    name="click",
                    arguments='{"tab_id":"tab_a9","target":"button"}',
                )
            ],
            usage=usage,
        )


class _NoProgressMockProvider(LLMProvider):
    @property
    def provider_name(self) -> str:
        return "no_progress_mock"

    @property
    def default_model(self) -> str:
        return "mock-stall-model"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        metadata: dict[str, Any] | None = None,
        run_id: str | None = None,
        step_id: str | None = None,
        call_purpose: CallPurpose = CallPurpose.PLAN,
        context_tier: ContextTier = ContextTier.TIER_0,
        escalation_path: list[ContextTier] | None = None,
        preflight_input_tokens: int = 0,
        preflight_output_tokens: int = 0,
        est_cost: float = 0.0,
        actual_cost: float | None = None,
        no_progress_retry: bool = False,
    ) -> LLMResponse:
        _ = (
            messages,
            tools,
            temperature,
            max_tokens,
            timeout_seconds,
            metadata,
            run_id,
            step_id,
            call_purpose,
            context_tier,
            escalation_path,
            preflight_input_tokens,
            preflight_output_tokens,
            est_cost,
            actual_cost,
            no_progress_retry,
        )
        return LLMResponse(
            provider=self.provider_name,
            model=model or self.default_model,
            content="",
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


async def _assert_cassette_telemetry(
    *,
    smoke: SmokeRunner,
    cassette_path: Path,
    sqlite_path: Path,
    case_token: str,
) -> None:
    cassette = _load_cassette(cassette_path)
    run_id = generate_run_id()
    repo = TelemetryRepository(sqlite_path=sqlite_path)
    provider = CassetteReplayProvider(cassette, telemetry_repository=repo)
    await provider.chat(
        [{"role": "user", "content": "ignored in replay"}],
        run_id=run_id,
        step_id=f"step_{case_token}",
        call_purpose=CallPurpose.PLAN,
        context_tier=ContextTier.TIER_0,
    )
    rows = await repo.load_llm_calls_for_run(run_id)
    smoke.check(len(rows) == 1, f"Expected 1 LLMCall for {case_token}, got {len(rows)}")
    row = rows[0]
    exp = cassette["response"].get("usage") or {}
    smoke.check(row.provider == cassette["provider"], f"provider mismatch for {case_token}")
    smoke.check(row.model == cassette["model"], f"model mismatch for {case_token}")
    smoke.check(row.call_purpose == CallPurpose.PLAN, f"callPurpose mismatch for {case_token}")
    smoke.check(row.context_tier == ContextTier.TIER_0, f"contextTier mismatch for {case_token}")
    smoke.check(row.input_tokens == int(exp.get("prompt_tokens", 0)), f"inputTokens mismatch for {case_token}")
    smoke.check(
        row.output_tokens == int(exp.get("completion_tokens", 0)),
        f"outputTokens mismatch for {case_token}",
    )


async def main() -> int:
    smoke = SmokeRunner(phase="A9", default_task="A9.1")

    with smoke.case("a9_1_cassettes_committed", task="A9.1", feature="llm_cassettes"):
        for name in ("openai_chat.json", "anthropic_chat.json", "openai_compatible_chat.json"):
            path = FIXTURE_CASSETTES / name
            smoke.check(path.is_file(), f"Missing cassette {path}")

    with smoke.case("a9_2_llm_call_rows", task="A9.2", feature="llm_telemetry"):
        with tempfile.TemporaryDirectory(prefix="a9-llm-") as tmp:
            db = Path(tmp) / "telemetry.sqlite"
            await _assert_cassette_telemetry(
                smoke=smoke,
                cassette_path=FIXTURE_CASSETTES / "openai_chat.json",
                sqlite_path=db,
                case_token="openai",
            )
            await _assert_cassette_telemetry(
                smoke=smoke,
                cassette_path=FIXTURE_CASSETTES / "anthropic_chat.json",
                sqlite_path=db,
                case_token="anthropic",
            )
            await _assert_cassette_telemetry(
                smoke=smoke,
                cassette_path=FIXTURE_CASSETTES / "openai_compatible_chat.json",
                sqlite_path=db,
                case_token="oai_compat",
            )

    with smoke.case("a9_3_tier_escalation_path", task="A9.3", feature="llm_orchestrator"):
        provider = _TierEscalationMockProvider()
        orchestrator = LLMOrchestrator(
            model=provider.default_model,
            config=OrchestratorConfig(
                maxRoundTrips=12,
                maxNoProgressRetries=2,
                maxTierEscalations=2,
                escalateAfterNoProgress=1,
                reviewAfterSuccess=True,
            ),
        )

        async def _dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "tool": name, "echo": arguments}

        result = await orchestrator.run_step(
            provider=provider,
            run_id=generate_run_id(),
            step={"stepId": "step_a9_tier", "mode": "action", "action": "click"},
            tool_dispatcher=_dispatch,
            task_prompt="Exercise tier escalation.",
            initial_tier=ContextTier.TIER_0,
        )
        smoke.check(result.success, f"Expected success, got {result.stop_reason}")
        smoke.check(
            result.escalation_path == [ContextTier.TIER_0, ContextTier.TIER_1, ContextTier.TIER_2],
            f"Unexpected escalation_path: {result.escalation_path}",
        )
        smoke.check(
            result.final_tier == ContextTier.TIER_2,
            f"Expected final tier 2, got {result.final_tier}",
        )
        for call in result.llm_calls:
            smoke.check(
                call.context_tier != ContextTier.TIER_3,
                "Tier 3 must not appear when maxTierEscalations caps at tier 2",
            )

    with smoke.case("a9_3_tier3_only_when_forced", task="A9.3", feature="llm_orchestrator"):
        provider = _NoProgressMockProvider()
        orchestrator = LLMOrchestrator(
            model=provider.default_model,
            config=OrchestratorConfig(
                maxRoundTrips=4,
                maxNoProgressRetries=2,
                maxTierEscalations=0,
                escalateAfterNoProgress=1,
            ),
        )
        result = await orchestrator.run_step(
            provider=provider,
            run_id=generate_run_id(),
            step={"stepId": "step_a9_t3", "mode": "action", "action": "click"},
            tool_dispatcher=lambda *_a, **_k: {"ok": True},
            task_prompt="Forced tier 3 only.",
            initial_tier=ContextTier.TIER_3,
        )
        smoke.check(
            result.escalation_path == [ContextTier.TIER_3],
            f"Expected only forced tier3 path, got {result.escalation_path}",
        )
        smoke.check(all(c.context_tier == ContextTier.TIER_3 for c in result.llm_calls), "All calls must stay at tier 3")

    with smoke.case("a9_4_no_progress_budget", task="A9.4", feature="llm_orchestrator"):
        provider = _NoProgressMockProvider()
        orchestrator = LLMOrchestrator(
            model=provider.default_model,
            config=OrchestratorConfig(
                maxRoundTrips=20,
                maxNoProgressRetries=2,
                maxTierEscalations=0,
                escalateAfterNoProgress=1,
            ),
        )
        result = await orchestrator.run_step(
            provider=provider,
            run_id=generate_run_id(),
            step={"stepId": "step_a9_stall", "mode": "action", "action": "click"},
            tool_dispatcher=lambda *_a, **_k: {"ok": True},
            task_prompt="Stall forever.",
            initial_tier=ContextTier.TIER_0,
        )
        smoke.check(not result.success, "Expected unsuccessful orchestration")
        smoke.check(
            result.stop_reason == "no_progress_budget_exhausted",
            f"Expected no_progress_budget_exhausted, got {result.stop_reason}",
        )

    with smoke.case("a9_5_mode_switch_hybrid", task="A9.5", feature="mode_switch"):
        with tempfile.TemporaryDirectory(prefix="a9-mode-") as tmp:
            root = Path(tmp).resolve()
            db = root / "events.sqlite"
            runs_root = root / "runs"
            run_id = generate_run_id()
            session_id = "browser_session_a9"
            tab_id = "tab_a9_stable"
            controller = ModeController(initial_mode=RuntimeMode.HYBRID)
            switch = await controller.switch_mode(
                target_mode=RuntimeMode.MANUAL,
                reason="a9_hybrid_toggle_smoke",
                actor="smoke_a9",
                binding=RuntimeBinding(
                    run_id=run_id,
                    current_step_id="step_a9_mode",
                    browser_session_id=session_id,
                    tab_id=tab_id,
                ),
                sqlite_path=db,
                runs_root=runs_root,
            )
            smoke.check(switch.changed, "Expected mode change")
            smoke.check(not switch.runtime_state_reset, "runtime_state_reset must stay false")
            events = await EventRepository(sqlite_path=db).load_for_run(run_id)
            mode_events = [e for e in events if e.type == EventType.MODE_SWITCHED]
            smoke.check(len(mode_events) == 1, f"Expected 1 mode_switched event, got {len(mode_events)}")
            ev = mode_events[0]
            smoke.check(ev.actor == "smoke_a9", ev.actor)
            payload = ev.payload
            smoke.check(payload.get("reason") == "a9_hybrid_toggle_smoke", payload)
            smoke.check(payload.get("runtimeStateReset") is False, payload)
            smoke.check(payload.get("browserSessionId") == session_id, payload)
            smoke.check(payload.get("tabId") == tab_id, payload)
            smoke.check(payload.get("previousMode") == "hybrid", payload)
            smoke.check(payload.get("newMode") == "manual", payload)

    return smoke.finalize()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

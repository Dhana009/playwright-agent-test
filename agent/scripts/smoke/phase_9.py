from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(SCRIPT_DIR))

from _runner import SmokeRunner  # noqa: E402
from agent.core.config import Settings  # noqa: E402
from agent.core.ids import generate_run_id  # noqa: E402
from agent.core.mode import ModeController, RuntimeBinding, RuntimeMode  # noqa: E402
from agent.llm.context import StagedContextBuilder  # noqa: E402
from agent.llm.orchestrator import LLMOrchestrator  # noqa: E402
from agent.llm.provider import LLMProviderError, build_provider_from_settings  # noqa: E402
from agent.llm.provider import LLMProvider, LLMResponse, LLMToolCall, LLMUsage  # noqa: E402
from agent.storage.repos.telemetry import TelemetryRepository  # noqa: E402
from agent.telemetry.models import CallPurpose, ContextTier  # noqa: E402


@dataclass
class SmokeResult:
    provider: str
    ok: bool
    detail: str


def _load_env_files() -> None:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    load_dotenv(PROJECT_ROOT / ".env.test", override=False)


async def _run_provider_smoke(
    *,
    settings: Settings,
    provider_name: str,
    prompt: str,
    timeout_seconds: float,
    api_base: str | None = None,
    telemetry_repository: TelemetryRepository | None = None,
) -> SmokeResult:
    llm_settings = settings.llm.model_copy(
        update={
            "provider": provider_name,
            "api_base": api_base if api_base is not None else settings.llm.api_base,
        }
    )

    try:
        provider = build_provider_from_settings(
            llm_settings,
            timeout_seconds=timeout_seconds,
            telemetry_repository=telemetry_repository,
        )
    except LLMProviderError as exc:
        return SmokeResult(provider=provider_name, ok=False, detail=f"setup failed: {exc}")

    run_id = generate_run_id()
    try:
        response = await provider.chat(
            [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            run_id=run_id,
            call_purpose=CallPurpose.PLAN,
            context_tier=ContextTier.TIER_0,
        )
    except Exception as exc:  # pragma: no cover - manual smoke script
        return SmokeResult(provider=provider_name, ok=False, detail=f"request failed: {exc}")

    preview = (response.content or "").strip()
    preview = preview[:80] + ("..." if len(preview) > 80 else "")
    telemetry_marker = "telemetry=missing"
    if response.llm_call is not None:
        telemetry_marker = "telemetry=ok"
    summary_marker = "summary=missing"
    if response.run_summary is not None:
        summary_marker = f"summary_calls={response.run_summary.total_llm_calls}"
    return SmokeResult(
        provider=provider_name,
        ok=True,
        detail=(
            f"finish_reason={response.finish_reason} {telemetry_marker} "
            f"{summary_marker} text_preview={preview!r}"
        ),
    )


async def main() -> int:
    _load_env_files()

    parser = argparse.ArgumentParser(description="Phase 9 smoke: provider abstraction adapters.")
    parser.add_argument(
        "--prompt",
        default="Reply with one short sentence saying provider adapter is alive.",
        help="Prompt sent to each provider.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="Per-call timeout.",
    )
    parser.add_argument(
        "--openai-compatible-base",
        default=None,
        help="OpenAI-compatible base URL (LM Studio etc).",
    )
    args = parser.parse_args()

    runner = SmokeRunner(phase="T8", run_id=generate_run_id(), default_task="T8.1")

    settings: Settings | None = None
    with runner.case("settings_load", task="T8.1", feature="provider_adapters", error_class="config"):
        settings = Settings.load()

    if settings is None:
        return runner.finalize()

    with runner.case("context_builder", task="T8.2", feature="context_escalation"):
        context_details = _run_context_builder_smoke(model=settings.llm.model)
        runner.check(len(context_details) == 4, f"Expected 4 escalation tiers, got {len(context_details)}")
        for detail in context_details:
            print(detail)

    with runner.case("mode_switch", task="T8.3", feature="mode_switch"):
        mode_result = await _run_mode_switch_smoke(settings=settings)
        runner.check(mode_result.ok, mode_result.detail)
        print(mode_result.detail)

    with runner.case("orchestrator", task="T8.4", feature="orchestrator"):
        orchestrator_result = await _run_orchestrator_smoke()
        runner.check(orchestrator_result.ok, orchestrator_result.detail)
        print(orchestrator_result.detail)

    telemetry_repository = TelemetryRepository(sqlite_path=settings.storage.sqlite_path)
    openai_compatible_base = (
        args.openai_compatible_base
        or settings.llm.api_base
        or os.getenv("OPENAI_COMPATIBLE_API_BASE")
        or os.getenv("LM_STUDIO_API_BASE")
    )

    provider_checks = [
    ]
    skipped_provider_reasons: dict[str, str] = {}

    if os.getenv("OPENAI_API_KEY"):
        provider_checks.append(
            _run_provider_smoke(
                settings=settings,
                provider_name="openai",
                prompt=args.prompt,
                timeout_seconds=args.timeout_seconds,
                telemetry_repository=telemetry_repository,
            )
        )
    else:
        skipped_provider_reasons["openai"] = "OPENAI_API_KEY is not set"

    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        provider_checks.append(
            _run_provider_smoke(
                settings=settings,
                provider_name="anthropic",
                prompt=args.prompt,
                timeout_seconds=args.timeout_seconds,
                telemetry_repository=telemetry_repository,
            )
        )
    else:
        skipped_provider_reasons["anthropic"] = (
            "ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is not set"
        )

    if openai_compatible_base:
        provider_checks.append(
            _run_provider_smoke(
                settings=settings,
                provider_name="openai_compatible",
                prompt=args.prompt,
                timeout_seconds=args.timeout_seconds,
                api_base=openai_compatible_base,
                telemetry_repository=telemetry_repository,
            )
        )
    else:
        skipped_provider_reasons["openai_compatible"] = (
            "OPENAI_COMPATIBLE_API_BASE/LM_STUDIO_API_BASE is not set"
        )

    if not provider_checks:
        with runner.case(
            "provider_configuration",
            task="T8.1",
            feature="provider_adapters",
            error_class="config",
        ):
            raise RuntimeError(
                "No provider credentials are configured. "
                "Set at least one provider auth value in .env/.env.test."
            )

    for provider_name, reason in skipped_provider_reasons.items():
        with runner.case(
            f"provider_{provider_name}_skipped",
            task="T8.1",
            feature="provider_adapters",
            error_class="config",
        ):
            print(f"skipped: {reason}")

    if provider_checks:
        provider_results = await asyncio.gather(*provider_checks)
    else:
        provider_results = []

    for provider_result in provider_results:
        with runner.case(
            f"provider_{provider_result.provider}",
            task="T8.1",
            feature="provider_adapters",
            error_class="runtime",
        ):
            runner.check(provider_result.ok, provider_result.detail)
            print(provider_result.detail)

    return runner.finalize()


def _run_context_builder_smoke(*, model: str) -> list[str]:
    builder = StagedContextBuilder(model=model, default_output_tokens=256)
    sequence = builder.build_escalation_sequence(
        target_tier=ContextTier.TIER_3,
        step={
            "stepId": "step_sample",
            "mode": "action",
            "action": "click",
            "metadata": {"semanticTarget": "submit login"},
        },
        outcome={"status": "failed", "reason": "not_found"},
        scoped_target={
            "semanticKey": "submit_button",
            "candidateSelectors": ["[data-testid='submit']", "button[type='submit']"],
        },
        history=[
            {"stepId": "step_prev_1", "status": "succeeded"},
            {"stepId": "step_prev_2", "status": "failed", "reason": "timeout"},
        ],
        contradictions=[
            {"contradictionType": "stale_locator", "decision": "accept_new"},
        ],
        full_snapshot={
            "url": "https://example.test/login",
            "domHash": "abc123",
            "ariaYaml": "root:\n  - button: Submit",
        },
        system_prompt="You are a Playwright execution planner.",
        task_prompt="Decide next safe action.",
    )
    details: list[str] = []
    for result in sequence:
        details.append(
            "tier="
            f"{result.tier} sections={','.join(result.included_sections)} "
            f"input_tokens={result.preflight_input_tokens} "
            f"output_tokens={result.preflight_output_tokens}"
        )
    return details


class _MockOrchestratorProvider(LLMProvider):
    def __init__(self) -> None:
        self._round = 0

    @property
    def provider_name(self) -> str:
        return "mock_provider"

    @property
    def default_model(self) -> str:
        return "mock-model"

    async def chat(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        metadata: dict[str, object] | None = None,
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
        self._round += 1
        if call_purpose == CallPurpose.REVIEW:
            return LLMResponse(
                provider=self.provider_name,
                model=model or self.default_model,
                content="review completed",
                finish_reason="stop",
                usage=LLMUsage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
            )

        if self._round == 1:
            return LLMResponse(
                provider=self.provider_name,
                model=model or self.default_model,
                content="executing click tool",
                finish_reason="tool_calls",
                tool_calls=[
                    LLMToolCall(
                        id="mock_tool_call_1",
                        name="click",
                        arguments='{"tab_id":"tab_demo","target":"button[data-testid=\\"submit\\"]"}',
                    )
                ],
                usage=LLMUsage(prompt_tokens=12, completion_tokens=8, total_tokens=20),
            )

        return LLMResponse(
            provider=self.provider_name,
            model=model or self.default_model,
            content="no additional tools",
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=4, completion_tokens=3, total_tokens=7),
        )


async def _run_orchestrator_smoke() -> SmokeResult:
    provider = _MockOrchestratorProvider()
    orchestrator = LLMOrchestrator(model=provider.default_model)

    async def _tool_dispatcher(name: str, arguments: dict[str, object]) -> dict[str, object]:
        return {
            "ok": True,
            "tool": name,
            "echo": arguments,
        }

    result = await orchestrator.run_step(
        provider=provider,
        run_id=generate_run_id(),
        step={
            "stepId": "step_orchestrator_demo",
            "mode": "action",
            "action": "click",
            "metadata": {"intent": "submit login"},
        },
        tool_dispatcher=_tool_dispatcher,
        task_prompt="Complete the step safely using available tools.",
        initial_tier=ContextTier.TIER_0,
    )

    return SmokeResult(
        provider="orchestrator",
        ok=result.success,
        detail=(
            f"stop_reason={result.stop_reason} rounds={result.rounds_executed} "
            f"final_tier={result.final_tier}"
        ),
    )


async def _run_mode_switch_smoke(*, settings: Settings) -> SmokeResult:
    initial_mode = RuntimeMode(settings.mode)
    target_mode = RuntimeMode.HYBRID if initial_mode != RuntimeMode.HYBRID else RuntimeMode.MANUAL
    controller = ModeController(initial_mode=initial_mode)
    run_id = generate_run_id()
    result = await controller.switch_mode(
        target_mode=target_mode,
        reason="phase_9_mode_switch_smoke",
        actor="smoke",
        binding=RuntimeBinding(
            run_id=run_id,
            current_step_id="step_mode_switch_smoke",
            browser_session_id="browser_session_smoke",
            tab_id="tab_smoke",
        ),
        sqlite_path=settings.storage.sqlite_path,
    )

    ok = bool(result.changed and not result.runtime_state_reset and result.event_id)
    detail = (
        f"previous={result.previous_mode} active={result.active_mode} "
        f"runtime_state_reset={result.runtime_state_reset} event_id={result.event_id}"
    )
    return SmokeResult(provider="mode_switch", ok=ok, detail=detail)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

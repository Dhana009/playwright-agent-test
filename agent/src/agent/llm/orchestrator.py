from __future__ import annotations

# Ported from Hermes-Agent/run_agent.py — adapted for agent/

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.core.ids import generate_event_id
from agent.core.logging import get_logger
from agent.llm.context import StagedContextBuilder, TokenPreflightEstimator
from agent.llm.provider import LLMProvider, LLMResponse
from agent.telemetry.models import CallPurpose, ContextTier, LLMCall

if TYPE_CHECKING:
    from agent.execution.tools import ToolRuntime

ToolDispatcher = Callable[[str, dict[str, Any]], Awaitable[Any]]

PHASE3_TOOL_NAMES: tuple[str, ...] = (
    "navigate",
    "navigate_back",
    "wait_for",
    "wait_timeout",
    "click",
    "fill",
    "type",
    "press",
    "check",
    "uncheck",
    "select",
    "upload",
    "drag",
    "hover",
    "focus",
    "tabs_list",
    "tabs_select",
    "tabs_close",
    "console_messages",
    "network_requests",
    "screenshot",
    "take_trace",
    "assert_visible",
    "assert_text",
    "assert_url",
    "assert_title",
    "assert_value",
    "assert_checked",
    "assert_enabled",
    "assert_hidden",
    "assert_count",
    "assert_in_viewport",
    "dialog_handle",
    "frame_enter",
    "frame_exit",
)


class OrchestratorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_round_trips: int = Field(default=8, ge=1, alias="maxRoundTrips")
    max_no_progress_retries: int = Field(default=2, ge=0, alias="maxNoProgressRetries")
    max_tier_escalations: int = Field(default=3, ge=0, alias="maxTierEscalations")
    escalate_after_no_progress: int = Field(default=1, ge=1, alias="escalateAfterNoProgress")
    max_tool_calls_per_round: int = Field(default=8, ge=1, alias="maxToolCallsPerRound")
    review_after_success: bool = Field(default=True, alias="reviewAfterSuccess")
    default_output_tokens: int = Field(default=512, ge=0, alias="defaultOutputTokens")


class ToolExecutionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    tool_call_id: str = Field(alias="toolCallId")
    tool_name: str = Field(alias="toolName")
    arguments: dict[str, Any]
    ok: bool
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class OrchestratorResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)

    success: bool
    stop_reason: str = Field(alias="stopReason")
    final_response: str | None = Field(default=None, alias="finalResponse")
    rounds_executed: int = Field(default=0, alias="roundsExecuted")
    final_tier: ContextTier = Field(alias="finalTier")
    escalation_path: list[ContextTier] = Field(default_factory=list, alias="escalationPath")
    llm_calls: list[LLMCall] = Field(default_factory=list, alias="llmCalls")
    tool_executions: list[ToolExecutionRecord] = Field(default_factory=list, alias="toolExecutions")
    last_outcome: dict[str, Any] = Field(default_factory=dict, alias="lastOutcome")


@dataclass(slots=True)
class _LoopState:
    phase: Literal["plan", "classification", "repair", "review"] = "plan"
    no_progress_retries: int = 0
    tier_escalations: int = 0


class LLMOrchestrator:
    def __init__(
        self,
        *,
        model: str,
        config: OrchestratorConfig | None = None,
    ) -> None:
        self._logger = get_logger(__name__)
        self._model = model
        self._config = config or OrchestratorConfig()
        self._context_builder = StagedContextBuilder(
            model=model,
            default_output_tokens=self._config.default_output_tokens,
        )
        self._preflight_estimator = TokenPreflightEstimator(
            model=model,
            default_output_tokens=self._config.default_output_tokens,
        )

    async def run_step(
        self,
        *,
        provider: LLMProvider,
        run_id: str,
        step: Any,
        tool_dispatcher: ToolDispatcher | None = None,
        runtime: ToolRuntime | None = None,
        tool_definitions: list[dict[str, Any]] | None = None,
        scoped_target: Any | None = None,
        history: list[Any] | None = None,
        contradictions: list[Any] | None = None,
        full_snapshot: Any | None = None,
        system_prompt: str | None = None,
        task_prompt: str | None = None,
        initial_tier: ContextTier = ContextTier.TIER_0,
        max_output_tokens: int | None = None,
    ) -> OrchestratorResult:
        dispatcher = tool_dispatcher
        if dispatcher is None:
            if runtime is None:
                raise ValueError("Provide either tool_dispatcher or runtime.")
            dispatcher = build_phase3_tool_dispatcher(runtime)

        available_tools = tool_definitions or build_phase3_tool_definitions()
        step_id = _extract_step_id(step)
        outcome: dict[str, Any] = {}
        escalation_path: list[ContextTier] = [initial_tier]
        llm_calls: list[LLMCall] = []
        tool_executions: list[ToolExecutionRecord] = []
        state = _LoopState()

        current_tier = initial_tier
        base_context = self._context_builder.build_for_tier(
            tier=current_tier,
            step=step,
            outcome=outcome or None,
            scoped_target=scoped_target,
            history=history or [],
            contradictions=contradictions or [],
            full_snapshot=full_snapshot,
            system_prompt=system_prompt,
            task_prompt=task_prompt,
            output_tokens=max_output_tokens,
        )
        messages = list(base_context.messages)

        for round_index in range(1, self._config.max_round_trips + 1):
            call_purpose = _phase_to_call_purpose(state.phase)
            call_messages = list(messages)
            phase_hint = _phase_hint(
                phase=state.phase,
                outcome=outcome,
                tool_executions=tool_executions,
            )
            if phase_hint is not None:
                call_messages.append({"role": "user", "content": phase_hint})

            preflight = self._preflight_estimator.preflight(
                call_messages,
                output_tokens=max_output_tokens,
            )
            response = await provider.chat(
                call_messages,
                tools=available_tools,
                model=self._model,
                run_id=run_id,
                step_id=step_id,
                call_purpose=call_purpose,
                context_tier=current_tier,
                escalation_path=escalation_path,
                preflight_input_tokens=preflight.input_tokens,
                preflight_output_tokens=preflight.output_tokens,
                no_progress_retry=state.no_progress_retries > 0,
                metadata={
                    "orchestratorRound": round_index,
                    "orchestratorPhase": state.phase,
                },
            )
            if response.llm_call is not None:
                llm_calls.append(response.llm_call)

            messages.append(_assistant_message(response))
            round_progress = False
            round_failures: list[ToolExecutionRecord] = []
            response_text = (response.content or "").strip()

            if response.tool_calls:
                for raw_tool_call in response.tool_calls[: self._config.max_tool_calls_per_round]:
                    tool_call_id = raw_tool_call.id or f"toolcall_{generate_event_id()}"
                    tool_name = raw_tool_call.name
                    parsed_args = _parse_tool_arguments(raw_tool_call.arguments)
                    try:
                        tool_result = await dispatcher(tool_name, parsed_args)
                        normalized_result = _normalize_for_json(tool_result)
                        ok = _tool_ok(normalized_result)
                        record = ToolExecutionRecord(
                            toolCallId=tool_call_id,
                            toolName=tool_name,
                            arguments=parsed_args,
                            ok=ok,
                            result=normalized_result if isinstance(normalized_result, dict) else {"value": normalized_result},
                        )
                    except Exception as exc:
                        record = ToolExecutionRecord(
                            toolCallId=tool_call_id,
                            toolName=tool_name,
                            arguments=parsed_args,
                            ok=False,
                            result={},
                            error=str(exc),
                        )
                    tool_executions.append(record)
                    if record.ok:
                        round_progress = True
                    else:
                        round_failures.append(record)

                    tool_payload = {
                        "ok": record.ok,
                        "tool": record.tool_name,
                        "result": record.result,
                    }
                    if record.error is not None:
                        tool_payload["error"] = record.error
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": record.tool_call_id,
                            "content": json.dumps(tool_payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
                        }
                    )

                if round_failures:
                    outcome = {
                        "status": "failed",
                        "phase": state.phase,
                        "round": round_index,
                        "failures": [item.model_dump(mode="json", by_alias=True) for item in round_failures],
                    }
                    if state.phase == "classification":
                        state.phase = "repair"
                    elif state.phase == "repair":
                        state.phase = "repair"
                    else:
                        state.phase = "classification"
                else:
                    outcome = {
                        "status": "succeeded",
                        "phase": state.phase,
                        "round": round_index,
                        "executedTools": [
                            item.model_dump(mode="json", by_alias=True)
                            for item in tool_executions[-len(response.tool_calls) :]
                        ],
                    }
                    if self._config.review_after_success:
                        state.phase = "review"
                    else:
                        return OrchestratorResult(
                            success=True,
                            stopReason="step_succeeded",
                            finalResponse=response_text or "Step completed.",
                            roundsExecuted=round_index,
                            finalTier=current_tier,
                            escalationPath=escalation_path,
                            llmCalls=llm_calls,
                            toolExecutions=tool_executions,
                            lastOutcome=outcome,
                        )
            else:
                if response_text:
                    round_progress = True

                if state.phase == "review":
                    return OrchestratorResult(
                        success=True,
                        stopReason="review_completed",
                        finalResponse=response_text or "Review completed.",
                        roundsExecuted=round_index,
                        finalTier=current_tier,
                        escalationPath=escalation_path,
                        llmCalls=llm_calls,
                        toolExecutions=tool_executions,
                        lastOutcome=outcome,
                    )
                if state.phase == "classification":
                    state.phase = "repair"

            if round_progress:
                state.no_progress_retries = 0
            else:
                state.no_progress_retries += 1

            if state.no_progress_retries > self._config.max_no_progress_retries:
                return OrchestratorResult(
                    success=False,
                    stopReason="no_progress_budget_exhausted",
                    finalResponse=response_text or None,
                    roundsExecuted=round_index,
                    finalTier=current_tier,
                    escalationPath=escalation_path,
                    llmCalls=llm_calls,
                    toolExecutions=tool_executions,
                    lastOutcome=outcome,
                )

            if (
                state.no_progress_retries >= self._config.escalate_after_no_progress
                and state.tier_escalations < self._config.max_tier_escalations
            ):
                next_tier = _next_context_tier(current_tier)
                if next_tier is not None:
                    state.tier_escalations += 1
                    current_tier = next_tier
                    escalation_path.append(current_tier)
                    rebuilt = self._context_builder.build_for_tier(
                        tier=current_tier,
                        step=step,
                        outcome=outcome or None,
                        scoped_target=scoped_target,
                        history=history or [],
                        contradictions=contradictions or [],
                        full_snapshot=full_snapshot,
                        system_prompt=system_prompt,
                        task_prompt=task_prompt,
                        output_tokens=max_output_tokens,
                    )
                    messages = list(rebuilt.messages)
                    state.phase = "repair"
                    state.no_progress_retries = 0

            self._logger.info(
                "llm_orchestrator_round_completed",
                run_id=run_id,
                step_id=step_id,
                round=round_index,
                phase=state.phase,
                tier=current_tier.value,
                no_progress_retries=state.no_progress_retries,
            )

        return OrchestratorResult(
            success=False,
            stopReason="max_round_trips_exhausted",
            finalResponse=None,
            roundsExecuted=self._config.max_round_trips,
            finalTier=current_tier,
            escalationPath=escalation_path,
            llmCalls=llm_calls,
            toolExecutions=tool_executions,
            lastOutcome=outcome,
        )


def build_phase3_tool_dispatcher(runtime: ToolRuntime) -> ToolDispatcher:
    from agent.execution import tools as execution_tools

    handlers = {
        tool_name: getattr(execution_tools, tool_name)
        for tool_name in PHASE3_TOOL_NAMES
        if hasattr(execution_tools, tool_name)
    }

    async def _dispatch(tool_name: str, arguments: dict[str, Any]) -> Any:
        handler = handlers.get(tool_name)
        if handler is None:
            raise ValueError(f"Unknown Phase 3 tool '{tool_name}'.")
        return await handler(runtime, **arguments)

    return _dispatch


def build_phase3_tool_definitions(
    tool_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    names = tool_names or list(PHASE3_TOOL_NAMES)
    return [
        {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": f"Playwright Phase 3 tool: {tool_name}",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True,
                },
            },
        }
        for tool_name in names
    ]


def _phase_to_call_purpose(phase: str) -> CallPurpose:
    if phase == "repair":
        return CallPurpose.REPAIR
    if phase == "classification":
        return CallPurpose.CLASSIFICATION
    if phase == "review":
        return CallPurpose.REVIEW
    return CallPurpose.PLAN


def _phase_hint(
    *,
    phase: str,
    outcome: dict[str, Any],
    tool_executions: list[ToolExecutionRecord],
) -> str | None:
    if phase == "classification":
        return (
            "Classify the most recent failure and explain a safe repair strategy. "
            f"Latest outcome JSON: {json.dumps(outcome, ensure_ascii=True)}"
        )
    if phase == "repair":
        return (
            "Attempt a repair using available tools with minimal side effects. "
            f"Latest outcome JSON: {json.dumps(outcome, ensure_ascii=True)}"
        )
    if phase == "review":
        recent = [item.model_dump(mode="json", by_alias=True) for item in tool_executions[-3:]]
        return (
            "Review the executed tool results and summarize whether the step can be considered complete. "
            f"Recent tools JSON: {json.dumps(recent, ensure_ascii=True)}"
        )
    return None


def _assistant_message(response: LLMResponse) -> dict[str, Any]:
    tool_calls: list[dict[str, Any]] = []
    for tool_call in response.tool_calls:
        tool_call_id = tool_call.id or f"toolcall_{generate_event_id()}"
        tool_calls.append(
            {
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                },
            }
        )
    return {
        "role": "assistant",
        "content": response.content or "",
        "tool_calls": tool_calls or None,
    }


def _parse_tool_arguments(raw_arguments: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _normalize_for_json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, dict):
        return {str(key): _normalize_for_json(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_normalize_for_json(inner) for inner in value]
    if isinstance(value, tuple):
        return [_normalize_for_json(inner) for inner in value]
    if hasattr(value, "model_dump") and callable(getattr(value, "model_dump")):
        return value.model_dump(mode="json", by_alias=True)
    return value


def _tool_ok(result: Any) -> bool:
    if isinstance(result, dict):
        ok_value = result.get("ok")
        if isinstance(ok_value, bool):
            return ok_value
        return True
    if hasattr(result, "ok"):
        try:
            return bool(getattr(result, "ok"))
        except Exception:
            return False
    return True


def _next_context_tier(current_tier: ContextTier) -> ContextTier | None:
    if current_tier == ContextTier.TIER_0:
        return ContextTier.TIER_1
    if current_tier == ContextTier.TIER_1:
        return ContextTier.TIER_2
    if current_tier == ContextTier.TIER_2:
        return ContextTier.TIER_3
    return None


def _extract_step_id(step: Any) -> str | None:
    if isinstance(step, dict):
        raw_step_id = step.get("stepId") or step.get("step_id")
        return str(raw_step_id) if raw_step_id else None
    if hasattr(step, "step_id"):
        raw_step_id = getattr(step, "step_id")
        return str(raw_step_id) if raw_step_id else None
    return None

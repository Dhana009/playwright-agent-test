from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.core.ids import generate_repair_id
from agent.core.logging import get_logger
from agent.memory.models import LearnedRepair, RepairLifecycleState
from agent.storage.repos.memory import MemoryRepository


@dataclass(frozen=True)
class RepairLifecyclePolicy:
    promote_after_successes: int = 3
    degrade_after_failures: int = 2
    retire_after_failures: int = 4
    revalidate_after_successes: int | None = None

    def __post_init__(self) -> None:
        if self.promote_after_successes <= 0:
            raise ValueError("promote_after_successes must be a positive integer")
        if self.degrade_after_failures <= 0:
            raise ValueError("degrade_after_failures must be a positive integer")
        if self.retire_after_failures <= 0:
            raise ValueError("retire_after_failures must be a positive integer")
        if self.retire_after_failures <= self.degrade_after_failures:
            raise ValueError("retire_after_failures must be greater than degrade_after_failures")
        if (
            self.revalidate_after_successes is not None
            and self.revalidate_after_successes <= 0
        ):
            raise ValueError("revalidate_after_successes must be positive when provided")

    @property
    def revalidate_threshold(self) -> int:
        return self.revalidate_after_successes or self.promote_after_successes


@dataclass
class LearnedRepairPersistence:
    repo: MemoryRepository


class LearnedRepairStore:
    """
    Learned repair store with scoped-key persistence and lifecycle gates.
    """

    def __init__(
        self,
        persistence: LearnedRepairPersistence,
        *,
        lifecycle_policy: RepairLifecyclePolicy | None = None,
    ) -> None:
        self._logger = get_logger(__name__)
        self._p = persistence
        self._policy = lifecycle_policy or RepairLifecyclePolicy()

    @classmethod
    def create(
        cls,
        *,
        sqlite_path: str | Path | None = None,
        lifecycle_policy: RepairLifecyclePolicy | None = None,
    ) -> "LearnedRepairStore":
        return cls(
            persistence=LearnedRepairPersistence(
                repo=MemoryRepository(sqlite_path=sqlite_path),
            ),
            lifecycle_policy=lifecycle_policy,
        )

    async def record_manual_fix_candidate(
        self,
        *,
        domain: str,
        normalized_route_template: str,
        frame_context: list[str] | None,
        target_semantic_key: str | None,
        source_run_id: str,
        source_step_id: str,
        actor: str,
        confidence_score: float,
        app_version: str | None = None,
        rollback_ref: str | None = None,
        expires_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
        repair_id: str | None = None,
    ) -> LearnedRepair:
        normalized_frame_context = list(frame_context or [])
        repair = LearnedRepair(
            repairId=repair_id or generate_repair_id(),
            domain=domain,
            normalizedRouteTemplate=normalized_route_template,
            frameContext=normalized_frame_context,
            targetSemanticKey=target_semantic_key,
            appVersion=app_version,
            scopeKey=build_scope_key(
                domain=domain,
                normalized_route_template=normalized_route_template,
                frame_context=normalized_frame_context,
                target_semantic_key=target_semantic_key,
            ),
            state=RepairLifecycleState.CANDIDATE,
            sourceRunId=source_run_id,
            sourceStepId=source_step_id,
            actor=actor,
            confidenceScore=confidence_score,
            rollbackRef=rollback_ref,
            expiresAt=expires_at,
            metadata=dict(metadata or {}),
        )
        await self._p.repo.save_learned_repair(repair)
        self._logger.debug(
            "learned_repair_candidate_recorded",
            repair_id=repair.repair_id,
            scope_key=repair.scope_key,
            source_run_id=repair.source_run_id,
            source_step_id=repair.source_step_id,
        )
        return repair

    async def upsert(self, repair: LearnedRepair) -> LearnedRepair:
        await self._p.repo.save_learned_repair(repair)
        return repair

    async def get(self, repair_id: str) -> LearnedRepair | None:
        return await self._p.repo.load_learned_repair(repair_id)

    async def list(
        self,
        *,
        source_run_id: str | None = None,
        source_step_id: str | None = None,
        scope_key: str | None = None,
        state: RepairLifecycleState | str | None = None,
        limit: int = 500,
    ) -> list[LearnedRepair]:
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        state_value = state.value if isinstance(state, RepairLifecycleState) else state
        return await self._p.repo.load_learned_repairs(
            source_run_id=source_run_id,
            source_step_id=source_step_id,
            scope_key=scope_key,
            state=state_value,
            limit=limit,
        )

    async def record_validation(
        self,
        *,
        repair_id: str,
        succeeded: bool,
        validated_at: datetime | None = None,
    ) -> LearnedRepair:
        repair = await self._p.repo.load_learned_repair(repair_id)
        if repair is None:
            raise ValueError(f"Unknown learned repair id: {repair_id}")

        if repair.state == RepairLifecycleState.RETIRED:
            return repair

        success_count = repair.validation_success_count + 1 if succeeded else 0
        failure_count = repair.validation_failure_count + 1 if not succeeded else 0
        next_state = _resolve_state(
            current_state=repair.state,
            success_count=success_count,
            failure_count=failure_count,
            policy=self._policy,
        )
        updated_repair = repair.model_copy(
            update={
                "validation_success_count": success_count,
                "validation_failure_count": failure_count,
                "last_validated_at": validated_at or datetime.now(UTC),
                "state": next_state,
            }
        )
        await self._p.repo.save_learned_repair(updated_repair)
        self._logger.debug(
            "learned_repair_validation_recorded",
            repair_id=repair_id,
            succeeded=succeeded,
            previous_state=repair.state,
            next_state=next_state,
            validation_success_count=success_count,
            validation_failure_count=failure_count,
        )
        return updated_repair


def build_scope_key(
    *,
    domain: str,
    normalized_route_template: str,
    frame_context: list[str] | None,
    target_semantic_key: str | None,
) -> str:
    frame_segment = "/".join(frame_context or []) if frame_context else "main"
    semantic_segment = target_semantic_key or "__route_scoped__"
    return f"{domain}|{normalized_route_template}|{frame_segment}|{semantic_segment}"


def _resolve_state(
    *,
    current_state: RepairLifecycleState,
    success_count: int,
    failure_count: int,
    policy: RepairLifecyclePolicy,
) -> RepairLifecycleState:
    if current_state == RepairLifecycleState.CANDIDATE:
        if success_count >= policy.promote_after_successes:
            return RepairLifecycleState.TRUSTED
        return RepairLifecycleState.CANDIDATE

    if current_state == RepairLifecycleState.TRUSTED:
        if failure_count >= policy.degrade_after_failures:
            return RepairLifecycleState.DEGRADED
        return RepairLifecycleState.TRUSTED

    if current_state == RepairLifecycleState.DEGRADED:
        if failure_count >= policy.retire_after_failures:
            return RepairLifecycleState.RETIRED
        if success_count >= policy.revalidate_threshold:
            return RepairLifecycleState.TRUSTED
        return RepairLifecycleState.DEGRADED

    return RepairLifecycleState.RETIRED

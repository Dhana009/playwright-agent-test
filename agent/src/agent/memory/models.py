from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RawEvidenceType(str, Enum):
    SNAPSHOT = "snapshot"
    ACCESSIBILITY_TREE = "accessibility_tree"
    TRACE = "trace"
    CONSOLE_LOG = "console_log"
    NETWORK_LOG = "network_log"
    SCREENSHOT = "screenshot"
    OTHER = "other"


class MemoryEntryType(str, Enum):
    LOCATOR_BUNDLE = "locator_bundle"
    STEP_STATE = "step_state"
    LEARNED_REPAIR = "learned_repair"
    ROUTE_SIGNATURE = "route_signature"
    OTHER = "other"


class RepairLifecycleState(str, Enum):
    CANDIDATE = "candidate"
    TRUSTED = "trusted"
    DEGRADED = "degraded"
    RETIRED = "retired"


class RawEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)

    evidence_id: str = Field(alias="evidenceId")
    run_id: str = Field(alias="runId")
    step_id: str | None = Field(default=None, alias="stepId")
    actor: str = Field(min_length=1)
    evidence_type: RawEvidenceType = Field(alias="evidenceType")
    artifact_ref: str = Field(alias="artifactRef")
    captured_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        alias="capturedAt",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class CompiledMemoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)

    entry_id: str = Field(alias="entryId")
    entry_type: MemoryEntryType = Field(alias="entryType")
    key: str
    value: dict[str, Any]
    version: int = Field(default=1, ge=1)
    raw_evidence_ids: list[str] = Field(default_factory=list, alias="rawEvidenceIds")
    confidence_score: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        alias="confidenceScore",
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        alias="updatedAt",
    )


class SchemaPolicyVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: str = Field(alias="schemaVersion")
    policy_version: str = Field(alias="policyVersion")
    config_version: str | None = Field(default=None, alias="configVersion")
    activated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        alias="activatedAt",
    )
    notes: str | None = None


class LearnedRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)

    repair_id: str = Field(alias="repairId")
    domain: str
    normalized_route_template: str = Field(alias="normalizedRouteTemplate")
    frame_context: list[str] = Field(default_factory=list, alias="frameContext")
    target_semantic_key: str | None = Field(default=None, alias="targetSemanticKey")
    app_version: str | None = Field(default=None, alias="appVersion")
    scope_key: str | None = Field(default=None, alias="scopeKey")
    state: RepairLifecycleState = RepairLifecycleState.CANDIDATE
    source_run_id: str = Field(alias="sourceRunId")
    source_step_id: str = Field(alias="sourceStepId")
    actor: str = Field(min_length=1)
    confidence_score: float = Field(alias="confidenceScore", ge=0.0, le=1.0)
    validation_success_count: int = Field(default=0, ge=0, alias="validationSuccessCount")
    validation_failure_count: int = Field(default=0, ge=0, alias="validationFailureCount")
    last_validated_at: datetime | None = Field(default=None, alias="lastValidatedAt")
    expires_at: datetime | None = Field(default=None, alias="expiresAt")
    rollback_ref: str | None = Field(default=None, alias="rollbackRef")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _hydrate_scope_key(self) -> "LearnedRepair":
        if self.scope_key:
            return self

        frame_segment = "/".join(self.frame_context) if self.frame_context else "main"
        semantic_segment = self.target_semantic_key or "__route_scoped__"
        self.scope_key = (
            f"{self.domain}|"
            f"{self.normalized_route_template}|"
            f"{frame_segment}|"
            f"{semantic_segment}"
        )
        return self

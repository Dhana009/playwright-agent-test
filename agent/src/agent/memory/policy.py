from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agent.core.config import Settings
from agent.core.logging import get_logger
from agent.memory.models import SchemaPolicyVersion
from agent.storage.files import resolve_runs_root
from agent.storage.repos._common import dumps_json


@dataclass(frozen=True)
class SchemaPolicyPersistence:
    runs_root: Path


class SchemaPolicyStore:
    """
    Schema/policy version registry controlled by explicit config versions.
    """

    def __init__(self, persistence: SchemaPolicyPersistence) -> None:
        self._logger = get_logger(__name__)
        self._p = persistence

    @classmethod
    def create(cls, *, runs_root: str | Path | None = None) -> "SchemaPolicyStore":
        return cls(
            persistence=SchemaPolicyPersistence(runs_root=resolve_runs_root(runs_root)),
        )

    @property
    def history_path(self) -> Path:
        return self._p.runs_root / "memory" / "schema_policy_versions.jsonl"

    @property
    def active_path(self) -> Path:
        return self._p.runs_root / "memory" / "schema_policy_active.json"

    async def activate(
        self,
        *,
        schema_version: str,
        policy_version: str,
        config_version: str,
        notes: str | None = None,
        activated_at: datetime | None = None,
    ) -> SchemaPolicyVersion:
        version = SchemaPolicyVersion(
            schemaVersion=schema_version,
            policyVersion=policy_version,
            configVersion=config_version,
            activatedAt=activated_at or datetime.now(UTC),
            notes=notes,
        )
        return await self.activate_version(version)

    async def activate_version(self, version: SchemaPolicyVersion) -> SchemaPolicyVersion:
        config_version = (version.config_version or "").strip()
        if not config_version:
            raise ValueError(
                "schema/policy version updates require an explicit non-empty config_version"
            )

        prepared = version.model_copy(update={"config_version": config_version})
        active = await self.get_active()
        if active is not None:
            if _same_version(active, prepared):
                return active

            if active.config_version == prepared.config_version:
                raise ValueError(
                    "config_version is already active with a different schema/policy pair; "
                    "bump config_version to change schema/policy conventions"
                )

        self._ensure_parent_dirs()
        await self._append_history(prepared)
        self.active_path.write_text(
            dumps_json(prepared.model_dump(mode="json", by_alias=True)),
            encoding="utf-8",
        )
        self._logger.info(
            "schema_policy_activated",
            schema_version=prepared.schema_version,
            policy_version=prepared.policy_version,
            config_version=prepared.config_version,
            active_path=str(self.active_path),
            history_path=str(self.history_path),
        )
        return prepared

    async def get_active(self) -> SchemaPolicyVersion | None:
        if not self.active_path.exists():
            return None
        payload = self.active_path.read_text(encoding="utf-8").strip()
        if not payload:
            return None
        return SchemaPolicyVersion.model_validate(json.loads(payload))

    async def list_versions(
        self,
        *,
        config_version: str | None = None,
        limit: int = 500,
    ) -> list[SchemaPolicyVersion]:
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        if not self.history_path.exists():
            return []

        versions: list[SchemaPolicyVersion] = []
        normalized_config = config_version.strip() if config_version else None
        with self.history_path.open("r", encoding="utf-8") as file_obj:
            lines = file_obj.readlines()

        for line in reversed(lines):
            payload = line.strip()
            if not payload:
                continue
            record = SchemaPolicyVersion.model_validate(json.loads(payload))
            if normalized_config and record.config_version != normalized_config:
                continue
            versions.append(record)
            if len(versions) >= limit:
                break
        return versions

    async def activate_from_settings(
        self,
        *,
        settings: Settings,
        schema_version: str,
        policy_version: str,
        notes: str | None = None,
        activated_at: datetime | None = None,
    ) -> SchemaPolicyVersion:
        return await self.activate(
            schema_version=schema_version,
            policy_version=policy_version,
            config_version=derive_config_version(settings),
            notes=notes,
            activated_at=activated_at,
        )

    def _ensure_parent_dirs(self) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.active_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.history_path.exists():
            self.history_path.write_text("", encoding="utf-8")

    async def _append_history(self, version: SchemaPolicyVersion) -> None:
        payload = dumps_json(version.model_dump(mode="json", by_alias=True))
        with self.history_path.open("a", encoding="utf-8") as file_obj:
            file_obj.write(payload + "\n")


def derive_config_version(settings: Settings) -> str:
    payload = dumps_json(settings.model_dump(mode="json"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"cfg_{digest[:12]}"


def _same_version(left: SchemaPolicyVersion, right: SchemaPolicyVersion) -> bool:
    return (
        left.schema_version == right.schema_version
        and left.policy_version == right.policy_version
        and left.config_version == right.config_version
    )

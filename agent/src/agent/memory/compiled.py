from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.core.ids import generate_memory_entry_id
from agent.core.logging import get_logger
from agent.memory.models import CompiledMemoryEntry, MemoryEntryType
from agent.storage.repos.memory import MemoryRepository


@dataclass
class CompiledMemoryPersistence:
    repo: MemoryRepository


class CompiledMemoryStore:
    """
    Versioned compiled-memory upserts with raw-evidence provenance checks.
    """

    def __init__(
        self,
        persistence: CompiledMemoryPersistence,
    ) -> None:
        self._logger = get_logger(__name__)
        self._p = persistence

    @classmethod
    def create(
        cls,
        *,
        sqlite_path: str | Path | None = None,
    ) -> "CompiledMemoryStore":
        return cls(
            CompiledMemoryPersistence(
                repo=MemoryRepository(sqlite_path=sqlite_path),
            )
        )

    async def upsert(
        self,
        *,
        entry_type: MemoryEntryType | str,
        key: str,
        value: dict[str, Any],
        raw_evidence_ids: list[str],
        confidence_score: float | None = None,
        entry_id: str | None = None,
        version: int = 1,
        updated_at: datetime | None = None,
    ) -> CompiledMemoryEntry:
        entry = CompiledMemoryEntry(
            entryId=entry_id or generate_memory_entry_id(),
            entryType=entry_type,
            key=key,
            value=dict(value),
            version=version,
            rawEvidenceIds=_normalize_ids(raw_evidence_ids),
            confidenceScore=confidence_score,
            updatedAt=updated_at or datetime.now(UTC),
        )
        return await self.upsert_entry(entry)

    async def upsert_entry(self, entry: CompiledMemoryEntry) -> CompiledMemoryEntry:
        normalized_ids = _normalize_ids(entry.raw_evidence_ids)
        if not normalized_ids:
            raise ValueError(
                "compiled memory upserts require at least one raw evidence id "
                "for provenance"
            )

        await self._ensure_provenance(normalized_ids)

        prepared_entry = entry.model_copy(update={"raw_evidence_ids": normalized_ids})
        saved_entry = await self._p.repo.save_compiled_entry(prepared_entry)
        self._logger.debug(
            "compiled_memory_upserted",
            entry_id=saved_entry.entry_id,
            entry_type=saved_entry.entry_type,
            key=saved_entry.key,
            version=saved_entry.version,
            raw_evidence_ids=saved_entry.raw_evidence_ids,
        )
        return saved_entry

    async def get(self, entry_id: str) -> CompiledMemoryEntry | None:
        return await self._p.repo.load_compiled_entry(entry_id)

    async def list(
        self,
        *,
        entry_type: MemoryEntryType | str | None = None,
        key: str | None = None,
        limit: int = 500,
    ) -> list[CompiledMemoryEntry]:
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        entry_type_value = (
            entry_type.value if isinstance(entry_type, MemoryEntryType) else entry_type
        )
        return await self._p.repo.load_compiled_entries(
            entry_type=entry_type_value,
            key=key,
            limit=limit,
        )

    async def _ensure_provenance(self, raw_evidence_ids: list[str]) -> None:
        missing_ids: list[str] = []
        for evidence_id in raw_evidence_ids:
            evidence = await self._p.repo.load_raw_evidence(evidence_id)
            if evidence is None:
                missing_ids.append(evidence_id)

        if missing_ids:
            missing_display = ", ".join(missing_ids)
            raise ValueError(
                "compiled memory provenance check failed; unknown raw evidence ids: "
                f"{missing_display}"
            )


def _normalize_ids(raw_evidence_ids: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for evidence_id in raw_evidence_ids:
        if not isinstance(evidence_id, str):
            continue
        value = evidence_id.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized

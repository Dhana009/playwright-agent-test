from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from agent.core.ids import generate_evidence_id
from agent.core.logging import get_logger
from agent.memory.models import RawEvidence, RawEvidenceType
from agent.storage.files import RunLayout, get_run_layout
from agent.storage.repos._common import dumps_json
from agent.storage.repos.memory import MemoryRepository


@dataclass
class RawEvidencePersistence:
    run_id: str
    repo: MemoryRepository
    layout: RunLayout

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must be non-empty")


class RawEvidenceWriter:
    """
    Append-only writer for the Raw Evidence memory layer.
    """

    def __init__(
        self,
        persistence: RawEvidencePersistence,
        *,
        also_write_jsonl: bool = True,
    ) -> None:
        self._logger = get_logger(__name__)
        self._p = persistence
        self._also_write_jsonl = also_write_jsonl

    @classmethod
    def for_run(
        cls,
        *,
        run_id: str,
        sqlite_path: str | Path | None = None,
        runs_root: str | Path | None = None,
        also_write_jsonl: bool = True,
    ) -> "RawEvidenceWriter":
        persistence = RawEvidencePersistence(
            run_id=run_id,
            repo=MemoryRepository(sqlite_path=sqlite_path),
            layout=get_run_layout(run_id, runs_root=runs_root),
        )
        return cls(persistence, also_write_jsonl=also_write_jsonl)

    @property
    def run_id(self) -> str:
        return self._p.run_id

    @property
    def jsonl_path(self) -> Path:
        return self._p.layout.run_dir / "raw_evidence.jsonl"

    async def append(
        self,
        *,
        actor: str,
        evidence_type: RawEvidenceType | str,
        artifact_ref: str,
        step_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        evidence_id: str | None = None,
        captured_at: datetime | None = None,
    ) -> RawEvidence:
        evidence = RawEvidence(
            evidenceId=evidence_id or generate_evidence_id(),
            runId=self._p.run_id,
            stepId=step_id,
            actor=actor,
            evidenceType=evidence_type,
            artifactRef=artifact_ref,
            capturedAt=captured_at or datetime.now(UTC),
            metadata=dict(metadata or {}),
        )
        return await self.append_record(evidence)

    async def append_record(self, evidence: RawEvidence) -> RawEvidence:
        if evidence.run_id != self._p.run_id:
            raise ValueError(
                "RawEvidence run_id must match writer run_id "
                f"({self._p.run_id}); received {evidence.run_id}"
            )

        try:
            await self._p.repo.save_raw_evidence(evidence)
        except aiosqlite.IntegrityError as exc:
            message = str(exc)
            if "raw_evidence.evidence_id" in message:
                raise ValueError(
                    f"Raw evidence id '{evidence.evidence_id}' already exists; "
                    "raw evidence is append-only."
                ) from exc
            raise

        if self._also_write_jsonl:
            await self._append_jsonl(evidence)

        self._logger.debug(
            "raw_evidence_appended",
            run_id=self._p.run_id,
            evidence_id=evidence.evidence_id,
            step_id=evidence.step_id,
            evidence_type=evidence.evidence_type,
            artifact_ref=evidence.artifact_ref,
            path=str(self.jsonl_path) if self._also_write_jsonl else None,
        )
        return evidence

    async def get(self, evidence_id: str) -> RawEvidence | None:
        return await self._p.repo.load_raw_evidence(evidence_id)

    async def list(
        self,
        *,
        step_id: str | None = None,
        limit: int = 500,
    ) -> list[RawEvidence]:
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        return await self._p.repo.load_raw_evidence_for_run(
            run_id=self._p.run_id,
            step_id=step_id,
            limit=limit,
        )

    async def _append_jsonl(self, evidence: RawEvidence) -> None:
        payload = evidence.model_dump(mode="json", by_alias=True)
        line = dumps_json(payload) + "\n"
        path = self.jsonl_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("", encoding="utf-8")
        with path.open("a", encoding="utf-8") as file_obj:
            file_obj.write(line)

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agent.cache.models import CacheDecision, ContextFingerprint
from agent.core.logging import get_logger
from agent.stepgraph.models import LocatorBundle, StepGraph
from agent.storage.files import get_run_layout
from agent.storage.repos._common import loads_json, open_connection
from agent.storage.repos.cache import CacheRepository
from agent.storage.repos.step_graph import StepGraphRepository


class ExportRunProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mode: str
    status: str
    started_at: str = Field(alias="startedAt")
    ended_at: str | None = Field(default=None, alias="endedAt")
    metadata: dict[str, object] = Field(default_factory=dict)


class ExportProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    generated_by: str = Field(alias="generatedBy")
    generated_at: str = Field(alias="generatedAt")
    source: str
    run: ExportRunProvenance | None = None


class ManifestLocatorBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    step_id: str = Field(alias="stepId")
    action: str
    bundle: LocatorBundle


class ManifestFingerprintEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, use_enum_values=True)

    step_id: str = Field(alias="stepId")
    fingerprint: ContextFingerprint
    decision: CacheDecision
    decision_reasons: list[str] = Field(default_factory=list, alias="decisionReasons")
    created_at: str = Field(alias="createdAt")


class PortableManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    manifest_version: str = Field(default="1.0", alias="manifestVersion")
    run_id: str = Field(alias="runId")
    generated_at: str = Field(alias="generatedAt")
    step_count: int = Field(alias="stepCount")
    step_graph_version: str = Field(alias="stepGraphVersion")
    step_graph: StepGraph = Field(alias="stepGraph")
    locator_bundles: list[ManifestLocatorBundle] = Field(default_factory=list, alias="locatorBundles")
    fingerprints: list[ManifestFingerprintEntry] = Field(default_factory=list)
    provenance: ExportProvenance


class ManifestWriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    run_id: str = Field(alias="runId")
    manifest_path: str = Field(alias="manifestPath")
    step_count: int = Field(alias="stepCount")
    locator_bundle_count: int = Field(alias="locatorBundleCount")
    fingerprint_count: int = Field(alias="fingerprintCount")


@dataclass
class PortableManifestPersistence:
    step_graph_repo: StepGraphRepository
    cache_repo: CacheRepository
    sqlite_path: str | Path | None = None


class PortableManifestWriter:
    def __init__(
        self,
        persistence: PortableManifestPersistence,
        *,
        runs_root: str | Path | None = None,
    ) -> None:
        self._logger = get_logger(__name__)
        self._p = persistence
        self._runs_root = runs_root

    @classmethod
    def create(
        cls,
        *,
        sqlite_path: str | Path | None = None,
        runs_root: str | Path | None = None,
    ) -> "PortableManifestWriter":
        return cls(
            persistence=PortableManifestPersistence(
                step_graph_repo=StepGraphRepository(sqlite_path=sqlite_path),
                cache_repo=CacheRepository(sqlite_path=sqlite_path),
                sqlite_path=sqlite_path,
            ),
            runs_root=runs_root,
        )

    async def build_manifest(self, *, run_id: str) -> PortableManifest:
        step_graph = await self._p.step_graph_repo.load(run_id)
        if step_graph is None:
            raise ValueError(f"Step graph not found for run_id={run_id}")

        cache_records = await self._p.cache_repo.load_for_run(run_id=run_id, limit=10_000)
        latest_by_step: dict[str, ManifestFingerprintEntry] = {}
        for record in cache_records:
            if record.step_id in latest_by_step:
                continue
            latest_by_step[record.step_id] = ManifestFingerprintEntry(
                stepId=record.step_id,
                fingerprint=record.fingerprint.model_dump(mode="python", by_alias=True),
                decision=record.decision,
                decisionReasons=list(record.decision_reasons),
                createdAt=record.created_at.isoformat(),
            )

        locator_bundles = [
            ManifestLocatorBundle(
                stepId=step.step_id,
                action=step.action,
                bundle=step.target.model_dump(mode="python", by_alias=True),
            )
            for step in step_graph.steps
            if step.target is not None
        ]
        fingerprints = [
            latest_by_step[step.step_id]
            for step in step_graph.steps
            if step.step_id in latest_by_step
        ]

        run_provenance = await self._load_run_provenance(run_id=run_id)
        now = datetime.now(UTC).isoformat()
        return PortableManifest(
            runId=run_id,
            generatedAt=now,
            stepCount=len(step_graph.steps),
            stepGraphVersion=step_graph.version,
            stepGraph=step_graph.model_dump(mode="python", by_alias=True),
            locatorBundles=[entry.model_dump(mode="python", by_alias=True) for entry in locator_bundles],
            fingerprints=[entry.model_dump(mode="python", by_alias=True) for entry in fingerprints],
            provenance=ExportProvenance(
                generatedBy="agent.export.manifest.PortableManifestWriter",
                generatedAt=now,
                source="sqlite:step_graph+cache_records+runs",
                run=(
                    run_provenance.model_dump(mode="python", by_alias=True)
                    if run_provenance is not None
                    else None
                ),
            ).model_dump(mode="python", by_alias=True),
        )

    async def write_manifest(
        self,
        *,
        run_id: str,
        output_path: str | Path | None = None,
    ) -> ManifestWriteResult:
        manifest = await self.build_manifest(run_id=run_id)
        if output_path is None:
            destination = get_run_layout(run_id, self._runs_root).manifest_json
        else:
            destination = Path(output_path)
            destination.parent.mkdir(parents=True, exist_ok=True)

        destination.write_text(
            manifest.model_dump_json(indent=2, by_alias=True),
            encoding="utf-8",
        )
        self._logger.info(
            "portable_manifest_written",
            run_id=run_id,
            manifest_path=str(destination),
            step_count=manifest.step_count,
            locator_bundle_count=len(manifest.locator_bundles),
            fingerprint_count=len(manifest.fingerprints),
        )
        return ManifestWriteResult(
            runId=run_id,
            manifestPath=str(destination),
            stepCount=manifest.step_count,
            locatorBundleCount=len(manifest.locator_bundles),
            fingerprintCount=len(manifest.fingerprints),
        )

    async def _load_run_provenance(self, *, run_id: str) -> ExportRunProvenance | None:
        async with open_connection(self._p.sqlite_path) as connection:
            cursor = await connection.execute(
                """
                SELECT mode, status, started_at, ended_at, metadata_json
                FROM runs
                WHERE run_id = ?;
                """,
                (run_id,),
            )
            row = await cursor.fetchone()

        if row is None:
            return None

        metadata = loads_json(row["metadata_json"], {})
        if not isinstance(metadata, dict):
            metadata = {}
        return ExportRunProvenance(
            mode=row["mode"],
            status=row["status"],
            startedAt=row["started_at"],
            endedAt=row["ended_at"],
            metadata=metadata,
        )

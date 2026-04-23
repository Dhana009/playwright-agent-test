from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agent.core.logging import get_logger
from agent.export._ported.codegen import build_playwright_test_source
from agent.stepgraph.models import StepGraph
from agent.storage.files import get_run_layout
from agent.storage.repos.step_graph import StepGraphRepository


class PlaywrightSpecWriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    run_id: str = Field(alias="runId")
    spec_path: str = Field(alias="specPath")
    step_count: int = Field(alias="stepCount")


@dataclass
class PlaywrightSpecWriterPersistence:
    step_graph_repo: StepGraphRepository


class PlaywrightSpecWriter:
    def __init__(
        self,
        persistence: PlaywrightSpecWriterPersistence,
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
    ) -> "PlaywrightSpecWriter":
        return cls(
            persistence=PlaywrightSpecWriterPersistence(
                step_graph_repo=StepGraphRepository(sqlite_path=sqlite_path)
            ),
            runs_root=runs_root,
        )

    async def build_source_from_run(
        self,
        *,
        run_id: str,
        test_name: str = "recorded user flow",
        target_url: str | None = None,
    ) -> str:
        step_graph = await self._p.step_graph_repo.load(run_id=run_id)
        if step_graph is None:
            raise ValueError(f"Step graph not found for run_id={run_id}")
        return self.build_source(
            step_graph=step_graph,
            test_name=test_name,
            target_url=target_url,
        )

    def build_source(
        self,
        *,
        step_graph: StepGraph,
        test_name: str = "recorded user flow",
        target_url: str | None = None,
    ) -> str:
        return build_playwright_test_source(
            step_graph=step_graph,
            test_name=test_name,
            target_url=target_url,
        )

    async def write_spec(
        self,
        *,
        run_id: str,
        output_path: str | Path | None = None,
        test_name: str = "recorded user flow",
        target_url: str | None = None,
    ) -> PlaywrightSpecWriteResult:
        step_graph = await self._p.step_graph_repo.load(run_id=run_id)
        if step_graph is None:
            raise ValueError(f"Step graph not found for run_id={run_id}")

        source = self.build_source(
            step_graph=step_graph,
            test_name=test_name,
            target_url=target_url,
        )
        if output_path is None:
            destination = get_run_layout(run_id, self._runs_root).run_dir / f"{run_id}.spec.ts"
        else:
            destination = Path(output_path)
            destination.parent.mkdir(parents=True, exist_ok=True)

        if destination.suffixes[-2:] != [".spec", ".ts"]:
            destination = destination.with_suffix(".spec.ts")

        destination.write_text(source, encoding="utf-8")
        self._logger.info(
            "playwright_spec_written",
            run_id=run_id,
            spec_path=str(destination),
            step_count=len(step_graph.steps),
        )
        return PlaywrightSpecWriteResult(
            runId=run_id,
            specPath=str(destination),
            stepCount=len(step_graph.steps),
        )

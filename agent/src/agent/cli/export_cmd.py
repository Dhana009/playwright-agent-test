from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from agent.core.config import Settings
from agent.export.gating import ExportGateThresholds, evaluate_export_confidence
from agent.export.manifest import PortableManifestWriter
from agent.export.spec_writer import PlaywrightSpecWriter

app = typer.Typer(help="Export commands for manifest/spec artifacts.")


@app.callback()
def _root() -> None:
    """
    Export run artifacts.
    """


@app.command("export")
def export(
    run_id: str = typer.Argument(..., help="Run id to export."),
    sqlite_path: Path | None = typer.Option(
        None,
        "--sqlite-path",
        exists=False,
        dir_okay=False,
        file_okay=True,
        help="Optional sqlite path override.",
    ),
    runs_root: Path | None = typer.Option(
        None,
        "--runs-root",
        exists=False,
        file_okay=False,
        dir_okay=True,
        help="Optional runs root override for artifact output.",
    ),
    manifest_path: Path | None = typer.Option(
        None,
        "--manifest-path",
        exists=False,
        dir_okay=False,
        file_okay=True,
        help="Optional destination for manifest.json.",
    ),
    write_spec: bool = typer.Option(
        False,
        "--write-spec",
        help="Also generate a Playwright .spec.ts file.",
    ),
    spec_path: Path | None = typer.Option(
        None,
        "--spec-path",
        exists=False,
        dir_okay=False,
        file_okay=True,
        help="Optional destination for generated .spec.ts output.",
    ),
    test_name: str = typer.Option(
        "recorded user flow",
        "--test-name",
        help="Test name used in generated Playwright spec.",
    ),
    target_url: str | None = typer.Option(
        None,
        "--target-url",
        help="Optional URL passed to generated Playwright spec.",
    ),
    review_threshold: float = typer.Option(
        0.70,
        "--review-threshold",
        min=0.0,
        max=1.0,
        help="Confidence threshold below which export is blocked.",
    ),
    allow_threshold: float = typer.Option(
        0.85,
        "--allow-threshold",
        min=0.0,
        max=1.0,
        help="Confidence threshold at or above which export is allowed.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Allow manifest/spec export even when confidence gate blocks.",
    ),
) -> None:
    """
    Export portable manifest and optional Playwright spec for a completed run.
    """

    async def _impl() -> None:
        settings = Settings.load()
        resolved_sqlite_path = str(sqlite_path) if sqlite_path is not None else settings.storage.sqlite_path
        resolved_runs_root = str(runs_root) if runs_root is not None else None

        manifest_writer = PortableManifestWriter.create(
            sqlite_path=resolved_sqlite_path,
            runs_root=resolved_runs_root,
        )
        manifest = await manifest_writer.build_manifest(run_id=run_id)
        thresholds = ExportGateThresholds(
            reviewThreshold=review_threshold,
            allowThreshold=allow_threshold,
        )
        gate = evaluate_export_confidence(manifest.step_graph, thresholds=thresholds)
        blocked = gate.decision.value == "block"
        if blocked and not force:
            typer.echo("Export blocked by confidence gate.")
            for reason in gate.reasons:
                typer.echo(f"- {reason.code.value}: {reason.message}")
            typer.echo("Use --force to bypass the block.")
            raise typer.Exit(code=2)

        manifest_result = await manifest_writer.write_manifest(
            run_id=run_id,
            output_path=str(manifest_path) if manifest_path is not None else None,
        )
        typer.echo(f"manifest: {manifest_result.manifest_path}")
        typer.echo(f"gate_decision: {gate.decision.value}")

        if write_spec:
            spec_writer = PlaywrightSpecWriter.create(
                sqlite_path=resolved_sqlite_path,
                runs_root=resolved_runs_root,
            )
            spec_result = await spec_writer.write_spec(
                run_id=run_id,
                output_path=str(spec_path) if spec_path is not None else None,
                test_name=test_name,
                target_url=target_url,
            )
            typer.echo(f"spec: {spec_result.spec_path}")

    asyncio.run(_impl())


def main() -> None:
    app()


if __name__ == "__main__":
    main()

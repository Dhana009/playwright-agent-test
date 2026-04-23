from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
PLAYWRIGHT_CLI = WORKSPACE_ROOT / "playwright-cli"
FIXTURE_GRAPH = PROJECT_ROOT / "scripts" / "fixtures" / "graphs" / "fixture_login_and_navigate.json"

sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(SCRIPT_DIR))

from agent.core.ids import generate_run_id  # noqa: E402
from agent.export.gating import (  # noqa: E402
    ExportDecision,
    ExportGateReasonCode,
    ExportGateThresholds,
    evaluate_export_confidence,
)
from agent.export.manifest import (  # noqa: E402
    PortableManifest,
    PortableManifestWriter,
    sanitize_stepgraph_for_manifest,
)
from agent.export.spec_writer import PlaywrightSpecWriter  # noqa: E402
from agent.stepgraph.models import LocatorBundle, Step, StepGraph, StepMode, TimeoutPolicy  # noqa: E402
from agent.storage.repos.step_graph import StepGraphRepository  # noqa: E402
from fixtures import running_server  # noqa: E402
from _runner import SmokeRunner  # noqa: E402


def _graph_with_confidence(score: float) -> StepGraph:
    run_id = generate_run_id()
    step = Step(
        mode=StepMode.ACTION,
        action="click",
        metadata={"tabId": "tab_x"},
        target=LocatorBundle(
            primarySelector="button#x",
            fallbackSelectors=[],
            confidenceScore=score,
        ),
        timeout_policy=TimeoutPolicy(timeoutMs=5000),
    )
    return StepGraph(runId=run_id, steps=[step], edges=[], version="1.0")


def _rewrite_fixture_urls(graph: StepGraph, base_url: str) -> StepGraph:
    """Replace fixed fixture port URLs with the live fixture server base URL."""
    steps: list[Step] = []
    for step in graph.steps:
        meta = dict(step.metadata)
        for key in ("url", "frameUrl"):
            val = meta.get(key)
            if isinstance(val, str):
                meta[key] = re.sub(
                    r"https?://127\.0\.0\.1:\d+",
                    base_url.rstrip("/"),
                    val,
                )
        steps.append(step.model_copy(update={"metadata": meta}))
    return graph.model_copy(update={"steps": steps})


def _ensure_playwright_cli_deps(smoke: SmokeRunner) -> Path:
    marker = PLAYWRIGHT_CLI / "node_modules" / "@playwright" / "test"
    if not marker.is_dir():
        proc = subprocess.run(
            ["npm", "ci"],
            cwd=str(PLAYWRIGHT_CLI),
            capture_output=True,
            text=True,
            check=False,
        )
        smoke.check(proc.returncode == 0, f"npm ci failed in playwright-cli: {proc.stderr or proc.stdout}")
        smoke.check(marker.is_dir(), "npm ci did not install @playwright/test")
    return PLAYWRIGHT_CLI


def _run_playwright_test_once(cli_root: Path, spec_rel: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["npx", "playwright", "test", spec_rel, "--reporter=list"],
        cwd=str(cli_root),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CI": "1"},
    )


async def main() -> int:
    smoke = SmokeRunner(phase="A11", default_task="A11.1")
    thresholds = ExportGateThresholds(reviewThreshold=0.70, allowThreshold=0.85)

    with smoke.case("a11_1_confidence_gating", task="A11.1", feature="export_gating"):
        g06 = evaluate_export_confidence(_graph_with_confidence(0.6), thresholds=thresholds)
        smoke.check(g06.decision == ExportDecision.BLOCK, f"expected block, got {g06.decision!r}")
        smoke.check(bool(g06.reasons), "block must include machine-readable reasons")
        smoke.check(
            any(r.code == ExportGateReasonCode.LOW_CONFIDENCE_BLOCK for r in g06.reasons),
            f"expected low_confidence_block reason, got {g06.reasons!r}",
        )

        g075 = evaluate_export_confidence(_graph_with_confidence(0.75), thresholds=thresholds)
        smoke.check(g075.decision == ExportDecision.REVIEW, f"expected review, got {g075.decision!r}")

        g09 = evaluate_export_confidence(_graph_with_confidence(0.9), thresholds=thresholds)
        smoke.check(g09.decision == ExportDecision.ALLOW, f"expected allow, got {g09.decision!r}")

    with smoke.case("a11_2_manifest_schema_and_redaction", task="A11.2", feature="export_manifest"):
        with tempfile.TemporaryDirectory(prefix="a11-manifest-") as tmp:
            db = Path(tmp) / "store.sqlite"
            run_id = generate_run_id()
            raw = json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))
            raw["runId"] = run_id
            graph = StepGraph.model_validate(raw)
            repo = StepGraphRepository(sqlite_path=db)
            await repo.save(graph)

            writer = PortableManifestWriter.create(sqlite_path=db, runs_root=Path(tmp) / "runs")
            result = await writer.write_manifest(run_id=run_id)
            manifest_path = Path(result.manifest_path)
            text = manifest_path.read_text(encoding="utf-8")
            smoke.check("fixture-password" not in text, "password fill value must not appear in manifest JSON")
            smoke.check("[REDACTED]" in text, "manifest should contain redacted password placeholder")

            parsed = json.loads(text)
            PortableManifest.model_validate(parsed)

            sanitized_only = sanitize_stepgraph_for_manifest(graph)
            pw_step = next(
                s
                for s in sanitized_only.steps
                if s.target is not None and "password" in s.target.primary_selector.lower()
            )
            smoke.check(pw_step.metadata.get("text") == "[REDACTED]", pw_step.metadata)

    with smoke.case("a11_3_playwright_spec_runs", task="A11.3", feature="export_spec"):
        if os.environ.get("SKIP_PLAYWRIGHT_A11", "").strip().lower() in {"1", "true", "yes"}:
            print("SKIP_PLAYWRIGHT_A11 set — skipping npx playwright test")
        else:
            cli_root = _ensure_playwright_cli_deps(smoke)
            with tempfile.TemporaryDirectory(prefix="a11-spec-") as tmpdir:
                tmp = Path(tmpdir).resolve()
                db = tmp / "s.sqlite"
                run_id = generate_run_id()
                with running_server(port=0) as server:
                    raw = json.loads(FIXTURE_GRAPH.read_text(encoding="utf-8"))
                    raw["runId"] = run_id
                    graph = StepGraph.model_validate(raw)
                    graph = _rewrite_fixture_urls(graph, server.base_url)
                    await StepGraphRepository(sqlite_path=db).save(graph)

                    spec_path = tmp / f"{run_id}.spec.ts"
                    await PlaywrightSpecWriter.create(sqlite_path=db, runs_root=tmp / "runs").write_spec(
                        run_id=run_id,
                        output_path=spec_path,
                        test_name="A11 fixture login export",
                        target_url=f"{server.base_url}/login.html",
                    )

                    tests_dir = cli_root / "tests"
                    tests_dir.mkdir(parents=True, exist_ok=True)
                    borrowed = tests_dir / "_a11_export_flow.spec.ts"
                    shutil.copyfile(spec_path, borrowed)
                    try:
                        proc = _run_playwright_test_once(cli_root, "tests/_a11_export_flow.spec.ts")
                        out = f"{proc.stderr or ''}\n{proc.stdout or ''}"
                        if proc.returncode != 0 and (
                            "Executable doesn't exist" in out or "npx playwright install" in out
                        ):
                            inst = subprocess.run(
                                ["npx", "playwright", "install", "chromium"],
                                cwd=str(cli_root),
                                capture_output=True,
                                text=True,
                                check=False,
                            )
                            smoke.check(
                                inst.returncode == 0,
                                f"playwright install chromium failed:\n{inst.stderr}\n{inst.stdout}",
                            )
                            proc = _run_playwright_test_once(cli_root, "tests/_a11_export_flow.spec.ts")
                        smoke.check(
                            proc.returncode == 0,
                            f"playwright test failed:\n{proc.stdout}\n{proc.stderr}",
                        )
                    finally:
                        borrowed.unlink(missing_ok=True)

    return smoke.finalize()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

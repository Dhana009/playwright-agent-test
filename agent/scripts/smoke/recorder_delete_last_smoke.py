"""Deterministic check for StepGraphRecorder.delete_last_step (no browser)."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agent.recorder.recorder import StepGraphRecorder  # noqa: E402
from agent.stepgraph.models import LocatorBundle, Step, StepEdge, StepMode  # noqa: E402


def main() -> None:
    recorder = StepGraphRecorder(url="about:blank", headless=True, browser_ui=False)
    bundle = LocatorBundle(primarySelector="button#x", confidenceScore=0.9)
    s0 = Step(mode=StepMode.ACTION, action="click", target=bundle)
    s1 = Step(mode=StepMode.ACTION, action="click", target=bundle)
    g = recorder.step_graph
    g.steps = [s0, s1]
    g.edges = [
        StepEdge(
            fromStepId=s0.step_id,
            toStepId=s1.step_id,
            condition="on_success",
        ),
    ]

    assert recorder.delete_last_step()
    assert len(g.steps) == 1
    assert g.steps[0].step_id == s0.step_id
    assert len(g.edges) == 0

    assert recorder.delete_last_step()
    assert len(g.steps) == 0
    assert len(g.edges) == 0

    assert not recorder.delete_last_step()

    print("recorder_delete_last_smoke: ok")


if __name__ == "__main__":
    main()

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(SCRIPT_DIR))

from _runner import SmokeRunner  # noqa: E402


def main() -> int:
    runner = SmokeRunner(phase="T8", default_task="T8.1")
    with runner.case("phase_8_scaffold", task="T8.1", feature="llm_layer"):
        print("Scaffold ready: implement provider, escalation, and mode-switch checks.")
    return runner.finalize()


if __name__ == "__main__":
    raise SystemExit(main())

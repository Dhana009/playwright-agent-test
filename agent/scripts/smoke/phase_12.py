from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(SCRIPT_DIR))

from _runner import SmokeRunner  # noqa: E402


def main() -> int:
    runner = SmokeRunner(phase="T12", default_task="T12.1")
    with runner.case("phase_12_scaffold", task="T12.1", feature="e2e_acceptance"):
        print("Scaffold ready: implement live acceptance flow checks.")
    return runner.finalize()


if __name__ == "__main__":
    raise SystemExit(main())

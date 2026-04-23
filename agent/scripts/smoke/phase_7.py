from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(SCRIPT_DIR))

from _runner import SmokeRunner  # noqa: E402


def main() -> int:
    runner = SmokeRunner(phase="T7", default_task="T7.1")
    with runner.case("phase_7_scaffold", task="T7.1", feature="memory_layer"):
        print("Scaffold ready: implement raw/compiled memory behavior checks.")
    return runner.finalize()


if __name__ == "__main__":
    raise SystemExit(main())

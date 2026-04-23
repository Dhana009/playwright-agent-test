from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agent.core.ids import generate_run_id  # noqa: E402
from agent.core.logging import configure_logging, get_logger  # noqa: E402


def main() -> None:
    run_id = generate_run_id()
    log_path = configure_logging(run_id=run_id)
    logger = get_logger(__name__)
    logger.info("phase_1_smoke_log_written", smoke_phase=1)

    print(f"run_id={run_id}")
    print(f"log_path={log_path}")


if __name__ == "__main__":
    main()

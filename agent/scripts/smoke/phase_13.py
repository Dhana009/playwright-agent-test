"""Backward-compatible entry point: A13 integration smoke lives in `phase_a13.py`."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from phase_a13 import main as a13_main  # noqa: E402


async def main() -> int:
    return await a13_main()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

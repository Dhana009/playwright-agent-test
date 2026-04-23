"""
Phase B0 — human verification session helper.

Prefer the CLI from the agent package root:

  uv run agent human start
  uv run agent human checkpoint -s <session_id> --id b1_c1 -q "…"

This file is the plan-specified entry point; it forwards to the same CLI.
"""

from __future__ import annotations

import sys
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_AGENT_ROOT / "src"))

from agent.cli.human_session_cmd import app  # noqa: E402

if __name__ == "__main__":
    app()

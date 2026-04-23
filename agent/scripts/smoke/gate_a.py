"""
Gate A — Phase A completion check (functional plan).

Runs Task A0–A13 smoke scripts in order, then asserts no `runtime` / `logical`
bugs with outcome `open` were written under `artifacts/test-runs/` during this
invocation (by bugs.jsonl mtime).

Usage (from `agent/`):

  uv run python scripts/smoke/gate_a.py

Quick mode (skip optional browser-heavy npx / bench matrix):

  uv run python scripts/smoke/gate_a.py --quick
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
ARTIFACTS_TEST_RUNS = PROJECT_ROOT / "artifacts" / "test-runs"

# Task A0–A13 entry points (see plan/functional_then_human_test_plan_be149dc5.plan.md).
_PHASE_A_SCRIPTS: tuple[str, ...] = (
    "phase_0.py",
    "phase_1.py",
    "phase_2.py",
    "phase_3.py",
    "phase_a4.py",
    "phase_a5.py",
    "phase_a6.py",
    "phase_a7.py",
    "phase_a8.py",
    "phase_a9.py",
    "phase_a10.py",
    "phase_a11.py",
    "phase_a12.py",
    "phase_a13.py",
)


def _assert_no_open_critical_bugs_since(*, since: float, artifacts_root: Path) -> None:
    bad: list[str] = []
    if not artifacts_root.is_dir():
        return
    for bugs_path in artifacts_root.glob("*/bugs.jsonl"):
        try:
            if bugs_path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        for line in bugs_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                bad.append(f"{bugs_path}: invalid json line")
                continue
            if entry.get("outcome") != "open":
                continue
            err_cls = entry.get("error_class")
            if err_cls in ("runtime", "logical"):
                bad.append(
                    f"{bugs_path}: open {err_cls} bug: {entry.get('summary', '')[:120]}",
                )
    if bad:
        msg = "Gate A bug log check failed:\n" + "\n".join(bad)
        raise SystemExit(msg)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Phase A (A0–A13) smokes and Gate A checks.")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Set SKIP_PLAYWRIGHT_A11, SKIP_PLAYWRIGHT_A13, SKIP_BENCH_A12 for a faster run.",
    )
    args = parser.parse_args()

    os.chdir(PROJECT_ROOT)
    started = time.time()
    env = {**os.environ}
    if args.quick:
        env["SKIP_PLAYWRIGHT_A11"] = "1"
        env["SKIP_PLAYWRIGHT_A13"] = "1"
        env["SKIP_BENCH_A12"] = "1"
        print("Gate A: quick mode (skipping optional npx playwright / bench browser matrix)")

    failed: list[str] = []
    for name in _PHASE_A_SCRIPTS:
        script = SCRIPT_DIR / name
        if not script.is_file():
            failed.append(f"missing script {name}")
            break
        print(f"\n=== Gate A: {name} ===", flush=True)
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(PROJECT_ROOT),
            env=env,
            check=False,
        )
        if proc.returncode != 0:
            failed.append(name)
            break

    if failed:
        print(f"\nGate A FAILED at: {failed[0]}", file=sys.stderr)
        return 1

    _assert_no_open_critical_bugs_since(since=started, artifacts_root=ARTIFACTS_TEST_RUNS)
    print("\nGate A: all Phase A smokes passed; no new open runtime/logical bugs in bug log.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

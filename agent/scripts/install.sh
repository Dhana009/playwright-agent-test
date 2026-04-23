#!/usr/bin/env bash
set -euo pipefail

uv sync
uv run playwright install chromium

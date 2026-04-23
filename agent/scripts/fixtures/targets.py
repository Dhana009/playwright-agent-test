from __future__ import annotations

from typing import Literal

from .live import TargetInfo, live_target

_OFFLINE_TARGET = TargetInfo(
    url="http://127.0.0.1:8787/login.html",
    email="fixture@example.test",
    password="fixture-password",
)


def offline_target() -> TargetInfo:
    return _OFFLINE_TARGET


def choose_target(kind: Literal["live", "offline"]) -> TargetInfo:
    if kind == "live":
        return live_target()
    if kind == "offline":
        return offline_target()
    raise ValueError(f"Unsupported target kind: {kind!r}")

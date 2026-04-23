from __future__ import annotations

from pathlib import Path
from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)  # type: ignore[name-defined]

_SRC_PACKAGE = Path(__file__).resolve().parent / "src" / "agent"
if _SRC_PACKAGE.exists():
    __path__.append(str(_SRC_PACKAGE))

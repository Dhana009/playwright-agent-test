from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunLayout:
    run_id: str
    runs_root: Path

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must be a non-empty string")
        if Path(self.run_id).name != self.run_id:
            raise ValueError("run_id must not contain path separators")

        normalized_root = self.runs_root
        if not normalized_root.is_absolute():
            normalized_root = (_project_root() / normalized_root).resolve()
        object.__setattr__(self, "runs_root", normalized_root)

    @property
    def run_dir(self) -> Path:
        return _ensure_dir(self.runs_root / self.run_id)

    @property
    def log_jsonl(self) -> Path:
        return self.run_dir / "log.jsonl"

    @property
    def events_jsonl(self) -> Path:
        return self.run_dir / "events.jsonl"

    @property
    def snapshots_dir(self) -> Path:
        return _ensure_dir(self.run_dir / "snapshots")

    @property
    def traces_dir(self) -> Path:
        return _ensure_dir(self.run_dir / "traces")

    @property
    def screenshots_dir(self) -> Path:
        return _ensure_dir(self.run_dir / "screenshots")

    @property
    def storage_state_json(self) -> Path:
        return self.run_dir / "storage_state.json"

    @property
    def manifest_json(self) -> Path:
        return self.run_dir / "manifest.json"


def get_run_layout(run_id: str, runs_root: str | Path | None = None) -> RunLayout:
    return RunLayout(run_id=run_id, runs_root=resolve_runs_root(runs_root))


def resolve_runs_root(runs_root: str | Path | None = None) -> Path:
    if runs_root is None:
        return _project_root() / "runs"

    candidate = Path(runs_root)
    if candidate.is_absolute():
        return candidate
    return _project_root() / candidate


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]

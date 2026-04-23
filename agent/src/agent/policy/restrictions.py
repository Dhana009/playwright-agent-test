from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field


class RestrictionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    allowed: bool
    reason_code: str = Field(alias="reasonCode")
    summary: str
    decision_path: list[str] = Field(default_factory=list, alias="decisionPath")
    normalized_value: str | None = Field(default=None, alias="normalizedValue")


class RestrictionViolation(RuntimeError):
    def __init__(self, decision: RestrictionDecision) -> None:
        super().__init__(decision.summary)
        self.decision = decision


class RestrictionsPolicy:
    def __init__(
        self,
        *,
        domain_allowlist: Sequence[str] | None = None,
        domain_denylist: Sequence[str] | None = None,
        upload_root_allowlist: Sequence[str] | None = None,
        allow_file_urls: bool = False,
    ) -> None:
        self._domain_allowlist = _normalize_domain_patterns(domain_allowlist or [])
        self._domain_denylist = _normalize_domain_patterns(domain_denylist or [])
        self._upload_roots = _normalize_roots(upload_root_allowlist or [])
        self._allow_file_urls = allow_file_urls

    @classmethod
    def from_settings(cls, policy_settings: object) -> "RestrictionsPolicy":
        return cls(
            domain_allowlist=getattr(policy_settings, "domain_allowlist", []),
            domain_denylist=getattr(policy_settings, "domain_denylist", []),
            upload_root_allowlist=getattr(policy_settings, "upload_root_allowlist", []),
            allow_file_urls=bool(getattr(policy_settings, "allow_file_urls", False)),
        )

    def enforce_navigation_url(self, url: str) -> RestrictionDecision:
        decision = self.evaluate_navigation_url(url)
        if not decision.allowed:
            raise RestrictionViolation(decision)
        return decision

    def evaluate_navigation_url(self, url: str) -> RestrictionDecision:
        parsed = urlparse(url)
        decision_path = ["restrictions_v1", "kind=navigation_url", f"url={url}"]

        if parsed.scheme.lower() == "file" and not self._allow_file_urls:
            decision_path.append("blocked=file_scheme")
            return RestrictionDecision(
                allowed=False,
                reasonCode="file_scheme_blocked",
                summary="Navigation to file:// URLs is blocked by policy.",
                decisionPath=decision_path,
                normalizedValue=url,
            )

        host = (parsed.hostname or "").lower()
        if host:
            if _matches_any_domain(host, self._domain_denylist):
                decision_path.append("blocked=domain_denylist")
                return RestrictionDecision(
                    allowed=False,
                    reasonCode="domain_denied",
                    summary=f"Navigation to domain '{host}' is denied by policy.",
                    decisionPath=decision_path,
                    normalizedValue=host,
                )
            if self._domain_allowlist and not _matches_any_domain(host, self._domain_allowlist):
                decision_path.append("blocked=domain_not_allowlisted")
                return RestrictionDecision(
                    allowed=False,
                    reasonCode="domain_not_allowlisted",
                    summary=f"Navigation to domain '{host}' is outside the allowlist.",
                    decisionPath=decision_path,
                    normalizedValue=host,
                )

        decision_path.append("allowed")
        return RestrictionDecision(
            allowed=True,
            reasonCode="allowed",
            summary="Navigation URL allowed by policy.",
            decisionPath=decision_path,
            normalizedValue=host or url,
        )

    def enforce_upload_paths(self, file_paths: str | Sequence[str]) -> list[str]:
        normalized_paths = self.normalize_upload_paths(file_paths)
        if not normalized_paths:
            decision = RestrictionDecision(
                allowed=False,
                reasonCode="empty_upload_set",
                summary="Upload blocked because no file paths were provided.",
                decisionPath=["restrictions_v1", "kind=upload_paths", "blocked=empty"],
            )
            raise RestrictionViolation(decision)
        return normalized_paths

    def normalize_upload_paths(self, file_paths: str | Sequence[str]) -> list[str]:
        paths = [file_paths] if isinstance(file_paths, str) else list(file_paths)
        if not paths:
            return []

        normalized: list[str] = []
        for raw_path in paths:
            resolved = _resolve_path(str(raw_path))
            if not self._upload_roots:
                # Empty allowlist: do not block (local dashboard / interactive replay). When
                # ``upload_root_allowlist`` is set, paths must stay under those roots.
                normalized.append(str(resolved))
                continue
            if not _is_within_any_root(resolved, self._upload_roots):
                decision = RestrictionDecision(
                    allowed=False,
                    reasonCode="upload_path_outside_allowlist",
                    summary=f"Upload path '{resolved}' is outside allowed roots.",
                    decisionPath=[
                        "restrictions_v1",
                        "kind=upload_paths",
                        "blocked=path_outside_allowlist",
                    ],
                    normalizedValue=str(resolved),
                )
                raise RestrictionViolation(decision)
            normalized.append(str(resolved))
        return normalized


def _normalize_domain_patterns(values: Sequence[str]) -> list[str]:
    patterns: list[str] = []
    for value in values:
        pattern = value.strip().lower()
        if not pattern:
            continue
        if "://" in pattern:
            parsed = urlparse(pattern)
            pattern = (parsed.hostname or "").lower()
        patterns.append(pattern)
    return sorted(set(patterns))


def _matches_any_domain(host: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        if pattern.startswith("*."):
            suffix = pattern[2:]
            if host == suffix or host.endswith(f".{suffix}"):
                return True
        elif host == pattern:
            return True
    return False


def _normalize_roots(values: Sequence[str]) -> list[Path]:
    roots: list[Path] = []
    for value in values:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = (Path.cwd() / candidate).resolve()
        else:
            candidate = candidate.resolve()
        roots.append(candidate)
    return sorted(set(roots), key=lambda path: str(path))


def _resolve_path(raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        return (Path.cwd() / candidate).resolve()
    return candidate.resolve()


def _is_within_any_root(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        try:
            if path.is_relative_to(root):
                return True
        except ValueError:
            continue
    return False

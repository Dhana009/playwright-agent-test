# Ported from playwright-repo-test/lib/locator/candidates.js — adapted for agent/
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PortedCandidate:
    priority: float
    label: str
    selector: str
    strategy: str


def _append_playwright_scoped_chain_candidates(
    out: list[PortedCandidate],
    *,
    tag: str,
    testid: str | None,
    element_id: str | None,
    role: str | None,
    aria_label: str | None,
    text: str | None,
    parents: Any,
) -> None:
    """Playwright-style narrowing: ancestor >> descendant (same idea as locator().filter() / chaining).

    Flat ``[data-testid="x"]`` often matches duplicates; scoping under a parent testid or id
    prefers a unique match without relying on ``nth=0``.
    """
    if not isinstance(parents, list):
        return

    name_hint = (aria_label or text or "")[:80].strip()

    for depth, raw_parent in enumerate(parents[:5]):
        if not isinstance(raw_parent, dict):
            continue
        parent_testid = _as_str(raw_parent.get("testid"))
        parent_id = _as_str(raw_parent.get("id"))
        base = 2.2 + depth * 0.015

        if testid and parent_testid:
            out.append(
                PortedCandidate(
                    base,
                    f"pw-chain:ancestor{depth}-testid>>testid",
                    f'[data-testid={_quote(parent_testid)}] >> [data-testid={_quote(testid)}]',
                    "scoped_chain",
                )
            )
        if testid and parent_id:
            out.append(
                PortedCandidate(
                    base + 0.002,
                    f"pw-chain:ancestor{depth}-id>>testid",
                    f'#{_escape_css_identifier(parent_id)} >> [data-testid={_quote(testid)}]',
                    "scoped_chain",
                )
            )
        if element_id and parent_testid:
            out.append(
                PortedCandidate(
                    base + 0.004,
                    f"pw-chain:ancestor{depth}-testid>>id",
                    f'[data-testid={_quote(parent_testid)}] >> #{_escape_css_identifier(element_id)}',
                    "scoped_chain",
                )
            )
        if role and name_hint and parent_testid:
            out.append(
                PortedCandidate(
                    base + 0.006,
                    f"pw-chain:ancestor{depth}-testid>>role",
                    f'[data-testid={_quote(parent_testid)}] >> [role={_quote(role)}]:has-text({_quote(name_hint)})',
                    "scoped_chain",
                )
            )
        if name_hint and parent_testid and _is_stable_text(name_hint):
            out.append(
                PortedCandidate(
                    base + 0.008,
                    f"pw-chain:ancestor{depth}-testid>>text",
                    f'[data-testid={_quote(parent_testid)}] >> {tag}:has-text({_quote(name_hint)})',
                    "scoped_chain",
                )
            )

    if testid and len(parents) >= 2:
        p0 = parents[0] if isinstance(parents[0], dict) else None
        p1 = parents[1] if isinstance(parents[1], dict) else None
        tid0 = _as_str(p0.get("testid")) if p0 else None
        tid1 = _as_str(p1.get("testid")) if p1 else None
        if tid0 and tid1:
            out.append(
                PortedCandidate(
                    2.18,
                    "pw-chain:testid3hop",
                    f'[data-testid={_quote(tid1)}] >> [data-testid={_quote(tid0)}] >> [data-testid={_quote(testid)}]',
                    "scoped_chain",
                )
            )


def build_candidates(target: dict[str, Any]) -> list[PortedCandidate]:
    out: list[PortedCandidate] = []

    tag = str(target.get("tag") or "div").lower()
    testid = _as_str(target.get("testid"))
    aria_label = _as_str(target.get("ariaLabel"))
    text = _as_str(target.get("text"))
    placeholder = _as_str(target.get("placeholder"))
    element_id = _as_str(target.get("id"))
    name = _as_str(target.get("name"))
    class_name = _as_str(target.get("className"))
    absolute_xpath = _as_str(target.get("absoluteXPath"))
    sibling_index = int(target.get("siblingIndex", -1))
    parents = target.get("parents") or []
    data_attrs = target.get("dataAttrs") or {}

    if testid:
        out.append(
            PortedCandidate(
                priority=2.0,
                label="getByTestId",
                selector=f"[data-testid={_quote(testid)}]",
                strategy="testid",
            )
        )
        out.append(
            PortedCandidate(
                priority=2.1,
                label="css:testid",
                selector=f"[data-test-id={_quote(testid)}]",
                strategy="testid",
            )
        )
        out.append(
            PortedCandidate(
                priority=2.2,
                label="css:data-qa",
                selector=f"[data-qa={_quote(testid)}]",
                strategy="testid",
            )
        )

    if aria_label:
        out.append(
            PortedCandidate(
                priority=1.0,
                label="aria-label",
                selector=f"[aria-label={_quote(aria_label)}]",
                strategy="aria_label",
            )
        )
        out.append(
            PortedCandidate(
                priority=1.1,
                label="label:text",
                selector=f"label:has-text({_quote(aria_label)})",
                strategy="label",
            )
        )

    role_map = {
        "button": "button",
        "a": "link",
        "input": "textbox",
        "textarea": "textbox",
        "select": "combobox",
        "h1": "heading",
        "h2": "heading",
        "h3": "heading",
        "h4": "heading",
        "h5": "heading",
        "h6": "heading",
    }
    role = _as_str(target.get("role")) or role_map.get(tag)
    name_hint = (aria_label or text or "")[:80].strip()
    if role and name_hint:
        out.append(
            PortedCandidate(
                priority=1.2,
                label="role+name",
                selector=f'[role={_quote(role)}]:has-text({_quote(name_hint)})',
                strategy="role_name",
            )
        )

    if placeholder:
        out.append(
            PortedCandidate(
                priority=1.4,
                label="placeholder",
                selector=f"[placeholder={_quote(placeholder)}]",
                strategy="placeholder",
            )
        )

    if text and _is_stable_text(text):
        out.append(
            PortedCandidate(
                priority=1.6,
                label="stable-text",
                selector=f"{tag}:has-text({_quote(text[:80])})",
                strategy="stable_text",
            )
        )

    if element_id:
        out.append(
            PortedCandidate(
                priority=3.0,
                label="id",
                selector=f"#{_escape_css_identifier(element_id)}",
                strategy="scoped_css",
            )
        )

    if name:
        out.append(
            PortedCandidate(
                priority=3.2,
                label="name",
                selector=f'{tag}[name={_quote(name)}]',
                strategy="scoped_css",
            )
        )

    for attr_name, attr_value in data_attrs.items():
        if attr_name in {"data-testid", "data-test-id", "data-qa"}:
            continue
        out.append(
            PortedCandidate(
                priority=3.5,
                label=f"data-attr:{attr_name}",
                selector=f"[{attr_name}={_quote(str(attr_value))}]",
                strategy="scoped_css",
            )
        )

    if class_name:
        skip = {
            "active",
            "selected",
            "disabled",
            "hover",
            "focus",
            "open",
            "show",
            "visible",
            "hidden",
        }
        classes = [
            token
            for token in class_name.split()
            if len(token) > 2 and token not in skip and not token.startswith("__rec")
        ]
        for cls in classes[:3]:
            out.append(
                PortedCandidate(
                    priority=3.8,
                    label=f"class:{cls}",
                    selector=f".{_escape_css_identifier(cls)}",
                    strategy="scoped_css",
                )
            )

    _append_playwright_scoped_chain_candidates(
        out,
        tag=tag,
        testid=testid,
        element_id=element_id,
        role=role,
        aria_label=aria_label,
        text=text,
        parents=parents,
    )

    parent = parents[0] if parents else None
    if isinstance(parent, dict):
        parent_id = _as_str(parent.get("id"))
        if parent_id:
            out.append(
                PortedCandidate(
                    priority=4.0,
                    label="parent-id-child",
                    selector=f"#{_escape_css_identifier(parent_id)} {tag}",
                    strategy="xpath_nth_fallback",
                )
            )
        parent_testid = _as_str(parent.get("testid"))
        if parent_testid:
            out.append(
                PortedCandidate(
                    priority=4.1,
                    label="parent-testid-child",
                    selector=f'[data-testid={_quote(parent_testid)}] {tag}',
                    strategy="xpath_nth_fallback",
                )
            )
        if sibling_index >= 0:
            out.append(
                PortedCandidate(
                    priority=4.3,
                    label="nth-child",
                    selector=f"{tag}:nth-child({sibling_index + 1})",
                    strategy="xpath_nth_fallback",
                )
            )

    if absolute_xpath:
        out.append(
            PortedCandidate(
                priority=4.8,
                label="absolute-xpath",
                selector=f"xpath={absolute_xpath}",
                strategy="xpath_nth_fallback",
            )
        )

    return _unique_candidates(out)


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _escape_css_identifier(value: str) -> str:
    return value.replace(".", "\\.").replace(":", "\\:")


def _is_stable_text(value: str) -> bool:
    if len(value) < 2 or len(value) > 80:
        return False
    if value.isdigit():
        return False
    return True


def _unique_candidates(candidates: list[PortedCandidate]) -> list[PortedCandidate]:
    seen: set[str] = set()
    unique: list[PortedCandidate] = []
    for candidate in sorted(candidates, key=lambda item: item.priority):
        if candidate.selector in seen:
            continue
        seen.add(candidate.selector)
        unique.append(candidate)
    return unique

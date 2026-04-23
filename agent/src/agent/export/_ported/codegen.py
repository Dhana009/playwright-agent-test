# Ported from playwright-repo-test/lib/codegen.js — adapted for agent/
from __future__ import annotations

import json
from typing import Any

from agent.stepgraph.models import Step, StepGraph

_LOCATOR_ACTIONS = {
    "assert_checked",
    "assert_count",
    "assert_enabled",
    "assert_hidden",
    "assert_in_viewport",
    "assert_text",
    "assert_value",
    "assert_visible",
    "check",
    "click",
    "drag",
    "fill",
    "focus",
    "hover",
    "select",
    "type",
    "uncheck",
    "upload",
}


def build_playwright_test_source(
    step_graph: StepGraph,
    *,
    test_name: str = "recorded user flow",
    target_url: str | None = None,
) -> str:
    resolved_url = target_url or _infer_target_url(step_graph) or "about:blank"
    locator_map = _build_locator_map(step_graph)
    locator_map_json = json.dumps(locator_map, indent=2, ensure_ascii=True)

    lines = [
        "// Auto-generated Playwright test.",
        f"// Run ID: {step_graph.run_id}",
        "import { test, expect, Page } from '@playwright/test';",
        "",
        f"const LOCATOR_MAP: Record<string, string[]> = {locator_map_json};",
        "",
        "async function locatorFor(page: Page, stepId: string) {",
        "  const candidates = LOCATOR_MAP[stepId] ?? [];",
        "  if (candidates.length === 0) {",
        "    throw new Error(`No locator candidates configured for ${stepId}`);",
        "  }",
        "  for (const selector of candidates) {",
        "    const candidate = page.locator(selector);",
        "    if ((await candidate.count()) > 0) {",
        "      return candidate.first();",
        "    }",
        "  }",
        "  return page.locator(candidates[0]).first();",
        "}",
        "",
        f"test({_ts_string(test_name)}, async ({'{'} page {'}'}) => {{",
    ]
    if resolved_url != "about:blank":
        lines.append(f"  await page.goto({_ts_string(resolved_url)}, {{ waitUntil: 'domcontentloaded' }});")
    else:
        lines.append("  // No explicit start URL was detected; provide one before running.")
    lines.append("")

    for index, step in enumerate(step_graph.steps, start=1):
        lines.extend(_render_step(step=step, step_number=index))

    lines.append("});")
    return "\n".join(lines)


def _render_step(*, step: Step, step_number: int) -> list[str]:
    lines = [f"  // step {step_number}: {step.action} ({step.step_id})"]
    action = step.action.strip().lower()
    metadata = step.metadata if isinstance(step.metadata, dict) else {}
    timeout_ms = max(step.timeout_policy.timeout_ms, 0)

    if action == "navigate":
        url = _pick_string(metadata, "url", "targetUrl", "navigateUrl", "sourceUrl")
        if url:
            lines.append(f"  await page.goto({_ts_string(url)}, {{ waitUntil: 'domcontentloaded' }});")
        else:
            lines.append("  // TODO: navigate step missing URL metadata.")
        lines.append("")
        return lines

    if action == "navigate_back":
        lines.append("  await page.goBack();")
        lines.append("")
        return lines

    if action == "wait_timeout":
        wait_ms = _pick_int(metadata, "timeoutMs", "waitMs", "ms") or timeout_ms
        lines.append(f"  await page.waitForTimeout({wait_ms});")
        lines.append("")
        return lines

    if action == "wait_for":
        url_pattern = _pick_string(metadata, "urlPattern", "url")
        text = _pick_string(metadata, "text", "expected")
        selector = _pick_string(metadata, "selector")
        if url_pattern:
            lines.append(
                f"  await page.waitForURL({_ts_string(url_pattern)}, {{ timeout: {timeout_ms or 30_000} }});"
            )
        elif text:
            lines.append(
                f"  await expect(page.locator('body')).toContainText("
                f"{_ts_string(text)}, {{ timeout: {timeout_ms or 30_000} }});"
            )
        elif selector:
            lines.append(
                f"  await page.locator({_ts_string(selector)}).first().waitFor("
                f"{{ state: 'visible', timeout: {timeout_ms or 30_000} }});"
            )
        else:
            lines.append(f"  await page.waitForTimeout({timeout_ms or 1_000});")
        lines.append("")
        return lines

    if action == "dialog_handle":
        accept = _pick_bool(metadata, "accept", "dialogAccept", default=True)
        prompt_text = _pick_string(metadata, "promptText", "prompt")
        if accept:
            if prompt_text is None:
                lines.append("  page.once('dialog', dialog => dialog.accept());")
            else:
                lines.append(
                    f"  page.once('dialog', dialog => dialog.accept({_ts_string(prompt_text)}));"
                )
        else:
            lines.append("  page.once('dialog', dialog => dialog.dismiss());")
        lines.append("")
        return lines

    if action in {"assert_url", "assert_title"}:
        expected = _pick_string(metadata, "urlPattern", "url", "titlePattern", "title", "expected")
        if expected is None:
            lines.append("  // TODO: assertion step missing expected value metadata.")
        elif action == "assert_url":
            lines.append(
                f"  await expect(page).toHaveURL({_ts_string(expected)}, {{ timeout: {timeout_ms or 30_000} }});"
            )
        else:
            lines.append(
                f"  await expect(page).toHaveTitle({_ts_string(expected)}, {{ timeout: {timeout_ms or 30_000} }});"
            )
        lines.append("")
        return lines

    locator_var = ""
    if action in _LOCATOR_ACTIONS:
        if step.target is None:
            lines.append("  // TODO: locator-based step has no target bundle.")
            lines.append("")
            return lines
        locator_var = f"loc{step_number}"
        lines.append(f"  const {locator_var} = await locatorFor(page, {_ts_string(step.step_id)});")

    lines.extend(
        _render_locator_action(
            action=action,
            metadata=metadata,
            timeout_ms=timeout_ms,
            locator_var=locator_var,
        )
    )
    lines.append("")
    return lines


def _render_locator_action(
    *,
    action: str,
    metadata: dict[str, Any],
    timeout_ms: int,
    locator_var: str,
) -> list[str]:
    timeout = timeout_ms or 30_000
    if action == "click":
        return [f"  await {locator_var}.click({{ timeout: {timeout} }});"]
    if action == "fill":
        value = _pick_string(metadata, "text", "value", "fillVal") or ""
        return [f"  await {locator_var}.fill({_ts_string(value)}, {{ timeout: {timeout} }});"]
    if action == "type":
        value = _pick_string(metadata, "text", "typeText", "value") or ""
        return [
            f"  await {locator_var}.pressSequentially({_ts_string(value)}, {{ delay: 50, timeout: {timeout} }});"
        ]
    if action == "press":
        key = _pick_string(metadata, "key", "value") or "Enter"
        return [f"  await page.keyboard.press({_ts_string(key)});"]
    if action == "check":
        return [f"  await {locator_var}.check({{ timeout: {timeout} }});"]
    if action == "uncheck":
        return [f"  await {locator_var}.uncheck({{ timeout: {timeout} }});"]
    if action == "select":
        value = _pick_string(metadata, "value", "selectValue", "option") or ""
        return [f"  await {locator_var}.selectOption({_ts_string(value)}, {{ timeout: {timeout} }});"]
    if action == "upload":
        path = _pick_string(metadata, "filePath", "path", "value") or ""
        return [f"  await {locator_var}.setInputFiles({_ts_string(path)});"]
    if action == "drag":
        source = _pick_string(metadata, "sourceSelector", "sourcePrimarySelector")
        if source is None:
            return ["  // TODO: drag step missing source selector metadata."]
        return [
            f"  await page.locator({_ts_string(source)}).first().dragTo({locator_var}, {{ timeout: {timeout} }});"
        ]
    if action == "hover":
        return [f"  await {locator_var}.hover({{ timeout: {timeout} }});"]
    if action == "focus":
        return [f"  await {locator_var}.focus({{ timeout: {timeout} }});"]
    if action == "assert_visible":
        return [f"  await expect({locator_var}).toBeVisible({{ timeout: {timeout} }});"]
    if action == "assert_text":
        expected = _pick_string(metadata, "expected", "text", "value") or ""
        contains = _pick_bool(metadata, "contains", default=True)
        matcher = "toContainText" if contains else "toHaveText"
        return [f"  await expect({locator_var}).{matcher}({_ts_string(expected)}, {{ timeout: {timeout} }});"]
    if action == "assert_value":
        expected = _pick_string(metadata, "expected", "expectedValue", "value") or ""
        return [f"  await expect({locator_var}).toHaveValue({_ts_string(expected)}, {{ timeout: {timeout} }});"]
    if action == "assert_checked":
        expected_checked = _pick_bool(metadata, "expected", "expectedChecked", default=True)
        if expected_checked:
            return [f"  await expect({locator_var}).toBeChecked({{ timeout: {timeout} }});"]
        return [f"  await expect({locator_var}).not.toBeChecked({{ timeout: {timeout} }});"]
    if action == "assert_enabled":
        expected_enabled = _pick_bool(metadata, "expected", "expectedEnabled", default=True)
        if expected_enabled:
            return [f"  await expect({locator_var}).toBeEnabled({{ timeout: {timeout} }});"]
        return [f"  await expect({locator_var}).toBeDisabled({{ timeout: {timeout} }});"]
    if action == "assert_hidden":
        return [f"  await expect({locator_var}).toBeHidden({{ timeout: {timeout} }});"]
    if action == "assert_count":
        count = _pick_int(metadata, "expectedCount", "count")
        return [f"  await expect({locator_var}).toHaveCount({count or 0}, {{ timeout: {timeout} }});"]
    if action == "assert_in_viewport":
        return [f"  await expect({locator_var}).toBeInViewport({{ timeout: {timeout} }});"]
    return [f"  // TODO: unsupported step action {_ts_string(action)}."]


def _infer_target_url(step_graph: StepGraph) -> str | None:
    for step in step_graph.steps:
        if step.action.strip().lower() != "navigate":
            continue
        metadata = step.metadata if isinstance(step.metadata, dict) else {}
        url = _pick_string(metadata, "url", "targetUrl", "navigateUrl", "sourceUrl")
        if url:
            return url
    return None


def _build_locator_map(step_graph: StepGraph) -> dict[str, list[str]]:
    locator_map: dict[str, list[str]] = {}
    for step in step_graph.steps:
        if step.target is None:
            continue
        selectors = [step.target.primary_selector] + list(step.target.fallback_selectors)
        deduped: list[str] = []
        seen: set[str] = set()
        for selector in selectors:
            value = selector.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            deduped.append(value)
        if deduped:
            locator_map[step.step_id] = deduped
    return locator_map


def _pick_string(metadata: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _pick_int(metadata: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _pick_bool(metadata: dict[str, Any], *keys: str, default: bool) -> bool:
    for key in keys:
        if key not in metadata:
            continue
        value = metadata[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes"}:
                return True
            if lowered in {"false", "0", "no"}:
                return False
    return default


def _ts_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f"'{escaped}'"

from __future__ import annotations

import asyncio
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(SCRIPT_DIR))

from agent.execution.browser import BrowserSession  # noqa: E402
from agent.locator.engine import LocatorEngine  # noqa: E402
from fixtures import running_server  # noqa: E402
from _runner import SmokeRunner  # noqa: E402


def _http_get(url: str) -> None:
    with urllib.request.urlopen(url, timeout=5.0) as response:  # noqa: S310
        response.read()


async def _resolve_tab_id(session: BrowserSession, page) -> str:
    for _ in range(30):
        tab_id = session.get_tab_id(page)
        if tab_id is not None:
            return tab_id
        await asyncio.sleep(0.01)
    raise RuntimeError("Timed out waiting for BrowserSession tab registration")


async def main() -> int:
    runner = SmokeRunner(phase="A4", default_task="A4.1")
    engine = LocatorEngine()

    with runner.case("a4_1_five_targets_strategy_priority", task="A4.1", feature="locator_engine"):
        with running_server() as fixture:
            session = BrowserSession(headless=True)
            try:
                await session.start()
                _, context = await session.new_context()
                page = await context.new_page()
                await _resolve_tab_id(session, page)
                await page.goto(f"{fixture.base_url}/dashboard.html", wait_until="domcontentloaded")
                await page.evaluate(
                    """
                    () => {
                      const host = document.createElement("section");
                      host.id = "a4-locator-host";
                      host.innerHTML = `
                        <input
                          id="a4-full-mix"
                          data-testid="a4-email"
                          aria-label="Work email"
                          placeholder="you@corp.test"
                          name="workEmail"
                          class="field primary"
                          type="email"
                          value=""
                        />
                        <button type="button" id="aria-only-btn" aria-label="Save draft">Ignore</button>
                        <input id="ph-only" placeholder="Search orders" type="text" />
                        <span id="stable-span">Fixture stable label</span>
                      `;
                      document.body.appendChild(host);
                    }
                    """
                )

                full_mix = {
                    "tag": "input",
                    "id": "a4-full-mix",
                    "testid": "a4-email",
                    "text": "Work email",
                    "placeholder": "you@corp.test",
                    "ariaLabel": "Work email",
                    "role": "textbox",
                    "name": "workEmail",
                    "dataAttrs": {"data-extra": "x"},
                    "siblingIndex": 0,
                    "parents": [{"id": "a4-locator-host"}],
                    "absoluteXPath": "/html/body/section/input[1]",
                }
                candidates = engine.build_candidates(full_mix)
                runner.check(bool(candidates), "Expected locator candidates for full-mix descriptor")
                first_strategy_index: dict[str, int] = {}
                for index, candidate in enumerate(candidates):
                    if candidate.strategy not in first_strategy_index:
                        first_strategy_index[candidate.strategy] = index

                expected_families = [
                    "testid",
                    "aria_label",
                    "label",
                    "role_name",
                    "placeholder",
                    "stable_text",
                    "scoped_css",
                    "xpath_nth_fallback",
                ]
                missing = [s for s in expected_families if s not in first_strategy_index]
                runner.check(
                    not missing,
                    f"Expected strategy families in candidates, missing: {missing}",
                )
                ordered_indexes = [first_strategy_index[s] for s in expected_families]
                runner.check(
                    ordered_indexes == sorted(ordered_indexes),
                    f"Expected strategy priority order {expected_families}, indexes {ordered_indexes}",
                )

                five_checks: list[tuple[str, dict, str]] = [
                    ("full_mix_primary", full_mix, "testid"),
                    (
                        "testid_button",
                        {
                            "tag": "button",
                            "testid": "primary-action",
                            "text": "Open Approval Queue",
                            "role": "button",
                        },
                        "testid",
                    ),
                    (
                        "aria_only",
                        {
                            "tag": "button",
                            "id": "aria-only-btn",
                            "ariaLabel": "Save draft",
                            "role": "button",
                            "text": "Ignore",
                        },
                        "aria_label",
                    ),
                    (
                        "placeholder_input",
                        {"tag": "input", "id": "ph-only", "placeholder": "Search orders"},
                        "placeholder",
                    ),
                    (
                        "stable_text_span",
                        {"tag": "span", "id": "stable-span", "text": "Fixture stable label"},
                        "stable_text",
                    ),
                ]

                for _label, descriptor, expected_strategy in five_checks:
                    ranked = await engine.rank_candidates(page, descriptor)
                    runner.check(bool(ranked), f"Expected ranked candidates for {_label}")
                    runner.check(
                        ranked[0].strategy == expected_strategy,
                        f"{_label}: expected primary strategy {expected_strategy!r}, "
                        f"got {ranked[0].strategy!r}",
                    )
            finally:
                await session.stop()

    with runner.case("a4_2_confidence_monotonic_in_ranked_list", task="A4.2", feature="locator_engine"):
        with running_server() as fixture:
            session = BrowserSession(headless=True)
            try:
                await session.start()
                _, context = await session.new_context()
                page = await context.new_page()
                await _resolve_tab_id(session, page)
                await page.goto(f"{fixture.base_url}/dashboard.html", wait_until="domcontentloaded")
                await page.evaluate(
                    """
                    () => {
                      const host = document.createElement("section");
                      host.id = "a4-locator-host";
                      host.innerHTML = `
                        <input
                          id="a4-full-mix"
                          data-testid="a4-email"
                          aria-label="Work email"
                          placeholder="you@corp.test"
                          name="workEmail"
                          class="field primary"
                          type="email"
                          value=""
                        />
                      `;
                      document.body.appendChild(host);
                    }
                    """
                )
                full_mix = {
                    "tag": "input",
                    "id": "a4-full-mix",
                    "testid": "a4-email",
                    "text": "Work email",
                    "placeholder": "you@corp.test",
                    "ariaLabel": "Work email",
                    "role": "textbox",
                    "name": "workEmail",
                    "dataAttrs": {"data-extra": "x"},
                    "siblingIndex": 0,
                    "parents": [{"id": "a4-locator-host"}],
                    "absoluteXPath": "/html/body/section/input[1]",
                }
                ranked = await engine.rank_candidates(page, full_mix)
                runner.check(len(ranked) >= 2, "Expected multiple ranked candidates for monotonicity check")
                for index in range(len(ranked) - 1):
                    runner.check(
                        ranked[index].confidence_score + 1e-9 >= ranked[index + 1].confidence_score,
                        "Expected confidence to be non-increasing down the ranked list "
                        f"at index {index}: {ranked[index].confidence_score} vs "
                        f"{ranked[index + 1].confidence_score}",
                    )

                bundle = await engine.build(page, full_mix)
                runner.check(
                    bundle.confidence_score == ranked[0].confidence_score,
                    "Expected LocatorBundle confidence to match top ranked candidate",
                )
                for fallback_selector in bundle.fallback_selectors:
                    matching = [item for item in ranked if item.selector == fallback_selector]
                    runner.check(
                        len(matching) == 1,
                        f"Expected exactly one ranked row for fallback {fallback_selector!r}",
                    )
                    fb = matching[0]
                    runner.check(
                        ranked[0].confidence_score + 1e-9 >= fb.confidence_score,
                        "Expected primary confidence >= each bundled fallback confidence",
                    )
            finally:
                await session.stop()

    with runner.case("a4_3_region_mutation_lowers_confidence", task="A4.3", feature="locator_engine"):
        with running_server() as fixture:
            session = BrowserSession(headless=True)
            try:
                await session.start()
                _, context = await session.new_context()
                page = await context.new_page()
                await _resolve_tab_id(session, page)
                await page.goto(f"{fixture.base_url}/dashboard.html", wait_until="domcontentloaded")

                order_line_descriptor = {
                    "tag": "li",
                    "text": "Invoice A - queued",
                    "parents": [{"id": "orders-list"}],
                }
                bundle_before = await engine.build(page, order_line_descriptor)
                runner.check(
                    bundle_before.confidence_score > 0.5,
                    "Expected high-confidence match before region mutation",
                )

                _http_get(f"{fixture.base_url}/mutate/region")
                await page.wait_for_timeout(400)

                ranked_after = await engine.rank_candidates(page, order_line_descriptor)
                if not ranked_after:
                    ranked_after = await engine.rank_candidates(
                        page, order_line_descriptor, force=True
                    )
                runner.check(
                    ranked_after,
                    "Expected ranked candidates after mutation (forced if multi-match)",
                )
                confidence_after = ranked_after[0].confidence_score
                runner.check(
                    confidence_after < bundle_before.confidence_score,
                    "Expected confidence to drop after /mutate/region invalidated stable text "
                    f"(before={bundle_before.confidence_score}, after={confidence_after})",
                )
            finally:
                await session.stop()

    return runner.finalize()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

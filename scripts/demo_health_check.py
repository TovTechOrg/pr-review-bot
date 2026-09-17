"""Weekly liveness check for the deployed demo.

Deliberately scoped to the dashboard payoff screen -- the one screen whose
breakage makes the article's link worthless -- rather than all four wizard
steps. A cron coupled to every step's markup breaks constantly and gets
ignored, which is worse than no check (2026-09-16 design, "Testing and CI").

Lives in scripts/ and NOT in tests/ on purpose: pyproject.toml's
`testpaths = ["tests", "dashboard/tests"]` means pytest never collects this,
so `uv run pytest -v` stays offline and CLAUDE.md's run-the-suite-before-push
rule keeps working.

No credentials: the demo dashboard's username and password are `demo`/`demo`,
baked into Dockerfile.demo and already public in this repository. There is
nothing here to put in a GitHub secret, and adding one would create a real
credential where the design deliberately has none.

Exit codes mirror scripts/check_consumer_contract.py:
  0  the demo works
  1  the demo is broken (the thing this check exists to find)
  2  the check itself could not run
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

BOT_URL = "https://demo-pr-review-bot.onrender.com"
LAUNCHER_URL = "https://tovtechorg.github.io/pr-review-bot/demo/"

# The canned security finding from demo/content.py's FINDINGS_BY_SCHEMA.
# dashboard.html's findingRows() renders finding.description into a
# `.finding` div, HTML-escaped; this substring contains nothing that escaping
# would alter.
EXPECTED_FINDING = "The API key is written to the log in plaintext"

# A measured cold start was 41.6s (2026-09-17). Generous ceiling: a weekly
# check that cries wolf on a slow boot is a check people learn to ignore.
WAKE_TIMEOUT_S = 180
# The dashboard fires /api/demo/bootstrap on load, which enqueues a ticket
# the dispatcher then has to claim and run.
REVIEW_TIMEOUT_MS = 90_000


def _report(summary_path: str | None, lines: list[str]) -> None:
    text = "\n".join(lines) + "\n"
    print(text, end="")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(text)


def wake(url: str) -> float:
    """Block until the service answers, returning seconds waited.

    Render holds the connection through a cold start rather than returning
    502 (measured), so this is one long request, not a poll loop.
    """
    started = time.monotonic()
    response = httpx.get(f"{url}/healthz", timeout=WAKE_TIMEOUT_S, follow_redirects=True)
    response.raise_for_status()
    return time.monotonic() - started


def check_dashboard() -> list[str]:
    """Drive the payoff screen. Returns failure reasons; empty means healthy."""
    problems: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        try:
            page.goto(BOT_URL, wait_until="domcontentloaded", timeout=WAKE_TIMEOUT_S * 1000)

            # demo/static/demo.js pre-fills these and marks them readOnly.
            # Playwright's fill() refuses a readonly input, so assert the
            # prefill (itself a real check -- a reader who has to type
            # credentials into a demo has already lost) and click submit.
            if page.locator("#passwordInput").count():
                page.wait_for_function(
                    "document.getElementById('usernameInput')"
                    " && document.getElementById('usernameInput').value === 'demo'",
                    timeout=15_000,
                )
                page.locator("#loginForm button[type=submit]").click()

            page.wait_for_selector(".review-card", timeout=REVIEW_TIMEOUT_MS)
            # Cards render collapsed: expandedPrs is empty on a first load, so
            # `.review-findings` is display:none until the row is clicked.
            page.locator(".review-row").first.click()
            page.wait_for_selector(".review-card.expanded .review-findings", timeout=15_000)

            findings = page.locator(".review-card.expanded .review-findings").first.inner_text()
            if EXPECTED_FINDING not in findings:
                problems.append(
                    f"the canned finding text is missing from the expanded review card; "
                    f"got {findings[:200]!r}"
                )
        except PlaywrightTimeoutError as exc:
            problems.append(f"timed out driving the dashboard: {exc}")
        finally:
            browser.close()
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", help="path to write a Markdown report to")
    args = parser.parse_args()

    lines = ["## Demo health check", ""]

    try:
        launcher = httpx.get(LAUNCHER_URL, timeout=30, follow_redirects=True)
        launcher.raise_for_status()
        lines.append(f"- Launcher page: OK ({launcher.status_code})")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"- Launcher page: **UNREACHABLE** ({type(exc).__name__})")
        _report(args.summary, lines + ["", "Verdict: **CANNOT CHECK**"])
        return 2

    try:
        waited = wake(BOT_URL)
        lines.append(f"- Demo bot woke in {waited:.1f}s")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"- Demo bot: **DID NOT WAKE** ({type(exc).__name__})")
        _report(args.summary, lines + ["", "Verdict: **BROKEN**"])
        return 1

    problems = check_dashboard()
    if problems:
        lines.append("- Dashboard: **BROKEN**")
        lines.extend(f"  - {problem}" for problem in problems)
        _report(args.summary, lines + ["", "Verdict: **BROKEN**"])
        return 1

    lines.append("- Dashboard: review rendered with the expected finding")
    _report(args.summary, lines + ["", "Verdict: **HEALTHY**"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

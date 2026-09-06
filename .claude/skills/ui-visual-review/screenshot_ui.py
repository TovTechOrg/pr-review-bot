"""Screenshots a URL at light-desktop, dark-desktop, and mobile viewports.

Standalone helper for the `ui-visual-review` skill -- catches CSS/layout
regressions (column blowouts, squeezed inputs, RTL mirroring, dark-mode
contrast) that are invisible from reading HTML/CSS source alone. Requires
nothing beyond this project's existing `playwright` dependency (already
used by its own browser tests) -- run via `uv run --no-project python
screenshot_ui.py <url> <out_dir> [--cookie name=value ...]`.

Never pass a credential as a `--cookie` value on the command line if it can
be avoided -- prefer a throwaway/test-only session. This script does not
log or print cookie values.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

_DESKTOP = {"width": 1280, "height": 800}
_MOBILE = {"width": 390, "height": 844}  # iPhone 13 mini-ish width


def _parse_cookie(raw: str) -> tuple[str, str]:
    name, _, value = raw.partition("=")
    if not name or not value:
        raise argparse.ArgumentTypeError(f"expected name=value, got {raw!r}")
    return name, value


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("out_dir")
    parser.add_argument(
        "--cookie",
        action="append",
        default=[],
        type=_parse_cookie,
        metavar="name=value",
        help="session cookie to inject before navigating (repeatable)",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    from urllib.parse import urlparse

    domain = urlparse(args.url).hostname or "localhost"

    shots = [
        ("light-desktop", _DESKTOP, "light"),
        ("dark-desktop", _DESKTOP, "dark"),
        ("mobile", _MOBILE, "light"),
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for name, viewport, color_scheme in shots:
                context = browser.new_context(
                    viewport=viewport, color_scheme=color_scheme
                )
                if args.cookie:
                    context.add_cookies(
                        [
                            {
                                "name": cookie_name,
                                "value": cookie_value,
                                "domain": domain,
                                "path": "/",
                            }
                            for cookie_name, cookie_value in args.cookie
                        ]
                    )
                page = context.new_page()
                page.goto(args.url, wait_until="networkidle")
                dest = out_dir / f"{name}.png"
                page.screenshot(path=str(dest), full_page=True)
                print(f"wrote {dest}")
                context.close()
        finally:
            browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

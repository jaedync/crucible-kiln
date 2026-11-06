"""Screenshot the dashboard at desktop and phone sizes with Playwright (Chromium).

    python3 tools/shoot_dash.py --url http://127.0.0.1:7070/ --out docs/screenshots --wait-for idle
    python3 tools/shoot_dash.py --wait-for decode2   # two streams decoding

Prints browser console errors and failed requests so layout checks double as a smoke test.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

VIEWPORTS = {"desktop": (1440, 900, 1), "phone": (390, 844, 3)}
# JS predicates over the live hero, evaluated in the page.
WAITS = {
    "idle": "document.body.dataset.activity === 'idle' && document.querySelector('.hl-conn').dataset.state === 'open'",
    "prefill": "document.body.dataset.activity === 'prefill' && !!document.querySelector('.stream .bar:not([hidden])')",
    "decode": "document.body.dataset.activity === 'decode'",
    "decode2": "document.querySelectorAll('.stream[data-phase=decode]').length >= 2",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:7070/")
    ap.add_argument("--out", default="docs/screenshots")
    ap.add_argument("--wait-for", choices=sorted(WAITS), default="idle")
    ap.add_argument("--name", default=None, help="file name stem; defaults to the wait state")
    ap.add_argument("--full", action="store_true", help="full-page screenshot")
    ap.add_argument("--only", choices=sorted(VIEWPORTS), default=None)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    problems = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, (w, h, scale) in VIEWPORTS.items():
            if args.only and name != args.only:
                continue
            page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=scale)
            page.on("console", lambda m: m.type in ("error", "warning") and problems.append(f"{name} console {m.type}: {m.text}"))
            page.on("pageerror", lambda e: problems.append(f"{name} page error: {e}"))
            page.on("requestfailed", lambda r: problems.append(f"{name} request failed: {r.url}"))
            # The dashboard must never reach outside its own origin.
            origin = args.url.split("/", 3)[:3]
            page.on("request", lambda r, o="/".join(origin): r.url.startswith(o) or problems.append(f"{name} external request: {r.url}"))
            page.goto(args.url)
            page.wait_for_function(WAITS[args.wait_for], timeout=120_000)
            page.wait_for_timeout(1500)  # let REST panels settle
            path = out / f"{args.name or args.wait_for}-{name}.png"
            page.screenshot(path=str(path), full_page=args.full)
            print(path)
            page.close()
        browser.close()
    for line in problems:
        print(line, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

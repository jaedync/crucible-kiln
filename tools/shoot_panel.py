"""Screenshot the rack panel at exactly 1424 x 280 CSS px, device pixel ratio 1, one PNG per page.

    python3 tools/shoot_panel.py --url http://127.0.0.1:7070/panel --out ~/.cache/kiln-panel/shots
    python3 tools/shoot_panel.py --url file:///path/demo/index.html --out shots --prefix demo-

`chromium --headless=new --window-size=1424,280` gives a viewport of only 1424 x 193 (the headless window
keeps room for browser chrome), so this drives Chromium over the DevTools protocol and sets the viewport
itself. Needs only aiohttp (a Kiln dependency) and a Chromium binary. Exits 1 if the page logs an error,
throws, or draws anything outside the 1424 x 280 box.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import base64
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

WIDTH, HEIGHT = 1424, 280
PAGES = ("glance", "engine", "fleet", "today", "settings")
CHECK = ("JSON.stringify({w: innerWidth, h: innerHeight, dpr: devicePixelRatio,"
         " sw: document.documentElement.scrollWidth, sh: document.documentElement.scrollHeight,"
         " pages: [...document.querySelectorAll('.page')].length})")


class Cdp:
    def __init__(self, ws):
        self.ws, self.n, self.events = ws, 0, []

    async def call(self, method: str, **params):
        self.n += 1
        await self.ws.send_json({"id": self.n, "method": method, "params": params})
        while True:
            msg = await self.ws.receive_json(timeout=30)
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            self.events.append(msg)


def devtools_port(profile: Path, deadline: float) -> int:
    f = profile / "DevToolsActivePort"
    while time.time() < deadline:
        if f.exists() and f.read_text().strip():
            return int(f.read_text().split()[0])
        time.sleep(0.1)
    raise RuntimeError("chromium did not open its DevTools port")


def problems_in(events: list) -> list[str]:
    out = []
    for e in events:
        if e.get("method") == "Runtime.exceptionThrown":
            out.append("page error: " + e["params"]["exceptionDetails"].get("text", "?"))
        elif e.get("method") == "Runtime.consoleAPICalled" and e["params"]["type"] in ("error", "warning"):
            out.append("console: " + " ".join(str(a.get("value", a.get("description", ""))) for a in e["params"]["args"]))
        elif e.get("method") == "Log.entryAdded" and e["params"]["entry"]["level"] == "error":
            out.append("log: " + e["params"]["entry"]["text"])
    return out


async def shoot(url: str, out: Path, prefix: str, pages: list[str], wait_s: float, chromium: str) -> int:
    out.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="kiln-panel-shot-"))
    proc = subprocess.Popen([chromium, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                             f"--user-data-dir={profile}", "--remote-debugging-port=0", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bad = 0
    try:
        port = devtools_port(profile, time.time() + 20)
        async with aiohttp.ClientSession() as http:
            async with http.put(f"http://127.0.0.1:{port}/json/new?about:blank") as r:
                target = await r.json()
            async with http.ws_connect(target["webSocketDebuggerUrl"], max_msg_size=0) as ws:
                cdp = Cdp(ws)
                for domain in ("Page", "Runtime", "Log"):
                    await cdp.call(f"{domain}.enable")
                await cdp.call("Emulation.setDeviceMetricsOverride", width=WIDTH, height=HEIGHT,
                               deviceScaleFactor=1, mobile=False)
                for i, page in enumerate(pages, 1):
                    cdp.events = []
                    # Through about:blank, so a change of the hash alone still loads the page anew.
                    await cdp.call("Page.navigate", url="about:blank")
                    await asyncio.sleep(0.3)
                    await cdp.call("Page.navigate", url=f"{url.split('#')[0]}#{page}")
                    await asyncio.sleep(wait_s)
                    m = json.loads((await cdp.call("Runtime.evaluate", expression=CHECK))["result"]["value"])
                    png = await cdp.call("Page.captureScreenshot", format="png")
                    n = PAGES.index(page) + 1 if page in PAGES else i
                    path = out / f"{prefix}{n:02d}-{page}.png"
                    path.write_bytes(base64.b64decode(png["data"]))
                    print(f"{path}  viewport {m['w']}x{m['h']} dpr {m['dpr']}  content {m['sw']}x{m['sh']}")
                    issues = problems_in(cdp.events)
                    if (m["w"], m["h"], m["sw"], m["sh"]) != (WIDTH, HEIGHT, WIDTH, HEIGHT):
                        issues.append("content or viewport is not exactly 1424 x 280")
                    for line in issues:
                        print(f"  {page}: {line}", file=sys.stderr)
                    bad += bool(issues)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        shutil.rmtree(profile, ignore_errors=True)
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:7070/panel")
    ap.add_argument("--out", default=str(Path.home() / ".cache/kiln-panel/shots"))
    ap.add_argument("--prefix", default="")
    ap.add_argument("--pages", default=",".join(PAGES), help=f"comma list from {','.join(PAGES)}")
    ap.add_argument("--wait", type=float, default=3.0, help="seconds after load before the screenshot")
    ap.add_argument("--chromium", default=shutil.which("chromium") or "chromium")
    a = ap.parse_args()
    return asyncio.run(shoot(a.url, Path(a.out).expanduser(), a.prefix, a.pages.split(","), a.wait, a.chromium))


if __name__ == "__main__":
    sys.exit(main())

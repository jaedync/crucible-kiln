"""Build a self-contained demo of the rack panel: one HTML file with the real panel CSS and JS inlined, the
fonts as data URLs, and a fixed sample snapshot instead of the live API (no network calls).

    python3 tools/build_panel_demo.py --out ~/.cache/kiln-panel/demo/index.html

Open it at 1424 x 280 (or larger: the panel sits at the top left). Swipe, tap a tab, or use the arrow keys
to change the page; #engine in the URL opens that page (a LOCAL pin). The query picks a sample state:

    ?state=switch|drain|stopped|night|failed|hot   the engine in that state (default: serving); night has
                                               the night driver's lease and plan
    ?as=act|view                               a session that follows a stand-in dashboard in the page (it
                                               answers panel.set, panel.settings and panel.mute), may act or
                                               may not; the hash is then the shared screen, not a pin
    ?view=rules                                Settings opens its Rules view
    ?strip=1                                   one tap on the page (on Home: the strip shows)
    ?mute=hot|failed                           the dashboard holds a mute of that alarm (28 minutes left)
    ?flag=engine|plan                          a flag: an engine switch in the manual quiet time, or a
                                               failed plan item (shows in the sync cell with ?as=act)
    ?dim=1                                     Screen at night: Dim

The sample data names no real host, address or person, so the file can be published.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "kiln" / "web" / "static"
# Dependency order: each module only imports modules listed before it.
MODULES = ["js/format.js", "js/dom.js", "js/store.js", "js/api.js", "js/ws.js", "js/progress.js", "js/count.js",
           "js/draft.js", "js/cost.js", "js/cost_mix.js", "js/pricing.js", "js/panel/model.js", "js/panel/glance_model.js",
           "js/panel/view.js", "js/panel/glance.js",
           "js/panel/sync_model.js", "js/panel/tabs_model.js", "js/panel/tabs.js", "js/panel/engine_model.js",
           "js/panel/engine.js", "js/panel/fleet_model.js", "js/panel/fleet.js", "js/panel/today_model.js",
           "js/panel/today.js", "js/panel/settings_model.js", "js/panel/settings.js", "js/panel/pages.js",
           "js/panel/main.js"]
T0 = 1_793_110_800.0  # sample "now" (14:20 UTC); the demo's clock runs on from here
GIB = 1024 ** 3
# Strings a published demo must not carry: loopback and private addresses, Tailscale names, home paths.
# KILN_DEMO_PRIVATE adds a site's own names (a regex alternation, for example "myhost|mylab"), so the
# shapes here stay generic.
_EXTRA = os.environ.get("KILN_DEMO_PRIVATE", "").strip()
PRIVATE = re.compile(r"127\.0\.0\.1|\b10\.\d+\.|\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.|\b192\.168\.|"
                     r"\b172\.(1[6-9]|2\d|3[01])\.|\.ts\.net\b|/home/(?!user\b)|/Users/"
                     + (f"|{_EXTRA}" if _EXTRA else ""), re.I)

IMPORT_NS = re.compile(r"^import \* as (\w+) from '([^']+)';$", re.M)
IMPORT_NAMED = re.compile(r"^import \{([^}]+)\} from '([^']+)';$", re.M)
EXPORT_DECL = re.compile(r"^export (const|let|function|class|async function) (\w+)", re.M)


def resolve(base: str, spec: str) -> str:
    parts = Path(base).parent.joinpath(spec).as_posix().split("/")
    out: list[str] = []
    for p in parts:
        if p == "..":
            out.pop()
        elif p != ".":
            out.append(p)
    return "/".join(out)


def as_iife(path: str, src: str) -> str:
    """One ES module as a function scope that returns its exports (enough for Kiln's plain modules)."""
    if re.search(r"^export default|^export \{", src, re.M):
        raise ValueError(f"{path}: only `export <declaration>` is supported by the demo bundler")
    names = [m[2] for m in EXPORT_DECL.finditer(src)]
    src = IMPORT_NS.sub(lambda m: f"const {m[1]} = __mods[{json.dumps(resolve(path, m[2]))}];", src)
    src = IMPORT_NAMED.sub(lambda m: "const {" + m[1].replace(" as ", ": ") + "} = __mods["
                           + json.dumps(resolve(path, m[2])) + "];", src)
    if re.search(r"^import ", src, re.M):
        raise ValueError(f"{path}: an import form the demo bundler does not know")
    src = EXPORT_DECL.sub(lambda m: f"{m[1]} {m[2]}", src)
    return f"__mods[{json.dumps(path)}] = (() => {{\n{src}\nreturn {{ {', '.join(names)} }};\n}})();\n"


def break_matches(data: str) -> str:
    """Base64 can spell a host name by chance, and the publishing host's scan would refuse the file. A
    backslash and a newline inside a CSS string are a line continuation (ignored), so cut each match."""
    return PRIVATE.sub(lambda m: m[0][0] + "\\\n" + m[0][1:], data)


def inline_css(css: str) -> str:
    def font(m):
        data = base64.b64encode((STATIC / "css" / m[1]).resolve().read_bytes()).decode()
        return f"url('data:font/woff2;base64,{break_matches(data)}')"
    return re.sub(r"url\('(\.\./fonts/[^']+\.woff2)'\)", font, css)


def sample() -> dict:
    """A busy afternoon: two streams decoding, one prefilling a long prompt, the daily engine holding the GPU
    by day, and the night driver's plan of five items (one running, two waiting, one done, one failed)."""
    secs = list(range(int(T0) - 900, int(T0)))
    decode = [max(0, round(44 + 10 * math.sin(i / 9) + 6 * math.sin(i / 2.3))) if (i % 300) > 70 else 0
              for i in range(len(secs))]
    hw_hist = [{"ts": T0 - 120 + i, "gpu_busy_pct": 0 if (i % 100) < 12 else 97 + (i % 3),
                "power_w": 22.0 if (i % 100) < 12 else 104 + 8 * math.sin(i / 7)} for i in range(120)]
    clients = [{"key": "laptop", "name": "Studio laptop", "label": None, "color": "#1d4f91"},
               {"key": "phone", "name": "Phone", "label": "chat", "color": "#d2342a"}]
    agent = {"key": "agent", "name": "Build agent", "label": None, "color": "#f2c230"}
    live = {"decode_tps": 78.4, "decode_exact": False, "decode_note": "Live figures are events x 1.02.",
            "in_flight": 3, "slots": 4, "streams": [
        {"id": "s1", "client": clients[0], "model": "qwen3.8-flash-next", "profile": "crucible-flashnext",
         "started": T0 - 290, "first_token": T0 - 281, "phase": "decode", "tokens": 12437, "tps": 47.3,
         "prefill_progress": None},
        {"id": "s2", "client": clients[1], "model": "qwen3.8-flash-next", "profile": "crucible-flashnext",
         "started": T0 - 80, "first_token": T0 - 78, "phase": "decode", "tokens": 2431, "tps": 31.1,
         "prefill_progress": None},
        {"id": "s3", "client": agent, "model": "qwen3.8-flash-next", "profile": "crucible-flashnext",
         "started": T0 - 31, "first_token": None, "phase": "prefill", "tokens": 0, "tps": 0,
         # A resumed agent turn, as live/prefill.py builds it: 61,440 cached, 44,367 to compute.
         "prefill_progress": {"done": 77824, "total": 105807, "elapsed_s": 11.0, "at": T0, "start": 61440,
                              "start_estimated": False, "rate": 1489.5, "rate_from": "engine", "next": 81920,
                              "reports": 4}}]}
    hw = {"ts": T0, "tctl_c": 71.4, "edge_c": 63.0, "mem_board_c": 50.1, "ambient_c": 31.0, "fan_rpm": 1784,
          "power_w": 109.6, "gpu_busy_pct": 98, "gtt_used_bytes": int(88.6 * GIB), "gtt_total_bytes": 133143986176,
          "vram_used_bytes": 268435456, "vram_total_bytes": 536870912, "sclk_mhz": 2900,
          "ram_avail_bytes": int(30.2 * GIB), "profile": "performance"}
    engine = {"profile": "crucible-flashnext", "label": "Crucible · Qwen3.8 Flash-Next", "engine": "crucible",
              "model": "qwen3.8-flash-next", "state": "ready", "since": T0 - 8130, "target": None, "detail": "", "phase": None, "phase_since": None,
              "kv": None, "blocked": None, "cache": None,
              "schedule": {"day": "09:00-18:00", "tz": "UTC", "in_day": True, "next_change": T0 + 13200,
                           "day_profile": "crucible-flashnext", "holds": ["kiln"], "dev_jobs": [],
                           "drain_timeout_s": 900}}
    snapshot = {"v": 1, "type": "snapshot", "ts": T0, "live": live, "second": None, "seconds": [],
                "last": {"id": "r1", "ts": T0 - 52, "ttft_s": 0.62, "decode_tps": 46.8, "prefill_tps": 1840.0,
                         "prompt": 41200, "cached": 39800, "completion": 733, "finish": "stop", "think_close": None},
                "engine": engine, "hw": hw,
                "today": {"requests": 322, "prompt": 3_900_000, "cached": 3_393_000, "completion": 61_000,
                          "cost": {"reference": 2.71, "openrouter": 0.31}},
                "health": {"gateway": "up", "dash_started": T0 - 9000, "parse_errors_24h": 0,
                           "last_request_ts": T0 - 52},
                "host": "rack-gpu", "hosts": {},
                # A prompt is read just before each decode run: the glance columns mark those windows.
                "series_1s": {"t": secs, "decode": decode,
                              "prefill": [1500 if 55 < (i % 300) <= 70 else 0 for i in range(len(secs))]},
                "profiles": PROFILES, "me": {"can_act": False, "identity": None}, "build": "demo",
                "pricing": {"reference": "Claude Sonnet 5.5", "input_per_m": 2.0, "cached_per_m": 0.1,
                            "output_per_m": 10.0}}
    # By day the daily engine holds the GPU lock itself (no lease line, no limit), and the night driver left
    # a plan behind: the Fleet page shows both.
    daily = {"agent": "daily engine", "kind": "serve", "prio": None, "elapsed_s": None, "limit_s": None,
             "typical_s": None, "daily": True}
    plan = {"night": "2026-01-15", "updated_ago_s": 60.0, "items": [
        {"id": "check-1", "label": "First check", "state": "pass", "expect_min": 35, "elapsed_s": None,
         "held_s": 1980.0, "rc": 0},
        {"id": "check-2", "label": "Second check", "state": "running", "expect_min": 20, "elapsed_s": 720.0,
         "held_s": None, "rc": None},
        {"id": "long-run", "label": "Long run", "state": "waiting", "expect_min": 45, "elapsed_s": None,
         "held_s": None, "rc": None},
        {"id": "short-test", "label": "Short test", "state": "fail", "expect_min": 15, "elapsed_s": None,
         "held_s": 900.0, "rc": 1},
        {"id": "check-3", "label": "Third check", "state": "waiting", "expect_min": 30, "elapsed_s": None,
         "held_s": None, "rc": None}]}
    panel = {"v": 1, "ts": T0, "fleet": {
        "lease": daily, "holds": ["kiln"], "holds_total": 1, "plan": plan,
        "queue": [{"agent": "job-e", "prio": 1, "waited_s": 1260.0, "kind": "bench", "limit_s": 960.0,
                   "typical_s": 212.0},
                  {"agent": "job-d", "prio": 2, "waited_s": 410.0, "kind": "gates", "limit_s": 360.0,
                   "typical_s": 188.0},
                  {"agent": "job-with-a-long-name-for-layout", "prio": 0, "waited_s": 7000.0, "kind": "gates",
                   "limit_s": 660.0, "typical_s": None},
                  {"agent": "job-f", "prio": 2, "waited_s": 5200.0, "kind": "other", "limit_s": 1560.0,
                   "typical_s": None},
                  {"agent": "job-g", "prio": 1, "waited_s": 900.0, "kind": "train", "limit_s": 1080.0,
                   "typical_s": 840.0},
                  {"agent": "job-h", "prio": 2, "waited_s": 60.0, "kind": "bench", "limit_s": 900.0,
                   "typical_s": 420.0}],
        "recent": [{"agent": "job-b", "kind": "bench", "prio": 1, "rc": 0, "held_s": 212.0, "ago_s": 8140.0},
                   {"agent": "job-d", "kind": "gates", "prio": 2, "rc": 0, "held_s": 188.0, "ago_s": 8400.0},
                   {"agent": "job-a", "kind": "other", "prio": 1, "rc": 1, "held_s": 31.0, "ago_s": 8700.0},
                   {"agent": "job-c", "kind": "train", "prio": 0, "rc": 0, "held_s": 344.0, "ago_s": 9100.0}]}}
    # Requests per hour (store/buckets.py): the current hour is 2 PM, 20 minutes in.
    hours = [0, 0, 0, 9, 0, 0, 0, 0, 81, 24, 52, 58, 21, 37, 40] + [0] * 9
    t0 = T0 - 14 * 3600 - 1200
    usage = {"period": "today", "group": "device", "host": "rack-gpu", "rows": [
        {"key": "laptop", "name": "Studio laptop", "color": "#1d4f91", "requests": 196},
        {"key": "phone", "name": "Phone", "color": "#d2342a", "requests": 71},
        {"key": "eval", "name": "Nightly eval with a long name", "color": "#f2c230", "requests": 55}],
        "totals": {"requests": sum(hours), "errors": 0, "busy_s": 9660.0},
        "buckets": {"step_s": 3600, "tz": "UTC", "t": [t0 + i * 3600 for i in range(24)],
                    "local": [f"2026-10-27 {i:02d}:00" for i in range(24)], "requests": hours}}
    return {"snapshot": snapshot, "panel": panel, "usage": usage, "hw_history": hw_hist, "settings": SETTINGS,
            "variants": variants(engine, hw, panel)}


PROFILES = [
    {"name": "crucible-flashnext", "label": "Crucible · Qwen3.8 Flash-Next", "engine": "crucible",
     "model": "qwen3.8-flash-next", "context": 262144, "slots": 4, "memory_gib": 110, "active": True, "disabled": None},
    {"name": "halogen-flashnext", "label": "Halogen · Qwen3.8 Flash-Next", "engine": "halogen",
     "model": "qwen3.8-flash-next", "context": 131072, "slots": 2, "memory_gib": 96, "active": False, "disabled": None}]
# The dashboard's panel settings (live/panel_screen.settings_view), with auto-switch on and two rules on.
SETTINGS = {"idle_home_s": 300.0, "manual_quiet_s": 300.0, "min_dwell_s": 30.0, "stable_s": 3.0, "alarm_home": True,
            "auto": {"enabled": True, "rules": {
                "engine_switch": {"on": True, "screen": "engine", "hold_s": 300.0},
                "night_start": {"on": True, "screen": "fleet", "hold_s": 120.0},
                "day_start": {"on": False, "screen": "glance", "hold_s": 0.0}}}}


def variants(engine: dict, hw: dict, panel: dict) -> dict:
    """Other engine states, as snapshot keys that replace the serving ones (?state=<name>). `fleet_panel`
    replaces the /api/v1/panel sample: at night the night driver holds the GPU and runs its plan."""
    idle_live = {"decode_tps": 0, "decode_exact": True, "decode_note": None, "in_flight": 0, "slots": 4, "streams": []}
    quiet_hw = {**hw, "gpu_busy_pct": 4, "power_w": 31.0, "sclk_mhz": 600, "gtt_used_bytes": int(2.1 * GIB)}
    load = {**engine, "state": "starting", "since": T0 - 20, "phase": "load", "phase_since": T0 - 20,
            "detail": "starting kiln-crucible-flashnext", "kv": None}
    drain = {**engine, "profile": "halogen-flashnext", "label": "Halogen · Qwen3.8 Flash-Next", "state": "stopping",
             "target": "crucible-flashnext", "since": T0 - 80, "phase": "drain", "phase_since": T0 - 72,
             "detail": "fleet paused; waiting for development GPU jobs to finish: job-c (train)"}
    stopped = {**engine, "profile": None, "label": None, "engine": None, "model": None, "state": "stopped",
               "since": T0 - 3 * 3600, "detail": "stopped by the owner", "kv": None,
               "blocked": "another container is running (dev-scratch)"}
    night = {**stopped, "blocked": None, "detail": "day window ended",
             "schedule": {**engine["schedule"], "in_day": False, "next_change": T0 + 67200}}
    failed = {**engine, "state": "failed", "since": T0 - 140, "kv": None,
              "detail": "kiln-crucible-flashnext.service failed: Main process exited, code=exited, status=1/FAILURE;"
                        " hip error: out of memory while it allocated the KV cache (110.4 GiB asked, 96.0 GiB free)"}
    driver = {"agent": "night-driver", "kind": "bench", "prio": 1, "elapsed_s": 3911.0, "limit_s": 14400.0,
              "typical_s": None}
    night_fleet = {**panel["fleet"], "lease": driver, "holds": [], "holds_total": 0,
                   "queue": panel["fleet"]["queue"][:3],
                   "recent": [{"agent": "job-d", "kind": "gates", "prio": 2, "rc": 1, "held_s": 31.0, "ago_s": 3600.0},
                              *panel["fleet"]["recent"]]}
    return {
        "hot": {"hw": {**hw, "edge_c": 91.2, "fan_rpm": 3120}},
        "switch": {"engine": load, "live": idle_live, "hw": {**hw, "gtt_used_bytes": int(61.3 * GIB), "gpu_busy_pct": 35}},
        "drain": {"engine": drain, "live": idle_live, "hw": quiet_hw},
        "stopped": {"engine": stopped, "live": idle_live, "hw": quiet_hw},
        "night": {"engine": night, "live": idle_live, "hw": {**quiet_hw, "gpu_busy_pct": 99, "power_w": 104.0},
                  "fleet_panel": {**panel, "fleet": night_fleet}},
        "failed": {"engine": failed, "live": idle_live, "hw": quiet_hw},
    }


BOOT = """
(() => {
  const D = window.KILN_PANEL_DEMO;
  const q = new URLSearchParams(location.search);
  const store = __mods['js/store.js'].createStore();
  const { fleet_panel: fleetPanel, ...variant } = D.variants[q.get('state')] || {};
  const snap = { ...D.snapshot, ...variant };
  const as = q.get('as');
  // The dashboard's shared state: a mute, a flag, the night display (?mute, ?flag, ?dim).
  const FLAGS = {
    engine: { screen: 'engine', tone: 'warn', why: 'Engine switch: see Engine', cause: 'quiet', quiet_until: snap.ts + 230 },
    plan: { screen: 'fleet', tone: 'bad', why: 'Plan item failed: Short test', cause: 'fault', quiet_until: null } };
  const settings = { ...D.settings, night_display: q.get('dim') ? 'dim' : 'normal' };
  const mute = q.get('mute') ? { kind: q.get('mute'), until: snap.ts + 1680 } : null;
  const flags = q.get('flag') && FLAGS[q.get('flag')] ? [FLAGS[q.get('flag')]] : [];
  // A stand-in for the dashboard: it answers like dash/panel_sync.py, a turn later, as a socket would.
  let link = null;
  if (as === 'act' || as === 'view') {
    const isObj = (v) => v && typeof v === 'object' && !Array.isArray(v);
    const merged = (a, b) => Object.fromEntries([...Object.keys(a), ...Object.keys(b)].map((k) =>
      [k, isObj(a[k]) && isObj(b[k]) ? merged(a[k], b[k]) : k in b ? b[k] : a[k]]));
    const shared = { screen: location.hash.slice(1) || 'glance', seq: 1, at: snap.ts - 30,
      by: { source: 'touch', who: 'rack', sid: 2, req: 1 }, what: 'screen', mute, flags, settings };
    snap.me = { can_act: as === 'act', identity: 'demo', sid: 1 };
    snap.panel = shared;
    link = { send(msg) {
      setTimeout(() => {
        if (msg.type === 'panel.touch') return;
        shared.seq += 1;
        shared.at = Date.now() / 1000 + store.get().skew;
        shared.by = { source: 'remote', who: 'demo', sid: 1, req: msg.req };
        if (msg.type === 'panel.set') shared.screen = msg.screen;
        if (msg.type === 'panel.settings') shared.settings = merged(shared.settings, msg.settings);
        if (msg.type === 'panel.mute') shared.mute = msg.kind ? { kind: msg.kind, until: shared.at + 1800 } : null;
        shared.what = msg.type === 'panel.set' ? 'screen' : msg.type.slice(6);
        const { settings, ...rest } = shared;
        store.emit('panel', { type: 'panel', ...rest, ...(msg.type === 'panel.settings' ? { settings } : {}) });
      }, 0);
      return true;
    } };
  } else {
    // A pinned or plain demo follows nothing, but it still draws the dashboard's mute and night display.
    snap.panel = { screen: location.hash.slice(1) || 'glance', seq: 1, at: snap.ts - 30,
      by: { source: 'touch', who: 'rack', sid: 2, req: 1 }, what: 'screen', mute, flags, settings };
  }
  const app = __mods['js/panel/main.js'].boot({ store, live: false, panel: fleetPanel || D.panel, usage: D.usage, link,
    pinHash: !link });
  store.set({ conn: 'open', tick: snap, engine: snap.engine, profiles: snap.profiles, pricing: snap.pricing,
              skew: snap.ts - Date.now() / 1000 });
  store.emit('snapshot', snap);
  for (const hw of D.hw_history) store.emit('tick', { ...snap, seconds: [], hw: { ...snap.hw, ...hw } });
  store.emit('tick', snap);
  if (q.get('view')) app.pages.find((p) => p.id === 'settings').open(q.get('view'));
  if (q.get('strip')) {
    const at = { clientX: 700, clientY: 100, bubbles: true };
    document.body.dispatchEvent(new PointerEvent('pointerdown', at));
    document.body.dispatchEvent(new PointerEvent('pointerup', at));
  }
  app.draw();
})();
"""


def bundle(modules: list[str]) -> str:
    """The modules as one script. A module imported but not listed earlier would be undefined at run time
    and leave the demo blank, so it fails the build instead."""
    out = "const __mods = {};\n"
    for i, path in enumerate(modules):
        src = (STATIC / path).read_text()
        for m in [*IMPORT_NS.finditer(src), *IMPORT_NAMED.finditer(src)]:
            dep = resolve(path, m[2])
            if dep not in modules[:i]:
                raise ValueError(f"{path} imports {dep}, which MODULES does not list before it")
        out += as_iife(path, src)
    return out


def build() -> str:
    html = (STATIC / "panel.html").read_text()
    css = inline_css((STATIC / "css/panel.css").read_text())
    bundle_js = bundle(MODULES)
    data = json.dumps(sample(), separators=(",", ":"))
    html = re.sub(r'\s*<link rel="(preload|icon)"[^>]*>', "", html)
    html = html.replace('<link rel="stylesheet" href="css/panel.css">', f"<style>\n{css}</style>")
    html = html.replace('<script type="module" src="js/panel/main.js"></script>', "")
    # The bundle runs after the body exists, as the module script would (modules are deferred).
    script = f"<script>window.KILN_PANEL_DEMO = {data};</script>\n<script>\n{bundle_js}{BOOT}</script>\n"
    html = html.replace("</body>", script + "</body>")
    if 'src="' in html or "href=\"" in html:
        raise ValueError("the demo still links an outside file")
    m = PRIVATE.search(html)
    if m:
        raise ValueError(f"the demo contains a private-looking string at offset {m.start()}: {m[0]!r}")
    return html


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(Path.home() / ".cache/kiln-panel/demo/index.html"))
    out = Path(ap.parse_args().out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build())
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

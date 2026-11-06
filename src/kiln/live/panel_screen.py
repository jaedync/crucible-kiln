"""The rack panel's shared screen: which page every following /panel session shows, and why.

Pure: frozen values in, frozen values out, with the clocks passed in. `now` is monotonic seconds (timers);
`wall` is epoch seconds (only for `at`, which the file keeps across a restart). The dashboard
(dash/panel_sync.py) holds one PanelScreen, applies person changes from the live socket, and calls `step`
once a tick for the idle return, the alarm jump and the opt-in auto rules.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

PANEL_SCREENS = ("glance", "engine", "fleet", "today", "settings")
HOME = "glance"
# Screens of step 1 that Engine replaced. A tab of an older build may still send one until it reloads.
RETIRED = ("live", "gpu")
FILE_V = 2  # v1 (step 1) had no Engine screen, so its engine_switch target was Home
# The GPU edge alarm, as the panel's HOT_EDGE_C (panel/glance_model.js). It clears a little lower, so a
# reading that wobbles about 90 °C is one alarm, not a jump home every few seconds.
HOT_EDGE_C = 90.0
HOT_CLEAR_C = 87.0
SOURCES = ("touch", "remote", "idle", "alarm", "start")  # and "auto:<rule>"
RULES = ("engine_switch", "night_start", "day_start")
# Each rule fires on one edge of one input (Seen): a steady state never fires again.
RULE_EDGES = {"engine_switch": "switch", "night_start": "night", "day_start": "day"}
MAX_S = 7 * 24 * 3600.0
IDLE_NEVER = 0.0  # idle_home_s: no idle return at all (Settings: "Never")
LIMITS = {"idle_home_s": (30, MAX_S), "manual_quiet_s": (0, MAX_S), "min_dwell_s": (0, MAX_S),
          "stable_s": (0, 3600), "hold_s": (0, MAX_S)}
# "Off" needs output power control that is not checked on the rack's HDMI display: only Dim.
NIGHT_DISPLAYS = ("normal", "dim")
# The alarms a person may mute, one kind at a time. NO DATA is not here: only the session whose feed
# stopped can see it, so the dashboard cannot hold a mute for it.
ALARM_KINDS = ("hot", "failed")
MUTE_S = 1800.0  # 30 minutes; an alarm that still holds at the end is a new alarm
NAMES = {"glance": "Home", "engine": "Engine", "fleet": "Fleet", "today": "Today", "settings": "Settings"}
FLAG_WHY = {"engine_switch": "Engine switch", "night_start": "Night began", "day_start": "Day began"}
# Each rule's flag holds while its input stays in the state that raised it (Seen field, value).
FLAG_HOLDS = {"engine_switch": ("switching", True), "night_start": ("night", True), "day_start": ("night", False)}
PLAN_FAIL = "plan_fail"


@dataclass(frozen=True)
class By:
    source: str
    who: str | None = None
    sid: int | None = None
    req: int | None = None


@dataclass(frozen=True)
class Rule:
    on: bool
    screen: str
    hold_s: float  # an auto change keeps its screen this long before the idle return may take it away


@dataclass(frozen=True)
class Mute:
    kind: str
    until: float  # wall seconds: the file keeps it across a restart, and the panels show the minutes left


@dataclass(frozen=True)
class Flag:
    """News on a tab that the panel did not open: a rule's input changed while the rule could not act
    (auto off, the quiet time, the dwell), or a fault (a failed night plan item)."""
    screen: str
    tone: str  # "warn": a change of state (yellow); "bad": a fault (red)
    rule: str  # a rule name or PLAN_FAIL: the flag ends with that state
    why: str
    cause: str  # "off", "quiet", "dwell" or "fault": the sync cell says why the screen did not change
    quiet_until: float | None = None  # wall seconds: the end of the manual quiet time, for "4 m left"


@dataclass(frozen=True)
class Settings:
    idle_home_s: float = 300.0  # 5 minutes from the last person input; 0 never
    manual_quiet_s: float = 300.0
    min_dwell_s: float = 30.0
    stable_s: float = 3.0
    alarm_home: bool = True
    night_display: str = "normal"
    auto_enabled: bool = False
    rules: tuple[tuple[str, Rule], ...] = (
        ("engine_switch", Rule(False, "engine", 300.0)),  # a switch takes minutes: Engine shows its steps
        ("night_start", Rule(False, "fleet", 120.0)),
        ("day_start", Rule(False, "glance", 0.0)),
    )

    def rule(self, name: str) -> Rule:
        return dict(self.rules)[name]


DEFAULT_SETTINGS = Settings()


@dataclass(frozen=True)
class Inputs:
    """What the tick knows: engine failed, GPU edge °C, a switch or start under way, night (None: unknown)."""
    failed: bool = False
    edge_c: float | None = None
    switching: bool = False
    night: bool | None = None
    plan_failed: int | None = None  # failed items in the night plan; None: no fleet data
    plan_failed_label: str | None = None


@dataclass(frozen=True)
class Track:
    """An input that counts only after it holds for stable_s: `value` is the held one, `cand` a new one."""
    value: object = None
    cand: object = None
    since: float | None = None


@dataclass(frozen=True)
class Seen:
    failed: bool
    hot: bool
    switching: Track
    night: Track
    plan_failed: int | None = None

    @property
    def alarm(self) -> bool:
        return self.failed or self.hot

    def holds(self, kind: str) -> bool:
        return self.hot if kind == "hot" else self.failed


@dataclass(frozen=True)
class PanelScreen:
    screen: str = HOME
    seq: int = 0
    at: float = 0.0
    by: By = By("start")
    settings: Settings = DEFAULT_SETTINGS
    # Memory only, monotonic: the last person input, an auto rule's hold, the last auto change, the inputs.
    active_at: float | None = None
    hold_until: float | None = None
    last_auto_at: float | None = None
    seen: Seen | None = None
    mute: Mute | None = None
    flags: tuple[Flag, ...] = ()  # memory only: news is about now, a restart starts without it
    what: str = "screen"  # the last change: screen, settings, mute or flags (the sync cell's words)


# -- settings ----------------------------------------------------------------------------------------
def settings_view(s: Settings) -> dict:
    return {"idle_home_s": s.idle_home_s, "manual_quiet_s": s.manual_quiet_s, "min_dwell_s": s.min_dwell_s,
            "stable_s": s.stable_s, "alarm_home": s.alarm_home, "night_display": s.night_display,
            "auto": {"enabled": s.auto_enabled,
                     "rules": {n: {"on": r.on, "screen": r.screen, "hold_s": r.hold_s} for n, r in s.rules}}}


def _merged(base: dict, patch, path: str) -> dict:
    if not isinstance(patch, dict):
        raise ValueError(f"{path or 'settings'} must be an object")
    out = dict(base)
    for key, value in patch.items():
        if key not in base:
            raise ValueError(f"unknown setting: {path}{str(key)[:40]}")
        out[key] = _merged(base[key], value, f"{path}{key}.") if isinstance(base[key], dict) else value
    return out


def _seconds(v, name: str) -> float:
    lo, hi = LIMITS[name]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise ValueError(f"{name} must be a number of seconds from {lo:g} to {hi:g}")
    return float(v)


def _idle(v) -> float:
    if v == IDLE_NEVER and not isinstance(v, bool):
        return IDLE_NEVER
    return _seconds(v, "idle_home_s")


def _flag(v, name: str) -> bool:
    if not isinstance(v, bool):
        raise ValueError(f"{name} must be true or false")
    return v


def _night_display(v) -> str:
    if v not in NIGHT_DISPLAYS or not isinstance(v, str):
        raise ValueError(f"night_display must be one of {', '.join(NIGHT_DISPLAYS)}")
    return v


def _rule(name: str, d: dict) -> Rule:
    if d["screen"] not in PANEL_SCREENS:
        raise ValueError(f"{name}.screen must be one of {', '.join(PANEL_SCREENS)}")
    return Rule(_flag(d["on"], f"{name}.on"), d["screen"], _seconds(d["hold_s"], "hold_s"))


def merge_settings(s: Settings, patch) -> Settings:
    """A partial settings object over the current ones. Unknown keys, wrong types and out-of-range numbers
    raise ValueError with a message for the person who sent them."""
    d = _merged(settings_view(s), patch, "")
    return Settings(idle_home_s=_idle(d["idle_home_s"]),
                    manual_quiet_s=_seconds(d["manual_quiet_s"], "manual_quiet_s"),
                    min_dwell_s=_seconds(d["min_dwell_s"], "min_dwell_s"),
                    stable_s=_seconds(d["stable_s"], "stable_s"),
                    alarm_home=_flag(d["alarm_home"], "alarm_home"),
                    night_display=_night_display(d["night_display"]),
                    auto_enabled=_flag(d["auto"]["enabled"], "auto.enabled"),
                    rules=tuple((n, _rule(n, d["auto"]["rules"][n])) for n in RULES))


# -- person changes --------------------------------------------------------------------------------------
def _changed(st: PanelScreen, screen: str, by: By, wall: float, what: str = "screen", **extra) -> PanelScreen:
    """A change that all sessions hear about. The screen that now shows has no news left: its flag goes."""
    flags = tuple(f for f in extra.pop("flags", st.flags) if f.screen != screen)
    return replace(st, screen=screen, seq=st.seq + 1, at=wall, by=by, what=what, flags=flags, **extra)


def _bumped(st: PanelScreen, what: str, **extra) -> PanelScreen:
    """A change by the dashboard that is no screen change (a flag, a mute's end): who set the screen stays."""
    return replace(st, seq=st.seq + 1, what=what, **extra)


def person_set(prev: PanelScreen, screen: str, by: By, now: float, wall: float) -> PanelScreen:
    """A person picked a screen. It always counts as a change, also to the screen that shows, so the
    sender gets its echo. A person's choice ends an auto rule's hold."""
    if screen in RETIRED:
        raise ValueError("this page is gone in the new panel; reload the page")
    if screen not in PANEL_SCREENS:
        raise ValueError("unknown screen")
    return _changed(prev, screen, by, wall, active_at=now, hold_until=None)


def person_touch(prev: PanelScreen, now: float) -> PanelScreen:
    """A person is at a following session: the idle time starts again. Not a change: nothing is sent."""
    return replace(prev, active_at=now)


def person_settings(prev: PanelScreen, patch, by: By, now: float, wall: float) -> PanelScreen:
    return _changed(prev, prev.screen, by, wall, "settings", settings=merge_settings(prev.settings, patch),
                    active_at=now)


def person_mute(prev: PanelScreen, kind, by: By, now: float, wall: float) -> PanelScreen:
    """A person muted the alarm of `kind` for MUTE_S, or ended the mute (kind None). A mute replaces one of
    another kind: one alarm kind is muted at a time."""
    if kind is None:
        if prev.mute is None:
            raise ValueError("nothing is muted")
        return _changed(prev, prev.screen, by, wall, "mute", mute=None, active_at=now)
    if kind == "nodata":
        raise ValueError("NO DATA cannot be muted: each panel finds it by itself")
    if kind not in ALARM_KINDS:
        raise ValueError("unknown alarm kind")
    if prev.seen is None or not prev.seen.holds(kind):
        raise ValueError("no such alarm now")
    return _changed(prev, prev.screen, by, wall, "mute", mute=Mute(kind, wall + MUTE_S), active_at=now)


# -- the tick step ---------------------------------------------------------------------------------------
def _hot(was_hot: bool, edge_c) -> bool:
    if edge_c is None:
        return False
    return edge_c >= HOT_EDGE_C or (was_hot and edge_c >= HOT_CLEAR_C)


def _held(track: Track, raw, now: float, stable_s: float) -> tuple[Track, bool]:
    """The track after one reading, and whether its held value changed now."""
    if raw == track.value:
        return Track(track.value), False
    cand, since = (track.cand, track.since) if raw == track.cand and track.since is not None else (raw, now)
    if now - since >= stable_s:
        return Track(raw), True
    return Track(track.value, cand, since), False


def _observe(seen: Seen | None, inputs: Inputs, now: float, stable_s: float) -> tuple[Seen, set[str]]:
    if seen is None:  # the first step after a start learns the inputs; it fires nothing
        hot = _hot(False, inputs.edge_c)
        return Seen(inputs.failed, hot, Track(inputs.switching), Track(inputs.night), inputs.plan_failed), set()
    hot = _hot(seen.hot, inputs.edge_c)
    switching, sw_changed = _held(seen.switching, inputs.switching, now, stable_s)
    night, night_changed = _held(seen.night, inputs.night, now, stable_s)
    edges = set()
    # Each alarm kind has its own edge: a mute holds one kind, and a new kind still takes the screen.
    if hot and not seen.hot:
        edges.add("alarm:hot")
    if inputs.failed and not seen.failed:
        edges.add("alarm:failed")
    if sw_changed and switching.value is True:
        edges.add("switch")
    if night_changed and seen.night.value is not None:  # unknown -> known is not an edge
        edges.add("night" if night.value is True else "day" if night.value is False else "")
    plan = inputs.plan_failed
    if plan is not None and seen.plan_failed is not None and plan > seen.plan_failed:
        edges.add(PLAN_FAIL)
    # Without fleet data the last count stays, so the data's return with the same count is no edge.
    kept = seen.plan_failed if plan is None else plan
    return Seen(inputs.failed, hot, switching, night, kept), edges - {""}


def resting_screen(st: PanelScreen) -> str:
    """Where the idle return goes: Fleet at night when night_start is on, else Home. Home during an alarm."""
    s, seen = st.settings, st.seen
    if seen is None or seen.alarm:
        return HOME
    night = s.rule("night_start")
    return night.screen if s.auto_enabled and night.on and seen.night.value is True else HOME


def _quiet(st: PanelScreen, now: float) -> bool:
    s = st.settings
    manual = st.active_at is not None and now - st.active_at < s.manual_quiet_s
    dwell = st.last_auto_at is not None and now - st.last_auto_at < s.min_dwell_s
    return manual or dwell


def _auto(st: PanelScreen, edges: set[str], now: float, wall: float) -> tuple[PanelScreen | None, str | None]:
    """The change of the first rule that fires, and its name. An edge in the quiet time is dropped, not kept
    for later: a flag on its tab says what the panel did not open (_flagged)."""
    s = st.settings
    if not s.auto_enabled or _quiet(st, now):
        return None, None
    for name in RULES:
        rule = s.rule(name)
        if rule.on and RULE_EDGES[name] in edges and rule.screen != st.screen:
            return _changed(st, rule.screen, By(f"auto:{name}"), wall, last_auto_at=now,
                            hold_until=now + rule.hold_s), name
    return None, None


def _idle_due(st: PanelScreen, now: float) -> bool:
    if st.settings.idle_home_s == IDLE_NEVER:
        return False
    since_person = -math.inf if st.active_at is None else st.active_at + st.settings.idle_home_s
    return now >= max(since_person, -math.inf if st.hold_until is None else st.hold_until)


def _mute_step(st: PanelScreen, edges: set[str], wall: float) -> tuple[PanelScreen, set[str]]:
    """A mute ends at its time or when its alarm clears. An alarm that still holds at the end is news again."""
    m = st.mute
    if m is None:
        return st, edges
    holds = st.seen.holds(m.kind)
    if holds and wall < m.until:
        return st, edges
    return _bumped(st, "mute", mute=None), edges | ({f"alarm:{m.kind}"} if holds else set())


def _flag_holds(f: Flag, seen: Seen) -> bool:
    if f.rule == PLAN_FAIL:
        return seen.plan_failed != 0
    field, value = FLAG_HOLDS[f.rule]
    return getattr(seen, field).value is value


def _cause(st: PanelScreen, rule: Rule, now: float, wall: float) -> tuple[str, float | None] | None:
    """Why a rule's edge did not change the screen, or None when the rule is off (it opens nothing)."""
    s = st.settings
    if not s.auto_enabled:
        return "off", None
    if not rule.on:
        return None
    if st.active_at is not None and now - st.active_at < s.manual_quiet_s:
        return "quiet", wall + (st.active_at + s.manual_quiet_s - now)
    return "dwell", None


def _new_flags(st: PanelScreen, edges: set[str], fired: str | None, now: float, wall: float,
               label: str | None) -> list[Flag]:
    out = []
    for name in RULES:
        rule = st.settings.rule(name)
        why = _cause(st, rule, now, wall) if RULE_EDGES[name] in edges and name != fired else None
        if why is not None:
            out.append(Flag(rule.screen, "warn", name, f"{FLAG_WHY[name]}: see {NAMES[rule.screen]}", *why))
    if PLAN_FAIL in edges:  # a fault: flagged whatever the rules say
        out.append(Flag("fleet", "bad", PLAN_FAIL, f"Plan item failed: {label}" if label else "A plan item failed",
                        "fault"))
    return out


def _flagged(st: PanelScreen, changed: bool, edges: set[str], fired: str | None, now: float, wall: float,
             label: str | None) -> PanelScreen:
    """The flags after this tick: ended ones go, new ones come (one per tab, red over yellow), and none for
    the screen that shows. A flag alone is a change too (`changed`: this tick changed something already), so
    every session draws it."""
    flags = {f.screen: f for f in st.flags if _flag_holds(f, st.seen)}
    for f in _new_flags(st, edges, fired, now, wall, label):
        old = flags.get(f.screen)
        if f.screen != st.screen and not (old is not None and old.tone == "bad" and f.tone != "bad"):
            flags[f.screen] = f
    nxt = tuple(flags.values())
    if nxt == st.flags:
        return st
    return replace(st, flags=nxt) if changed else _bumped(st, "flags", flags=nxt)


def step(prev: PanelScreen, inputs: Inputs, now: float, wall: float) -> PanelScreen:
    """One tick: at most one screen change, in this order: the alarm jump, an auto rule, the idle return.
    Before it a mute may end; after it the tab flags follow the edges no rule acted on."""
    seen, edges = _observe(prev.seen, inputs, now, prev.settings.stable_s)
    st, edges = _mute_step(replace(prev, seen=seen), edges, wall)
    alarms = {e for e in edges if e.startswith("alarm:")} - ({f"alarm:{st.mute.kind}"} if st.mute else set())
    fired = None
    if alarms and st.settings.alarm_home and st.screen != HOME:
        nxt = _changed(st, HOME, By("alarm"), wall)  # at once: no stable time, quiet time or dwell
    else:
        nxt, fired = _auto(st, edges, now, wall)
    if nxt is None and st.screen != resting_screen(st) and _idle_due(st, now):
        nxt = _changed(st, resting_screen(st), By("idle"), wall, hold_until=None)
    nxt = nxt or st
    return _flagged(nxt, nxt.seq != prev.seq, edges, fired, now, wall, inputs.plan_failed_label)


# -- wire and file ---------------------------------------------------------------------------------------
def _by_view(by: By) -> dict:
    return {"source": by.source, "who": by.who, "sid": by.sid, "req": by.req}


def _flag_view(f: Flag) -> dict:
    return {"screen": f.screen, "tone": f.tone, "why": f.why, "cause": f.cause, "quiet_until": f.quiet_until}


def view(st: PanelScreen, settings: bool = False) -> dict:
    """The wire form. The mute and the flags are small, so every message carries them: a session never has
    to merge an old one."""
    mute = None if st.mute is None else {"kind": st.mute.kind, "until": st.mute.until}
    out = {"screen": st.screen, "seq": st.seq, "at": st.at, "by": _by_view(st.by), "what": st.what, "mute": mute,
           "flags": [_flag_view(f) for f in st.flags]}
    return {**out, "settings": settings_view(st.settings)} if settings else out


def to_file(st: PanelScreen) -> dict:
    doc = {"v": FILE_V, **view(st, settings=True)}
    del doc["flags"]  # news is about now: a restart starts without it
    return doc


def _number(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _by_from(d) -> By:
    if not isinstance(d, dict) or not isinstance(d.get("source"), str):
        return By("start")
    who, sid, req = d.get("who"), d.get("sid"), d.get("req")
    return By(d["source"][:40], who[:80] if isinstance(who, str) else None,
              sid if isinstance(sid, int) else None, req if isinstance(req, int) else None)


def _upgraded(settings, v) -> object:
    """Settings of an older file, made valid for this build: a retired rule target becomes Home, and the
    v1 engine_switch target (Home, its default: v1 had no Engine screen and no settings UI) becomes Engine.
    Anything else goes to merge_settings unchanged, which checks it."""
    try:
        rules = settings["auto"]["rules"]
        items = list(rules.items())
    except (KeyError, TypeError, AttributeError):
        return settings
    fixed = {}
    for name, rule in items:
        if isinstance(rule, dict) and rule.get("screen") in RETIRED:
            rule = {**rule, "screen": HOME}
        if name == "engine_switch" and v == 1 and isinstance(rule, dict) and rule.get("screen") == HOME:
            rule = {**rule, "screen": "engine"}
        fixed[name] = rule
    return {**settings, "auto": {**settings["auto"], "rules": fixed}}


def _mute_from(d, wall: float) -> Mute | None:
    if not isinstance(d, dict) or d.get("kind") not in ALARM_KINDS:
        return None
    until = _number(d.get("until"))
    return Mute(d["kind"], until) if until is not None and wall < until <= wall + MUTE_S else None


def from_file(doc, wall: float, now: float) -> PanelScreen:
    """The state a start loads. A deploy must not move the rack, so the screen stays, unless it was set
    longer ago than the idle time (the rack would have gone home by then). Anything odd gives Home."""
    if not isinstance(doc, dict):
        return PanelScreen()
    try:
        settings = merge_settings(DEFAULT_SETTINGS, _upgraded(doc.get("settings", {}), doc.get("v")))
    except ValueError:
        settings = DEFAULT_SETTINGS
    seq = doc.get("seq") if isinstance(doc.get("seq"), int) and not isinstance(doc.get("seq"), bool) else 0
    at = _number(doc.get("at"))
    st = PanelScreen(screen=HOME, seq=max(0, seq), at=at or 0.0, by=_by_from(doc.get("by")), settings=settings,
                     mute=_mute_from(doc.get("mute"), wall))
    screen = doc.get("screen")
    age = None if at is None else max(0.0, wall - at)
    kept = age is not None and (settings.idle_home_s == IDLE_NEVER or age <= settings.idle_home_s)
    if screen in PANEL_SCREENS and kept:
        return replace(st, screen=screen, active_at=now - age)
    if screen == HOME:
        return st
    return _changed(st, HOME, By("start"), wall)

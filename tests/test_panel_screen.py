import pytest

from kiln.live import panel_screen as ps
from kiln.live.panel_screen import By, Inputs

T0 = 1000.0  # monotonic seconds
WALL = 1_790_000_000.0
QUIET = ps.DEFAULT_SETTINGS.manual_quiet_s
IDLE = ps.DEFAULT_SETTINGS.idle_home_s
STABLE = ps.DEFAULT_SETTINGS.stable_s
DWELL = ps.DEFAULT_SETTINGS.min_dwell_s
ME = By("remote", "owner-laptop", 7, 12)
LONG_IDLE = 3600  # with_auto: so that the quiet time ends long before the idle return


def with_auto(state, **rules):
    patch = {"idle_home_s": LONG_IDLE, "auto": {"enabled": True, "rules": {name: {"on": True} for name in rules}}}
    return ps.person_settings(state, patch, By("touch"), T0 - QUIET - 1, WALL)


def started(inputs=Inputs(), state=None):
    """A state whose first step has seen the inputs (the first step never fires a rule)."""
    return ps.step(state or ps.PanelScreen(), inputs, T0, WALL)


def run(state, inputs, start, end, every=0.5):
    """Step once a tick from start to end; return the last state and each change's (source, screen)."""
    changes, t = [], start
    while t <= end + 1e-9:
        nxt = ps.step(state, inputs, t, WALL + t)
        if nxt.seq != state.seq and nxt.what == "screen":  # not a tab flag (test_panel_mute_flags.py)
            changes.append((nxt.by.source, nxt.screen))
        state, t = nxt, t + every
    return state, changes


def test_the_defaults_are_the_owners_choices():
    s = ps.settings_view(ps.DEFAULT_SETTINGS)
    assert s["idle_home_s"] == 300 and s["alarm_home"] is True and s["auto"]["enabled"] is False
    assert {n: r["on"] for n, r in s["auto"]["rules"].items()} == {
        "engine_switch": False, "night_start": False, "day_start": False}
    assert s["auto"]["rules"]["engine_switch"]["screen"] == "engine"
    assert s["auto"]["rules"]["night_start"]["screen"] == "fleet"


def test_a_person_set_counts_a_change_and_names_who_made_it():
    st = ps.person_set(ps.PanelScreen(), "engine", ME, T0, WALL)
    assert (st.screen, st.seq, st.at, st.by, st.active_at) == ("engine", 1, WALL, ME, T0)
    again = ps.person_set(st, "engine", ME, T0 + 1, WALL + 1)
    assert again.seq == 2  # the same screen still answers: the sender waits for its echo
    with pytest.raises(ValueError):
        ps.person_set(st, "radar", ME, T0, WALL)  # not a screen of this build


def test_idle_return_comes_after_idle_home_s_and_a_touch_restarts_the_timer():
    st = ps.person_set(started(), "engine", ME, T0, WALL)
    st, changes = run(st, Inputs(), T0, T0 + IDLE - 0.5)
    assert changes == []
    st = ps.person_touch(st, T0 + IDLE - 1)
    assert st.seq == 1  # a touch is not a change
    st, changes = run(st, Inputs(), T0 + IDLE, T0 + 2 * IDLE - 1.5)
    assert changes == []
    st, changes = run(st, Inputs(), T0 + 2 * IDLE - 1, T0 + 3 * IDLE)
    assert changes == [("idle", "glance")]  # exactly once


def test_the_alarm_jumps_home_at_once_and_ignores_the_quiet_time():
    st = ps.person_set(started(), "engine", ME, T0 + 1, WALL)
    st = ps.step(st, Inputs(failed=True), T0 + 2, WALL + 2)
    assert (st.screen, st.by.source, st.seq) == ("glance", "alarm", 2)
    st = ps.person_set(st, "fleet", ME, T0 + 3, WALL + 3)  # a touch can still page away
    st, changes = run(st, Inputs(failed=True), T0 + 3, T0 + 60)
    assert changes == [] and st.screen == "fleet"  # a steady alarm never fires again


def test_the_alarm_rule_can_be_turned_off():
    st = ps.person_settings(started(), {"alarm_home": False}, By("touch"), T0, WALL)
    st = ps.person_set(st, "engine", ME, T0, WALL)
    assert ps.step(st, Inputs(failed=True), T0 + 1, WALL).screen == "engine"


def test_the_first_step_only_learns_the_inputs():
    st = ps.person_set(ps.PanelScreen(), "engine", ME, T0, WALL)
    st = ps.step(st, Inputs(failed=True, switching=True, night=True), T0 + 1, WALL)
    assert st.screen == "engine" and st.seq == 1  # an alarm from before a restart was handled then


@pytest.mark.parametrize("edges, alarm", [
    ([89.9], False), ([90.0], True), ([90.0, 88.0], True), ([90.0, 88.0, 86.9], False),
    ([90.0, 86.9, 88.0], False), ([90.0, None], False)])
def test_the_hot_flag_sets_at_90_and_clears_below_87(edges, alarm):
    st = started()
    for i, edge in enumerate(edges):
        st = ps.step(st, Inputs(edge_c=edge), T0 + 1 + i, WALL)
    assert st.seen.alarm is alarm


def test_a_hot_gpu_comes_back_as_a_new_alarm_only_after_it_cooled():
    st = ps.person_set(started(), "engine", ME, T0, WALL)
    st = ps.step(st, Inputs(edge_c=91.0), T0 + 1, WALL)
    assert st.by.source == "alarm"
    st = ps.person_set(st, "engine", ME, T0 + 2, WALL)
    for i, edge in enumerate([88.0, 90.5, 87.5]):  # still hot: no new alarm
        st = ps.step(st, Inputs(edge_c=edge), T0 + 3 + i, WALL)
    assert st.screen == "engine"
    st = ps.step(st, Inputs(edge_c=80.0), T0 + 10, WALL)
    st = ps.step(st, Inputs(edge_c=90.0), T0 + 11, WALL)
    assert (st.screen, st.by.source) == ("glance", "alarm")


def test_an_opt_in_rule_fires_after_its_input_held_for_stable_s():
    st = with_auto(started(), engine_switch=True)
    st = ps.person_set(st, "today", ME, T0 - QUIET - 1, WALL)
    st, changes = run(st, Inputs(switching=True), T0, T0 + STABLE - 0.5)
    assert changes == []
    st, changes = run(st, Inputs(switching=True), T0 + STABLE, T0 + STABLE + 30)
    assert changes == [("auto:engine_switch", "engine")]


def test_an_input_that_flickers_shorter_than_stable_s_never_fires():
    st = with_auto(started(), engine_switch=True)
    st = ps.person_set(st, "today", ME, T0 - QUIET - 1, WALL)
    for i in range(20):
        st = ps.step(st, Inputs(switching=i % 4 == 0), T0 + i, WALL)
    assert st.screen == "today"


def test_rules_are_off_unless_auto_is_enabled():
    st = ps.person_settings(started(), {"idle_home_s": LONG_IDLE, "auto": {"rules": {"engine_switch": {"on": True}}}},
                            By("touch"),
                            T0 - QUIET - 1, WALL)
    st = ps.person_set(st, "today", ME, T0 - QUIET - 1, WALL)
    _, changes = run(st, Inputs(switching=True), T0, T0 + 10)
    assert changes == []


def test_the_manual_quiet_time_drops_an_opt_in_rule():
    st = with_auto(started(), engine_switch=True)
    st = ps.person_set(st, "today", ME, T0, WALL)
    st, changes = run(st, Inputs(switching=True), T0, T0 + QUIET + 10)
    assert changes == []  # dropped, not queued for after the quiet time


def test_the_dwell_time_blocks_a_second_auto_change():
    st = with_auto(started(Inputs(night=False)), engine_switch=True, day_start=True)
    st = ps.person_settings(st, {"auto": {"rules": {"engine_switch": {"screen": "settings"},
                                                    "day_start": {"screen": "today"}}}},
                            By("touch"), T0 - QUIET - 1, WALL)
    st, changes = run(st, Inputs(switching=True, night=False), T0, T0 + 5)
    assert changes == [("auto:engine_switch", "settings")]
    st, changes = run(st, Inputs(switching=True, night=True), T0 + 5.5, T0 + 10)
    st, changes = run(st, Inputs(switching=True, night=False), T0 + 10.5, T0 + DWELL - 1)
    assert changes == []


def test_night_start_holds_its_screen_and_night_makes_it_the_resting_screen():
    st = with_auto(started(Inputs(night=False)), night_start=True)
    st, changes = run(st, Inputs(night=True), T0, T0 + 10)
    assert changes == [("auto:night_start", "fleet")]
    assert ps.resting_screen(st) == "fleet"
    st = ps.person_set(st, "engine", ME, T0 + QUIET + 20, WALL)
    _, changes = run(st, Inputs(night=True), T0 + QUIET + 20, T0 + QUIET + 20 + LONG_IDLE + 1, every=10)
    assert changes == [("idle", "fleet")]


def test_the_resting_screen_is_home_during_an_alarm_and_by_default():
    st = started(Inputs(night=True))
    assert ps.resting_screen(st) == "glance"
    st = with_auto(st, night_start=True)
    assert ps.resting_screen(st) == "fleet"
    st = ps.step(st, Inputs(night=True, failed=True), T0 + 1, WALL)
    assert ps.resting_screen(st) == "glance"


def test_an_auto_rule_whose_screen_shows_already_changes_nothing():
    st = with_auto(started(), engine_switch=True)
    st = ps.person_settings(st, {"auto": {"rules": {"engine_switch": {"screen": "glance"}}}}, By("touch"),
                            T0 - QUIET - 1, WALL)
    st, changes = run(st, Inputs(switching=True), T0, T0 + 10)
    assert changes == [] and st.last_auto_at is None


def test_seq_always_goes_up():
    st, seqs = started(), []
    for i, scr in enumerate(["engine", "engine", "fleet", "today"]):
        st = ps.person_set(st, scr, ME, T0 + i, WALL)
        seqs.append(st.seq)
    st = ps.step(st, Inputs(failed=True), T0 + 9, WALL)
    seqs.append(st.seq)
    st = ps.person_settings(st, {"stable_s": 1}, ME, T0 + 10, WALL)
    seqs.append(st.seq)
    assert seqs == sorted(set(seqs)) and seqs[0] == 1


@pytest.mark.parametrize("patch", [
    {"idle_home_s": 5}, {"idle_home_s": "300"}, {"idle_home_s": True}, {"idle_home_s": float("nan")},
    {"stable_s": -1}, {"alarm_home": 1}, {"colour": "red"}, {"auto": {"rules": {"prefill": {"on": True}}}},
    {"auto": {"rules": {"night_start": {"screen": "gpu"}}}}, {"auto": {"enabled": "yes"}},
    {"auto": []}, [], {"auto": {"rules": {"day_start": {"hold_s": 10 ** 9}}}}])
def test_settings_are_checked_and_a_bad_patch_changes_nothing(patch):
    with pytest.raises(ValueError):
        ps.merge_settings(ps.DEFAULT_SETTINGS, patch)


def test_a_settings_patch_keeps_the_other_settings():
    s = ps.merge_settings(ps.DEFAULT_SETTINGS, {"auto": {"enabled": True, "rules": {"night_start": {"on": True}}}})
    v = ps.settings_view(s)
    assert v["auto"]["enabled"] and v["auto"]["rules"]["night_start"] == {"on": True, "screen": "fleet", "hold_s": 120}
    assert v["idle_home_s"] == 300 and v["auto"]["rules"]["day_start"]["on"] is False


def test_the_file_keeps_the_screen_unless_it_is_older_than_the_idle_time():
    st = ps.person_set(ps.PanelScreen(), "engine", ME, T0, WALL)
    doc = ps.to_file(st)
    back = ps.from_file(doc, WALL + 60, T0 + 500)
    assert (back.screen, back.seq, back.by) == ("engine", 1, ME)
    assert back.active_at == T0 + 440  # the idle time goes on from the change
    stale = ps.from_file(doc, WALL + IDLE + 1, T0 + 500)
    assert (stale.screen, stale.seq, stale.by.source) == ("glance", 2, "start")


@pytest.mark.parametrize("doc", [None, [], {"screen": 3}, {"screen": "live", "seq": 9, "at": WALL},
                                 {"screen": "engine", "seq": "x"}, {"screen": "engine", "seq": 3, "at": "now"}])
def test_a_broken_file_gives_home_and_keeps_a_good_seq(doc):
    st = ps.from_file(doc, WALL, T0)
    assert st.screen == "glance"
    if isinstance(doc, dict) and doc.get("seq") == 9:
        assert st.seq >= 9


def test_the_file_keeps_the_settings_and_a_bad_settings_block_gives_the_defaults():
    st = ps.person_settings(ps.PanelScreen(), {"idle_home_s": 600}, ME, T0, WALL)
    assert ps.from_file(ps.to_file(st), WALL, T0).settings.idle_home_s == 600
    doc = {**ps.to_file(st), "settings": {"idle_home_s": "soon"}}
    assert ps.from_file(doc, WALL, T0).settings == ps.DEFAULT_SETTINGS


def test_the_wire_view_carries_the_settings_only_when_asked():
    st = ps.person_set(ps.PanelScreen(), "engine", ME, T0, WALL)
    v = ps.view(st)
    assert v == {"screen": "engine", "seq": 1, "at": WALL,
                 "by": {"source": "remote", "who": "owner-laptop", "sid": 7, "req": 12},
                 "what": "screen", "mute": None, "flags": []}
    assert ps.view(st, settings=True)["settings"]["idle_home_s"] == 300


@pytest.mark.parametrize("old", ["live", "gpu"])
def test_a_retired_screen_is_refused_with_a_reason_that_says_to_reload(old):
    with pytest.raises(ValueError, match="reload"):
        ps.person_set(started(), old, ME, T0, WALL)


def test_a_file_on_a_retired_screen_starts_at_the_resting_screen():
    doc = {**ps.to_file(ps.person_set(ps.PanelScreen(), "fleet", ME, T0, WALL)), "screen": "gpu"}
    st = ps.from_file(doc, WALL + 1, T0)
    assert (st.screen, st.seq, st.by.source) == ("glance", 2, "start")


def test_a_v1_file_moves_engine_switch_to_the_engine_screen_and_keeps_the_other_settings():
    v1 = ps.to_file(ps.person_settings(ps.PanelScreen(), {"idle_home_s": 900, "auto": {"rules": {
        "engine_switch": {"screen": "glance"}, "day_start": {"screen": "today"}}}}, ME, T0, WALL))
    doc = {**v1, "v": 1}
    doc["settings"]["auto"]["rules"]["night_start"]["screen"] = "live"  # a retired id in a rule target
    s = ps.settings_view(ps.from_file(doc, WALL, T0).settings)
    assert s["idle_home_s"] == 900
    assert {n: r["screen"] for n, r in s["auto"]["rules"].items()} == {
        "engine_switch": "engine", "night_start": "glance", "day_start": "today"}
    v2 = ps.to_file(ps.from_file(doc, WALL, T0))
    assert v2["v"] == 2
    v2["settings"]["auto"]["rules"]["engine_switch"]["screen"] = "glance"  # a person's choice in a v2 file stays
    assert ps.from_file(v2, WALL, T0).settings.rule("engine_switch").screen == "glance"


def test_never_is_an_idle_time_of_0_and_the_screen_then_stays():
    st = ps.person_settings(started(), {"idle_home_s": ps.IDLE_NEVER}, By("touch"), T0, WALL)
    st = ps.person_set(st, "today", ME, T0, WALL)
    _, changes = run(st, Inputs(), T0, T0 + 7 * 86400, every=3600)
    assert changes == []
    back = ps.from_file(ps.to_file(st), WALL + 30 * 86400, T0)
    assert back.screen == "today"


@pytest.mark.parametrize("v", [1, 29.9, -1])
def test_an_idle_time_between_never_and_30_s_is_refused(v):
    with pytest.raises(ValueError):
        ps.merge_settings(ps.DEFAULT_SETTINGS, {"idle_home_s": v})

"""The rack panel's step 3 rules (live/panel_screen.py): the alarm mute, the tab flags and the night display."""
import pytest

from kiln.live import panel_screen as ps
from kiln.live.panel_screen import By, Inputs

T0 = 1000.0
WALL = 1_790_000_000.0
QUIET = ps.DEFAULT_SETTINGS.manual_quiet_s
STABLE = ps.DEFAULT_SETTINGS.stable_s
ME = By("remote", "owner-laptop", 7, 12)
HOT = Inputs(edge_c=91.0)
FAILED = Inputs(failed=True)


def started(inputs=Inputs(), state=None, t=T0):
    return ps.step(state or ps.PanelScreen(), inputs, t, WALL + (t - T0))


def at(state, inputs, t):
    return ps.step(state, inputs, t, WALL + (t - T0))


def hot_muted(screen="glance"):
    st = started(HOT, ps.PanelScreen(settings=ps.merge_settings(ps.DEFAULT_SETTINGS, {"idle_home_s": 0})))
    st = ps.person_set(st, screen, ME, T0 + 1, WALL + 1) if screen != "glance" else st
    return ps.person_mute(st, "hot", ME, T0 + 2, WALL + 2)


# -- night display -----------------------------------------------------------------------------------
def test_night_display_is_normal_by_default_and_takes_dim():
    assert ps.settings_view(ps.DEFAULT_SETTINGS)["night_display"] == "normal"
    st = ps.person_settings(ps.PanelScreen(), {"night_display": "dim"}, ME, T0, WALL)
    assert st.settings.night_display == "dim" and ps.view(st, settings=True)["settings"]["night_display"] == "dim"


@pytest.mark.parametrize("bad", ["off", "", None, 1, True])
def test_night_display_off_and_other_values_are_refused(bad):
    with pytest.raises(ValueError, match="night_display"):
        ps.merge_settings(ps.DEFAULT_SETTINGS, {"night_display": bad})


def test_an_older_file_without_night_display_loads_with_normal():
    doc = ps.to_file(ps.PanelScreen())
    del doc["settings"]["night_display"]
    assert ps.from_file(doc, WALL, T0).settings.night_display == "normal"


# -- the alarm mute --------------------------------------------------------------------------------------
def test_a_mute_of_a_hot_gpu_is_shared_for_30_minutes_and_named_on_the_wire():
    st = hot_muted()
    assert st.mute == ps.Mute("hot", WALL + 2 + 1800)
    assert (st.by, st.what) == (ME, "mute")
    assert ps.view(st)["mute"] == {"kind": "hot", "until": WALL + 2 + 1800}


@pytest.mark.parametrize("kind, msg", [("nodata", "NO DATA"), ("cold", "unknown alarm"), (None, "nothing")])
def test_no_data_an_unknown_kind_and_an_unmute_without_a_mute_are_refused(kind, msg):
    with pytest.raises(ValueError, match=msg):
        ps.person_mute(started(HOT), kind, ME, T0, WALL)


def test_only_an_alarm_that_holds_now_can_be_muted():
    with pytest.raises(ValueError, match="no such alarm"):
        ps.person_mute(started(HOT), "failed", ME, T0, WALL)
    with pytest.raises(ValueError, match="no such alarm"):
        ps.person_mute(ps.PanelScreen(), "hot", ME, T0, WALL)  # before the first tick: nothing is known


def test_during_a_mute_the_same_alarm_kind_does_not_jump_home():
    st = hot_muted(screen="fleet")
    st = at(st, Inputs(edge_c=86.0), T0 + 10)  # it cooled: the mute ends with its alarm
    assert st.mute is None and st.what == "mute" and st.screen == "fleet"
    st = hot_muted(screen="fleet")
    st = at(st, HOT, T0 + 10)
    assert (st.screen, st.mute.kind) == ("fleet", "hot")


def test_a_new_alarm_kind_still_takes_the_screen_during_a_mute():
    st = hot_muted(screen="fleet")
    st = at(st, Inputs(failed=True, edge_c=91.0), T0 + 10)
    assert (st.screen, st.by.source) == ("glance", "alarm")
    assert st.mute.kind == "hot"  # the hot mute still holds: the hot gauge stays red, not the full screen


def test_the_mute_ends_at_its_time_and_a_hot_gpu_that_still_holds_jumps_home_again():
    st = hot_muted(screen="fleet")
    st = at(st, HOT, T0 + 2 + 1799)
    assert st.mute is not None and st.screen == "fleet"
    st = at(st, HOT, T0 + 2 + 1800)
    assert st.mute is None and (st.screen, st.by.source) == ("glance", "alarm")


def test_a_mute_end_keeps_who_set_the_screen():
    st = hot_muted(screen="fleet")
    before = (st.by, st.at)
    st = at(st, Inputs(edge_c=80.0), T0 + 10)
    assert st.mute is None and (st.by, st.at) == before and st.seq == 3


def test_a_mute_of_another_kind_replaces_the_first_and_unmute_ends_it():
    st = started(Inputs(failed=True, edge_c=91.0))
    st = ps.person_mute(st, "hot", ME, T0 + 1, WALL + 1)
    st = ps.person_mute(st, "failed", ME, T0 + 2, WALL + 2)
    assert st.mute.kind == "failed"
    st = ps.person_mute(st, None, ME, T0 + 3, WALL + 3)
    assert st.mute is None and st.what == "mute" and st.seq == 3


def test_the_file_keeps_a_mute_until_its_time():
    st = hot_muted()
    doc = ps.to_file(st)
    assert ps.from_file(doc, WALL + 60, T0).mute == st.mute
    assert ps.from_file(doc, WALL + 2 + 1800, T0).mute is None
    for bad in ({"kind": "nodata", "until": WALL + 99}, {"kind": "hot"}, "hot", {"kind": "hot", "until": True}):
        assert ps.from_file({**doc, "mute": bad}, WALL, T0).mute is None


def test_a_mute_kept_over_a_restart_ends_when_its_alarm_is_gone():
    st = ps.from_file(ps.to_file(hot_muted()), WALL + 60, T0 + 60)
    st = at(st, Inputs(edge_c=60.0), T0 + 61)
    assert st.mute is None


# -- tab flags ---------------------------------------------------------------------------------------
def held(state, inputs, start):
    """Step through stable_s so a rule's input change counts."""
    t = start
    while t <= start + STABLE + 1e-9:
        state = at(state, inputs, t)
        t += 0.5
    return state


def test_with_auto_off_a_switch_flags_the_engine_tab_and_does_not_move():
    st = held(started(), Inputs(switching=True), T0 + 1)
    assert st.screen == "glance"
    (flag,) = st.flags
    assert (flag.screen, flag.tone, flag.cause) == ("engine", "warn", "off")
    assert flag.why == "Engine switch: see Engine"
    assert st.what == "flags" and st.by.source == "start"  # a flag is no change of screen
    assert ps.view(st)["flags"] == [{"screen": "engine", "tone": "warn", "why": "Engine switch: see Engine",
                                     "cause": "off", "quiet_until": None}]


def test_a_flag_clears_when_its_state_ends():
    st = held(started(), Inputs(switching=True), T0 + 1)
    st = held(st, Inputs(switching=False), T0 + 10)
    assert st.flags == () and st.what == "flags"


def test_a_flag_clears_when_its_screen_opens():
    st = held(started(Inputs(night=False)), Inputs(night=True), T0 + 1)
    assert [f.screen for f in st.flags] == ["fleet"]
    st = ps.person_set(st, "fleet", ME, T0 + 20, WALL + 20)
    assert st.flags == ()


def test_in_the_manual_quiet_time_a_rule_flags_its_tab_with_the_quiet_end():
    st = ps.person_settings(started(), {"auto": {"enabled": True, "rules": {"engine_switch": {"on": True}}},
                                        "idle_home_s": 3600}, ME, T0, WALL)
    st = ps.person_set(st, "today", ME, T0 + 1, WALL + 1)
    st = held(st, Inputs(switching=True), T0 + 2)
    assert st.screen == "today"
    (flag,) = st.flags
    assert flag.cause == "quiet" and flag.quiet_until == pytest.approx(WALL + 1 + QUIET)


def test_a_rule_that_auto_runs_sets_no_flag_and_a_rule_turned_off_with_auto_on_neither():
    st = ps.person_settings(started(), {"auto": {"enabled": True, "rules": {"engine_switch": {"on": True}}},
                                        "idle_home_s": 3600}, ME, T0 - QUIET - 1, WALL)
    st = held(st, Inputs(switching=True), T0 + 1)
    assert st.screen == "engine" and st.flags == ()
    st = ps.person_settings(started(), {"auto": {"enabled": True}, "idle_home_s": 3600}, ME, T0 - QUIET - 1, WALL)
    st = held(st, Inputs(switching=True), T0 + 1)
    assert st.flags == () and st.screen == "glance"


def test_no_flag_for_the_screen_that_shows():
    st = ps.person_set(started(), "engine", ME, T0, WALL)
    st = held(st, Inputs(switching=True), T0 + 1)
    assert st.flags == ()


def test_a_failed_plan_item_flags_fleet_red_until_fleet_opens_or_the_plan_has_no_failure():
    st = started(Inputs(plan_failed=0))
    st = at(st, Inputs(plan_failed=1, plan_failed_label="Short test"), T0 + 1)
    (flag,) = st.flags
    assert (flag.screen, flag.tone, flag.why, flag.cause) == ("fleet", "bad", "Plan item failed: Short test", "fault")
    assert at(st, Inputs(plan_failed=0), T0 + 2).flags == ()
    assert at(st, Inputs(plan_failed=None), T0 + 2).flags == (flag,)  # no fleet data: the flag stays
    assert ps.person_set(st, "fleet", ME, T0 + 2, WALL + 2).flags == ()


def test_red_beats_yellow_on_one_tab():
    st = held(started(Inputs(night=False, plan_failed=0)), Inputs(night=True, plan_failed=0), T0 + 1)
    st = at(st, Inputs(night=True, plan_failed=1, plan_failed_label="Last check"), T0 + 10)
    assert [(f.screen, f.tone) for f in st.flags] == [("fleet", "bad")]
    st = at(st, Inputs(night=True, plan_failed=0), T0 + 11)
    assert st.flags == ()  # the plan's flag took the night's place; it ends with the failure


def test_flags_are_not_kept_in_the_file():
    st = held(started(), Inputs(switching=True), T0 + 1)
    assert ps.from_file(ps.to_file(st), WALL, T0).flags == ()

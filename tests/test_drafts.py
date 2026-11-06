from kiln.live.drafts import TURNS, ConvDrafts


def test_a_conversation_shows_the_sum_of_its_last_turns():
    d = ConvDrafts()
    for i in range(TURNS + 2):
        d.note("c1", f"r{i}", 100.0 + i, 100, 50 + i)
    kept = range(2, TURNS + 2)  # the two oldest turns fell out
    assert d.view("c1") == {"accepted": sum(50 + i for i in kept), "proposed": 100 * TURNS, "turns": TURNS}


def test_a_turn_noted_again_replaces_itself_and_a_turn_without_drafts_counts_nothing():
    d = ConvDrafts()
    d.note("c1", "r1", 100.0, None, None)  # the gateway record, before the engine line joins it
    assert d.view("c1") is None
    d.note("c1", "r1", 100.0, 200, 150)
    d.note("c1", "r1", 100.0, 200, 160)  # the same request again (a later join)
    d.note("c1", "r0", 90.0, 0, 0)  # a turn without speculation
    assert d.view("c1") == {"accepted": 160, "proposed": 200, "turns": 1}
    assert d.view(None) is None and d.view("other") is None


def test_an_older_turn_joined_late_does_not_push_out_a_newer_one():
    d = ConvDrafts()
    for i in range(TURNS):
        d.note("c", f"r{i}", 200.0 + i, 10, 5)
    d.note("c", "old", 100.0, 1000, 0)
    assert d.view("c") == {"accepted": 5 * TURNS, "proposed": 10 * TURNS, "turns": TURNS}


def test_bad_counts_are_ignored():
    d = ConvDrafts()
    d.note("c", "r1", 1.0, 10, 11)  # more accepted than proposed: not a count Kiln can trust
    d.note("c", "r2", 2.0, -5, 0)
    d.note("c", "r3", 3.0, True, 1)
    assert d.view("c") is None


def test_only_the_most_recently_noted_conversations_are_kept():
    d = ConvDrafts(keep=2)
    for c in ("a", "b", "c"):
        d.note(c, c, 1.0, 10, 5)
    assert d.view("a") is None and d.view("b") and d.view("c")
    d.note("b", "b2", 2.0, 10, 5)  # b is recent again, so the next new one pushes out c
    d.note("e", "e", 3.0, 10, 5)
    assert d.view("c") is None and d.view("b")["turns"] == 2

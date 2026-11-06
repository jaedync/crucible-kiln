from kiln.store import queries
from kiln.store.db import connect

NOW = 1_791_400_000.0
P = queries.PALETTE


def setup(colors: dict, busy: dict, ts=NOW - 3600):
    conn = connect(":memory:")
    for key, color in colors.items():
        conn.execute("INSERT INTO clients (key, name, color, updated) VALUES (?, ?, ?, 0)", (key, key, color))
    for i, (key, secs) in enumerate(busy.items()):
        conn.execute("INSERT INTO requests (id, source, host, ts_start, ts_end, client_key) VALUES (?, 'gateway',"
                     " 'strix', ?, ?, ?)", (f"r{i}", ts, ts + secs, key))
    return conn


def colors(conn):
    return {r[0]: r[1] for r in conn.execute("SELECT key, color FROM clients")}


def test_busy_clients_that_share_a_colour_are_split_busiest_keeps_it():
    conn = setup({"mac": P[2], "pc": P[2], "tablet": P[0]}, {"pc": 900, "mac": 800, "tablet": 100})
    assert queries.rebalance_colors(conn, NOW) == 1
    c = colors(conn)
    assert c["pc"] == P[2] and c["tablet"] == P[0] and c["mac"] == P[1]  # first colour no other busy client holds


def test_distinct_colours_do_not_move():
    conn = setup({"a": P[5], "b": P[3]}, {"a": 10, "b": 20})
    assert queries.rebalance_colors(conn, NOW) == 0 and colors(conn) == {"a": P[5], "b": P[3]}


def test_idle_clients_keep_their_colour_and_free_nothing():
    conn = setup({"old": P[0], "new": P[0]}, {"new": 50})
    conn.execute("UPDATE requests SET ts_start = ?, ts_end = ? WHERE client_key = 'new'", (NOW - 60, NOW))
    conn.execute("INSERT INTO requests (id, source, host, ts_start, ts_end, client_key) VALUES ('x', 'gateway', 'strix',"
                 " ?, ?, 'old')", (NOW - 40 * 86400, NOW - 40 * 86400 + 99))
    assert queries.rebalance_colors(conn, NOW) == 0 and colors(conn) == {"old": P[0], "new": P[0]}

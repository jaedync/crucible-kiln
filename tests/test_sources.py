"""The journal follower delivers a line soon after it arrives, not after the journal goes quiet."""
import sys

from kiln.collect.sources import JournalFollower

# Two engine lines 0.15 s apart, as Crucible logs a progress report and the next line of its prefill.
FAKE_JOURNAL = """
import json, sys, time
def line(n):
    print(json.dumps({"CONTAINER_NAME": "eng", "MESSAGE": f"line {n}", "__REALTIME_TIMESTAMP": "1000000000",
                      "__CURSOR": f"c{n}"}), flush=True)
line(1); time.sleep(0.15); line(2); time.sleep(0.3)
"""


class FakeJournal(JournalFollower):
    def _cmd(self):
        return [sys.executable, "-I", "-c", FAKE_JOURNAL]


async def test_a_line_is_delivered_without_waiting_for_the_journal_to_go_quiet():
    batches = []

    async def on_batch(entries, cursor):
        batches.append(([e[1] for e in entries], cursor))

    j = FakeJournal(1000, on_batch, None)
    assert await j._run_once() == 2
    # Both lines in one batch would mean the first waited for the second.
    assert [b for b in batches if b[0]] == [(["line 1"], "c1"), (["line 2"], "c2")]


QUIET_JOURNAL = "import time; time.sleep(0.3)"


class QuietJournal(JournalFollower):
    def _cmd(self):
        return [sys.executable, "-I", "-c", QUIET_JOURNAL]


async def test_a_quiet_journal_after_a_restart_delivers_nothing():
    # A saved cursor and no new line: nothing to deliver, and no empty batch (a database transaction)
    # every time the read times out.
    calls = []

    async def on_batch(entries, cursor):
        calls.append((entries, cursor))

    assert await QuietJournal(1000, on_batch, "c0")._run_once() == 0
    assert calls == []


BUSY_JOURNAL = """
import json, time
for n in range(15):
    print(json.dumps({"CONTAINER_NAME": "eng", "MESSAGE": f"line {n}", "__REALTIME_TIMESTAMP": "1000000000",
                      "__CURSOR": f"c{n}"}), flush=True)
    time.sleep(0.03)
"""


class BusyJournal(JournalFollower):
    def _cmd(self):
        return [sys.executable, "-I", "-c", BUSY_JOURNAL]


async def test_a_busy_journal_still_delivers_every_tenth_of_a_second():
    # Lines 30 ms apart never leave the journal quiet for 50 ms: without an age limit the first line
    # would wait for all of them.
    batches = []

    async def on_batch(entries, cursor):
        batches.append([e[1] for e in entries])

    await BusyJournal(1000, on_batch, None)._run_once()
    assert len([b for b in batches if b]) >= 3
    assert [x for b in batches for x in b] == [f"line {n}" for n in range(15)]

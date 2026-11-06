"""Draft acceptance of each live conversation, from its last finished turns.

An engine reports a turn's draft counts only when the turn ends (Crucible's crucible_turn line, Gufo's
completion line), so a live stream shows what its conversation's recent turns accepted. An agent session's
turns are short and come back to back, so the figure is seconds old. The gateway gives each stream its
conversation's random id at the start (gateway/prefix.py); the record of each finished turn carries the same id.
"""
from __future__ import annotations

from collections import OrderedDict

# In a measured run of 6,391 turns, the sum over a conversation's last 5 turns was within 5 points of its next
# turn's rate at the median and within 9 at the 75th percentile.
TURNS = 5
# The gateway remembers 16 conversations for each client and forgets them after 6 h; this is far more than
# can be live at once.
KEEP = 256


def _count(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


class ConvDrafts:
    def __init__(self, keep: int = KEEP):
        self.keep = keep
        # conv -> its last turns as (start, request id, proposed, accepted), oldest first
        self.convs: OrderedDict[str, tuple[tuple[float, str, int, int], ...]] = OrderedDict()

    def note(self, conv: str | None, rid: str, ts: float, proposed, accepted) -> None:
        """One finished turn's counts. A request noted again (its engine line joined later) replaces itself."""
        if not conv or not _count(proposed) or not _count(accepted) or proposed == 0 or accepted > proposed:
            return
        others = tuple(t for t in self.convs.get(conv, ()) if t[1] != rid)
        self.convs[conv] = tuple(sorted(others + ((ts, rid, proposed, accepted),)))[-TURNS:]
        self.convs.move_to_end(conv)
        while len(self.convs) > self.keep:
            self.convs.popitem(last=False)

    def view(self, conv: str | None) -> dict | None:
        """{accepted, proposed, turns} over the conversation's last turns, or None before its first one ends."""
        turns = self.convs.get(conv) if conv else None
        if not turns:
            return None
        return {"accepted": sum(t[3] for t in turns), "proposed": sum(t[2] for t in turns), "turns": len(turns)}

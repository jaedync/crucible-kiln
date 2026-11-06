"""Write an Xcursor theme whose cursors are one transparent pixel (the rack panel shows no cursor).

    python3 deploy/panel/blank_cursors.py ~/.local/share/kiln-panel/cursors

The theme is named `default`: cage asks wlroots for the default theme, and Chromium reads XCURSOR_THEME.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

IMAGE = 0xFFFD0002
SIZES = (24, 48)  # XCURSOR_SIZE and its 2x scale
# Every name cage or Chromium might ask for; the page itself sets `cursor: none`.
NAMES = ("default", "left_ptr", "pointer", "hand1", "hand2", "text", "xterm", "wait", "watch", "progress",
         "left_ptr_watch", "not-allowed", "crossed_circle", "grab", "grabbing", "crosshair", "move")


def blank_cursor() -> bytes:
    """One Xcursor file: a header, a table of contents and a 1 x 1 transparent image per size."""
    header = struct.pack("<4sIII", b"Xcur", 16, 0x10000, len(SIZES))
    chunk_len = 36 + 4
    toc_end = 16 + 12 * len(SIZES)
    toc = b"".join(struct.pack("<III", IMAGE, size, toc_end + i * chunk_len) for i, size in enumerate(SIZES))
    chunks = b"".join(struct.pack("<IIIIIIIIII", 36, IMAGE, size, 1, 1, 1, 0, 0, 0, 0) for size in SIZES)
    return header + toc + chunks


def write_theme(root: Path) -> Path:
    cursors = root / "default" / "cursors"
    cursors.mkdir(parents=True, exist_ok=True)
    data = blank_cursor()
    for name in NAMES:
        (cursors / name).write_bytes(data)
    (root / "default" / "index.theme").write_text("[Icon Theme]\nName=Kiln blank\nComment=No visible cursor\n")
    return cursors


if __name__ == "__main__":
    print(write_theme(Path(sys.argv[1]).expanduser()))

"""Values that Kiln writes into systemd unit files (security review L1).

systemd reads `%` as a specifier everywhere, `$` as a variable in Exec lines, whitespace as an argument
separator in Exec lines, and a trailing backslash as a line continuation. A path or a label that Kiln writes
must mean exactly what it says.
"""
from __future__ import annotations

import re

_SIMPLE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")


class UnitValueError(ValueError):
    pass


def literal(s: str) -> str:
    """A free text value (Description=): no specifiers. It must be one line without a trailing backslash."""
    _check(s)
    return s.replace("%", "%%")


def exec_arg(s: str) -> str:
    """One argument of an Exec line, as systemd reads it back unchanged."""
    _check(s)
    s = s.replace("%", "%%").replace("$", "$$")
    if _SIMPLE.match(s):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def exec_line(args: list[str]) -> str:
    return " ".join(exec_arg(a) for a in args)


def _check(s: str) -> None:
    if any(ord(c) < 32 or c == "\x7f" for c in s):
        raise UnitValueError(f"{s!r}: a unit value must not hold a line break or a control character")
    if s.endswith("\\"):
        raise UnitValueError(f"{s!r}: a trailing backslash would join the next line of the unit")

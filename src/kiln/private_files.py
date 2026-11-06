"""Directories and files for request metadata (IPs, device names, user agents): owner only (review L6).

A home directory at 0755 is common, and the default umask makes new files 0644, so any local account could
read the records and the database. Kiln creates them private; it never loosens what the owner set.
"""
from __future__ import annotations

import os
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600


def private_dir(path: Path) -> Path:
    """mkdir -p where every directory that Kiln creates gets DIR_MODE (existing ones keep their mode)."""
    path = Path(path)
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        if p.parent == p:
            break
        p = p.parent
    for d in reversed(missing):
        try:
            d.mkdir(mode=DIR_MODE)
        except FileExistsError:
            continue
        os.chmod(d, DIR_MODE)  # mkdir's mode is masked by the umask
    return path


def private_file(path: Path) -> Path:
    """Create the file empty at FILE_MODE if it does not exist yet."""
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE))
    except FileExistsError:
        pass
    return path

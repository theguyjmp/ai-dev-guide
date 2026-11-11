"""Process priority for the harness's children (Windows; a no-op elsewhere).

Builders, reviewers, fix agents and probes, with every check they run, start at BELOW_NORMAL: a child of a
BELOW_NORMAL process inherits that class, so cargo and pytest under a builder stay below it too. land.py, the
pre-review gate and the periodic gates run at NORMAL, and land.py's acceptance runs at ABOVE_NORMAL, so a timing test
that passes alone in 2.7 s is not starved into a false regression by 16 builders (pilot S123, 2026-11-09).
"""
from __future__ import annotations

import os
import subprocess

BELOW_NORMAL = "below_normal"
NORMAL = "normal"
ABOVE_NORMAL = "above_normal"

_CLASS = {  # the Win32 priority-class creation flags (subprocess exports them on Windows only)
    BELOW_NORMAL: getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000),
    NORMAL: getattr(subprocess, "NORMAL_PRIORITY_CLASS", 0x00000020),
    ABOVE_NORMAL: getattr(subprocess, "ABOVE_NORMAL_PRIORITY_CLASS", 0x00008000),
}


def flag(level: str, nt: bool | None = None) -> int:
    """The creationflags bit for level on Windows; 0 elsewhere."""
    return _CLASS[level] if (os.name == "nt" if nt is None else nt) else 0


def popen_kwargs(level: str, nt: bool | None = None) -> dict:
    """{"creationflags": <class>} for subprocess.run / Popen on Windows; {} elsewhere."""
    f = flag(level, nt)
    return {"creationflags": f} if f else {}


def set_self(level: str) -> bool:
    """Put the calling process in level's priority class (ctypes SetPriorityClass). False off Windows or on error."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        return bool(k32.SetPriorityClass(k32.GetCurrentProcess(), _CLASS[level]))
    except (AttributeError, OSError):
        return False

"""memcap.py: a per-process memory cap for everything drive.py starts ([limits] proc_mem_gb, default 12).

On Windows, cap_self puts the calling process in a Job Object with JOB_OBJECT_LIMIT_PROCESS_MEMORY. Every child it
starts later (builders, polish and fix agents, land.py and its acceptance runs, check commands, periodic gates, and
their cargo, rustc and test binaries) is in the same job, so any one process that passes the cap fails its next
allocation instead of starving the machine. The job does not kill anything when drive exits. Elsewhere it does
nothing. ctypes only.

cap_self also sets OVERHAUL_PROC_MEM_GB so children (land.py) can name the cap: note() turns an allocation failure in
a command's output into "process memory cap 12 GB hit: <tail>".
"""
import os
import re
import sys

ENV = "OVERHAUL_PROC_MEM_GB"
OOM = re.compile(r"memory allocation of \d+ bytes failed|out of memory|std::bad_alloc|\bMemoryError\b|"
                 r"Cannot allocate memory|failed to allocate|STATUS_NO_MEMORY|0xc0000017|"
                 r"paging file is too small|insufficient (?:system )?memory", re.I)
_job = None  # the job handle; kept open for the life of the process


def cap_self(gb: float) -> str:
    """Cap every process of this tree at gb GB. Returns what it did ('' when off or not Windows)."""
    global _job
    if not gb or float(gb) <= 0 or os.name != "nt":
        return ""
    if _job is not None:
        return f"process memory cap {gb:g} GB (already set)"
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Io(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes",
                                                   "OtherBytes")]

    class Extended(ctypes.Structure):
        _fields_ = [("Basic", Basic), ("Io", Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return f"process memory cap not set: CreateJobObject error {ctypes.get_last_error()}"
    info = Extended()
    info.Basic.LimitFlags = 0x100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
    info.ProcessMemoryLimit = int(float(gb) * 1024 ** 3)
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # extended limits
        return f"process memory cap not set: SetInformationJobObject error {ctypes.get_last_error()}"
    if not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
        return f"process memory cap not set: AssignProcessToJobObject error {ctypes.get_last_error()}"
    _job = job
    os.environ[ENV] = f"{float(gb):g}"
    return f"process memory cap {float(gb):g} GB per process"


def note(text: str, reason: str) -> str:
    """reason, prefixed with the cap when the cap is on and text shows a failed allocation."""
    cap = os.environ.get(ENV, "")
    if not cap or not OOM.search(text or ""):
        return reason
    head = f"process memory cap {cap} GB hit"
    return reason if reason.startswith(head) else f"{head}: {reason}" if reason else head


if __name__ == "__main__":  # py -3 memcap.py GB -- cmd...: run cmd under the cap (manual check)
    import subprocess
    print(cap_self(float(sys.argv[1])), flush=True)
    sys.exit(subprocess.call(sys.argv[3:]))

"""#99: raise competing load, and be certain it is gone afterwards.

#96 needed a loaded machine to prove a fix. The agent wrote a `burn.py` into its session
scratchpad, raised 112 processes in two waves, and the session ended while the test was
still running. Nothing tore them down. They were found 73 minutes later by the next
session, having cost 22 test failures that were not failures -- the same two suites ran
740.32 s / 12 failed and 751.04 s / 10 failed under the load, then 78.09 s / 0 failed and
97.05 s / 1 failed once it was killed, a 9.5x slowdown -- and about 16% of #79's
measurement window (`measured` 2026-09-12, all of it).

**The defect was not the load test. It was that raising the load and lowering it were two
separate acts and only the first was in a script.** This module is the second act, in the
same script as the first.

## What guarantees the tear-down, and what each layer does not cover

A Python parent that is force-killed does not run its `finally`. `TerminateProcess` --
which is what `Stop-Process -Force`, `taskkill /F` and a harness reaping its agent all
do -- gives the process no chance to clean up. So `finally` alone is not a mechanism, it
is a hope. Three independent layers sit under this instead:

1. **A Windows job object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.** This process joins
   the job at startup, so every process it spawns joins automatically at creation -- there
   is no window between `CreateProcess` and the assignment for a child to escape through.
   The only handle to the job is this process's, so when this process dies by any means
   the kernel closes that handle, the job's last handle goes, and every member is killed
   by the OS. This is the layer that covers force-kill.
   *Does not cover:* a Windows before 8 (no nested jobs) or a parent already inside a job
   that forbids breakaway -- in either case `SELF` assignment fails and layer 1 degrades
   to per-child assignment, which does have the spawn race. It does not survive the job
   object failing to be created at all. Both cases are reported, never assumed away.

2. **A bounded lifetime in each child.** Every child is given an absolute wall-clock
   deadline at spawn and exits on its own when it passes, whatever happened to its parent.
   `MAX_LIFETIME_SECONDS` caps it however long was asked for.
   *Does not cover:* promptness. A child whose parent died still burns until its deadline.
   This layer does not prevent contamination, it bounds it -- which is the whole argument
   for it: 73 minutes of unbounded orphan is a corrupted measurement nobody can date, and
   30 minutes of bounded orphan is a delay someone can wait out. It fails safe without
   needing the OS to cooperate, so it is the floor under the other two, not a substitute.

3. **An orphan watch in each child.** Each child opens a handle to its parent at startup
   and exits within `ORPHAN_POLL_SECONDS` of that handle signalling. The handle pins the
   parent's identity, so a recycled PID cannot make an orphan think its parent is alive.
   *Does not cover:* a parent that is alive but wedged. That is what layer 2 is for.

**What none of them covers:** a host that loses power mid-run, and a child that is itself
force-killed between `CreateProcess` and its first instruction. Neither leaves load
behind, so neither is this ticket's failure.

## Tear-down is verified, not asserted

`D:\\Convex Hedge\\i13-day\\stop-at-2130.ps1` is the same defect in another shape. It fired
at 13:30:10Z, logged `stopped PID 19236 (cmd)` and `done` -- and PID 19236 was a `cmd`
wrapper. Killing it did not kill the Python collector underneath, which kept sampling for
another 73 seconds and wrote 22 rows after the run had been declared complete
(`measured` 2026-09-12). **The script reported a clean stop that had not happened.**

So this module never prints `reaped` because it called `terminate()`. It re-reads the host
process table afterwards and counts what is actually gone, and `run` exits non-zero when
the count it raised and the count it reaped disagree.

## Use

    python tools/loadgen.py check                      # pre-flight, before any measurement
    python tools/loadgen.py run --count 24 --seconds 60

`check` is the one command that would have caught #96's orphans. Run it before a gate, a
benchmark or any number you intend to publish.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time

WINDOWS = sys.platform == "win32"

#: No child outlives this, however long `--seconds` asks for. Layer 2's hard ceiling: the
#: #96 orphans ran 73 minutes and were still running when found, so the ceiling is set
#: below the point at which an orphan stops being an inconvenience and becomes a lost
#: measurement window. `assumed`.
MAX_LIFETIME_SECONDS = 1800.0

#: How often a child checks its deadline and its parent. Small enough that an orphan is
#: gone before a human notices it, large enough not to be load of its own.
ORPHAN_POLL_SECONDS = 0.25

#: How long `run` waits for a terminated child to actually leave the process table before
#: it reports the child as survived.
REAP_TIMEOUT_SECONDS = 15.0

DEFAULT_TAG = "dxp-loadgen"

#: A process is a suspected load generator when its command line contains **every** term
#: in any one group. Groups rather than one substring because Windows quotes the script
#: path -- `"...\\tools\\loadgen.py" child` has a quote where a substring match wants a
#: space. `burn.py` is #96's own generator, which is what this pre-flight exists to find.
SUSPECT_GROUPS: tuple[tuple[str, ...], ...] = (
    ("loadgen.py", "child"),
    ("burn.py",),
)

def disabled(layer: str) -> bool:
    """Whether a tear-down layer has been switched off by the environment.

    **These exist for one test and no other caller.** #99's kill-the-parent test has to be
    able to remove a layer and watch the leak come back, because a test that cannot fail
    is the failure mode this repository keeps finding -- six confirmed instances this week.
    `LOADGEN_DISABLE_JOB=1` removes layer 1 and `LOADGEN_DISABLE_ORPHAN_WATCH=1` removes
    layer 3. `run` prints which layers are live, so a run that had one switched off cannot
    be read afterwards as a run that did not.
    """
    return os.environ.get(f"LOADGEN_DISABLE_{layer}") == "1"


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x102


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32():
    """`kernel32` with every signature this module uses declared.

    The signatures are not decoration. `GetCurrentProcess` returns the pseudo-handle
    `-1`, and with `argtypes` left unset ctypes tries to pass it as a C `int` and raises
    `OverflowError: int too long to convert` on a 64-bit build -- which is how the job
    object silently became unavailable the first time this was run.
    """
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = ctypes.c_void_p
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    k32.CreateJobObjectW.restype = handle
    k32.SetInformationJobObject.argtypes = [
        handle,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    k32.SetInformationJobObject.restype = ctypes.c_int
    k32.AssignProcessToJobObject.argtypes = [handle, handle]
    k32.AssignProcessToJobObject.restype = ctypes.c_int
    k32.IsProcessInJob.argtypes = [handle, handle, ctypes.POINTER(ctypes.c_int)]
    k32.IsProcessInJob.restype = ctypes.c_int
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.OpenProcess.restype = handle
    k32.GetCurrentProcess.argtypes = []
    k32.GetCurrentProcess.restype = handle
    k32.WaitForSingleObject.argtypes = [handle, ctypes.c_uint32]
    k32.WaitForSingleObject.restype = ctypes.c_uint32
    k32.CloseHandle.argtypes = [handle]
    k32.CloseHandle.restype = ctypes.c_int
    return k32


# --------------------------------------------------------------------------- layer 1


def open_kill_on_close_job() -> tuple[int | None, str]:
    """Create the job every child will belong to, and put this process inside it.

    Returns the job handle and the mode actually achieved, which is one of:

    ``self``
        This process is a member, so every process it spawns joins at creation. There is
        no spawn race.
    ``per-child``
        The self-assignment failed -- an outer job forbidding breakaway is the usual
        reason -- so each child must be assigned after `CreateProcess` returns. A child
        spawned in the instant before this process dies can escape; layers 2 and 3 are
        what bound that child.
    ``unavailable``
        Not Windows, or the job object could not be created at all. Layer 1 is absent and
        the caller is told so rather than being allowed to believe otherwise.
    """
    if not WINDOWS or disabled("JOB"):
        return None, "unavailable"
    k32 = _kernel32()
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None, "unavailable"
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = k32.SetInformationJobObject(
        ctypes.c_void_p(job),
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    )
    if not ok:
        k32.CloseHandle(ctypes.c_void_p(job))
        return None, "unavailable"
    if k32.AssignProcessToJobObject(ctypes.c_void_p(job), k32.GetCurrentProcess()):
        return job, "self"
    return job, "per-child"


def assign_to_job(job: int, pid: int) -> bool:
    """Put one already-running process into the job. Used only in ``per-child`` mode."""
    if not WINDOWS:
        return False
    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(k32.AssignProcessToJobObject(ctypes.c_void_p(job), handle))
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))


def in_job(job: int, pid: int) -> bool | None:
    """Whether `pid` is a member of `job`. `None` when the question cannot be asked.

    This is the check that makes layer 1 evidence rather than intention. `run` calls it on
    every child and prints the answer, because "the children are in the job" is exactly
    the kind of claim that reads true and is not.
    """
    if not WINDOWS:
        return None
    k32 = _kernel32()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    member = ctypes.c_int(0)
    try:
        ok = k32.IsProcessInJob(handle, ctypes.c_void_p(job), ctypes.byref(member))
        return bool(member.value) if ok else None
    finally:
        k32.CloseHandle(ctypes.c_void_p(handle))


# ----------------------------------------------------------------------- layers 2 & 3


def _parent_watch(parent_pid: int):
    """A predicate that is true once this process has been orphaned.

    On Windows the parent is held open by handle, so the answer cannot be confused by a
    PID the OS has since handed to somebody else. Failing to open the handle at all means
    the parent is already gone, and the child says so immediately.
    """
    if disabled("ORPHAN_WATCH"):
        return lambda: False
    if not WINDOWS:
        return lambda: os.getppid() != parent_pid
    k32 = _kernel32()
    handle = k32.OpenProcess(SYNCHRONIZE, False, parent_pid)
    if not handle:
        return lambda: True
    return lambda: k32.WaitForSingleObject(ctypes.c_void_p(handle), 0) != WAIT_TIMEOUT


def burn(deadline: float, is_orphaned, now=time.time) -> str:
    """Occupy one CPU until the deadline passes or the parent goes away.

    Returns the reason it stopped, which the child prints -- an orphaned child that exits
    by layer 3 is a different event from one that ran its course, and a log that cannot
    tell them apart cannot tell you your tear-down failed.
    """
    spin = 1.0
    while True:
        if now() >= deadline:
            return "deadline"
        if is_orphaned():
            return "orphaned"
        started = time.monotonic()
        while time.monotonic() - started < ORPHAN_POLL_SECONDS:
            for _ in range(20000):
                spin = spin * 1.0000001 + 1.0


# ------------------------------------------------------------------- the process table


def host_processes() -> list[dict[str, object]]:
    """Every process on the host with its command line.

    Shelling out to CIM rather than importing `psutil`, which this venv does not have --
    a pre-flight that cannot run until someone installs something is a pre-flight nobody
    runs.
    """
    if WINDOWS:
        script = (
            "Get-CimInstance Win32_Process | "
            "Select-Object ProcessId,Name,CommandLine,CreationDate | "
            "ConvertTo-Json -Compress -Depth 3"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        payload = json.loads(proc.stdout)
        rows = payload if isinstance(payload, list) else [payload]
        return [
            {
                "pid": int(row.get("ProcessId") or 0),
                "name": row.get("Name") or "",
                "command_line": row.get("CommandLine") or "",
                "started": str(row.get("CreationDate") or ""),
            }
            for row in rows
        ]
    proc = subprocess.run(
        ["ps", "-eo", "pid=,comm=,args="],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    rows = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3:
            rows.append(
                {
                    "pid": int(parts[0]),
                    "name": parts[1],
                    "command_line": parts[2],
                    "started": "",
                }
            )
    return rows


def tagged_children(
    tag: str, table: list[dict[str, object]] | None = None
) -> list[dict[str, object]]:
    """Every process on the host that is a child of this run, found by its command line.

    **Not by the PIDs `run` happens to hold, and this is the whole lesson of the ticket.**
    `.venv/Scripts/python.exe` on this machine is a 253 KB stub over
    `miniconda3/python.exe`: it re-execs the real interpreter as a second process with an
    identical command line, so `--count 2` puts **four** processes on the host --
    `measured` 2026-09-12, PIDs 5576 (stub) and 22512 (real), 29092 (stub) and 8560.
    `subprocess.Popen` holds the stub. Terminating what `Popen` holds and reporting the
    count it held would report a clean tear-down while the real interpreters kept burning,
    which is exactly what `i13-day/stop-at-2130.ps1` did when it killed a `cmd` wrapper
    and logged `done` over a collector that ran for another 73 seconds.

    So the count that matters is read back off the host, by tag, wrappers and all.
    """
    rows = host_processes() if table is None else table
    return [
        row
        for row in rows
        if tag in str(row["command_line"]) and "child" in str(row["command_line"])
    ]


def pid_alive(pid: int, table: list[dict[str, object]] | None = None) -> bool:
    """Whether `pid` is still in the host process table.

    Deliberately independent of the `Popen` object. `Popen.wait` returning tells you the
    handle you hold was signalled; it does not tell you the machine is quiet, and the
    watchdog in `i13-day` is what a tear-down that trusts its own kill call looks like.
    """
    rows = host_processes() if table is None else table
    return any(row["pid"] == pid for row in rows)


def suspects(
    table: list[dict[str, object]],
    extra: tuple[str, ...] = (),
    exclude: tuple[int, ...] = (),
) -> list[dict[str, object]]:
    """Every process in `table` whose command line looks like a load generator."""
    groups = [*SUSPECT_GROUPS, *[(term,) for term in extra]]
    found = []
    for row in table:
        if row["pid"] in exclude:
            continue
        line = str(row["command_line"])
        if any(all(term in line for term in group) for group in groups):
            found.append(row)
    return found


# -------------------------------------------------------------------------- the tool


def run_load(
    count: int, seconds: float, tag: str = DEFAULT_TAG, python: str = sys.executable
) -> dict[str, object]:
    """Raise `count` competing processes, hold them for `seconds`, reap them, and report.

    The tear-down is in the `finally`, which handles the clean exit and the interrupt. It
    is not what handles the force-kill -- the job object is -- and the difference is the
    point of the module.
    """
    lifetime = min(float(seconds), MAX_LIFETIME_SECONDS)
    deadline = time.time() + lifetime
    job, job_mode = open_kill_on_close_job()
    children: list[subprocess.Popen[bytes]] = []
    raised_processes = 0
    try:
        for _ in range(count):
            child = subprocess.Popen(
                [
                    python,
                    os.path.abspath(__file__),
                    "child",
                    "--parent",
                    str(os.getpid()),
                    "--deadline",
                    repr(deadline),
                    "--tag",
                    tag,
                ]
            )
            children.append(child)
            if job is not None and job_mode == "per-child":
                assign_to_job(job, child.pid)
        members = [in_job(job, c.pid) for c in children] if job is not None else []
        # Read the host back rather than trusting `len(children)`: an interpreter stub
        # puts two processes on the machine per child, and it is the host's number that
        # has to reach zero. See `tagged_children`.
        raised_processes = len(tagged_children(tag))
        print(
            f"loadgen: spawned={len(children)} raised={raised_processes} tag={tag} "
            f"job={job_mode} in_job={sum(1 for m in members if m)} "
            f"lifetime={lifetime:.1f}s "
            f"orphan_watch={'off' if disabled('ORPHAN_WATCH') else 'on'}",
            flush=True,
        )
        end = time.time() + lifetime
        while time.time() < end and any(c.poll() is None for c in children):
            time.sleep(0.2)
    finally:
        report = teardown(
            children, tag=tag, job_mode=job_mode, raised_processes=raised_processes
        )
    return report


def teardown(
    children: list[subprocess.Popen[bytes]],
    tag: str,
    job_mode: str = "",
    raised_processes: int = 0,
) -> dict[str, object]:
    """Kill every child, then read the host back to find out whether they actually died.

    Two counts are reported and they are not the same number. `spawned` is how many
    children were asked for. `raised_processes` is how many processes carrying this run's
    tag were seen on the host once they were up -- larger whenever an interpreter stub
    sits in front of the real one. It is `raised_processes` that must reach zero.
    """
    for child in children:
        if child.poll() is None:
            try:
                child.terminate()
            except OSError:
                pass
    for child in children:
        try:
            child.wait(timeout=REAP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    deadline = time.monotonic() + REAP_TIMEOUT_SECONDS
    survivors = tagged_children(tag)
    while survivors and time.monotonic() < deadline:
        time.sleep(0.5)
        survivors = tagged_children(tag)
    survivor_pids = sorted(int(row["pid"]) for row in survivors)
    reaped = raised_processes - len(survivor_pids)
    report = {
        "tag": tag,
        "job_mode": job_mode,
        "spawned": len(children),
        "raised": raised_processes,
        "reaped": reaped,
        "survivors": survivor_pids,
        "verified_by": "Win32_Process" if WINDOWS else "ps -eo",
    }
    print(
        f"loadgen: raised={raised_processes} reaped={reaped} "
        f"survivors={survivor_pids}",
        flush=True,
    )
    print(json.dumps(report, indent=2), flush=True)
    return report


def check(extra: tuple[str, ...] = ()) -> dict[str, object]:
    """The pre-flight. Count load generators on this host and say so loudly."""
    table = host_processes()
    found = suspects(table, extra=extra, exclude=(os.getpid(),))
    result = {
        "host_processes": len(table),
        "load_generators": len(found),
        "processes": found,
    }
    if found:
        bar = "!" * 78
        print(bar, flush=True)
        print(
            f"!! {len(found)} LOAD-GENERATOR PROCESSES ARE RUNNING ON THIS HOST.",
            flush=True,
        )
        print("!! Any measurement or gate taken now is not on a quiet machine.", flush=True)
        print("!! #96 left 112 of these for 73 minutes: 9.5x slower, 22 false failures.", flush=True)
        print(bar, flush=True)
        for row in found:
            print(f"   pid={row['pid']:<8} started={row['started']} {row['command_line']}")
    else:
        print(
            f"loadgen check: 0 load-generator processes found "
            f"among {len(table)} host processes. Machine is quiet.",
            flush=True,
        )
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="loadgen.py", description="Raise competing load and guarantee its tear-down."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    runner = sub.add_parser("run", help="raise N competing processes and reap them")
    runner.add_argument("--count", type=int, default=24, help="processes to raise")
    runner.add_argument(
        "--seconds",
        type=float,
        default=60.0,
        help=f"how long to hold the load, capped at {MAX_LIFETIME_SECONDS:.0f}s",
    )
    runner.add_argument("--tag", default=DEFAULT_TAG, help="marker in the child cmdline")

    pre = sub.add_parser("check", help="pre-flight: count load generators on this host")
    pre.add_argument(
        "--pattern",
        action="append",
        default=[],
        help="extra command-line substring to treat as a load generator",
    )

    kid = sub.add_parser("child", help="internal: one burning process, do not call by hand")
    kid.add_argument("--parent", type=int, required=True)
    kid.add_argument("--deadline", type=float, required=True)
    kid.add_argument("--tag", default=DEFAULT_TAG)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "child":
        capped = min(args.deadline, time.time() + MAX_LIFETIME_SECONDS)
        reason = burn(capped, _parent_watch(args.parent))
        print(f"loadgen child {os.getpid()} exiting: {reason}", flush=True)
        return 0
    if args.command == "check":
        result = check(extra=tuple(args.pattern))
        return 1 if result["load_generators"] else 0
    report = run_load(count=args.count, seconds=args.seconds, tag=args.tag)
    return 0 if report["raised"] == report["reaped"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""#99: the load generator's tear-down, and the case a clean-exit test would miss.

**The test that matters is `test_children_are_reaped_when_the_parent_is_force_killed`.**
Everything else is scaffolding around it. #96's 112 orphans were not left behind by a
script that exited badly; they were left behind by a session that ended while the script
was still running, which on Windows means `TerminateProcess` and no `finally`. A test that
lets the generator finish normally proves the branch that was never the problem.

`taskkill /F /PID` without `/T` is used deliberately: `/T` kills the process tree and
would make the job object irrelevant -- the test would pass with no mechanism at all.
The parent alone is killed, and the children have to be reaped by something other than
their parent's goodwill.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[2] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import loadgen  # noqa: E402

WINDOWS = sys.platform == "win32"
SCRIPT = TOOLS / "loadgen.py"

#: Small and short on purpose. Three processes for a few seconds prove the mechanism;
#: #96 proved that 112 for an hour prove nothing extra and cost a measurement window.
COUNT = 3
PATIENCE = 45.0


def _children(tag: str) -> list[dict[str, object]]:
    """Every live process carrying this test's unique tag, wrappers included.

    `>= COUNT` rather than `== COUNT` everywhere below, because this machine's
    `.venv/Scripts/python.exe` is a stub that re-execs `miniconda3/python.exe`: one
    logical child is two host processes. Asserting equality here is what made the first
    run of this file hang for 45 s a test at a time -- it was waiting for exactly three
    processes while six were up.
    """
    return loadgen.tagged_children(tag)


def _real_parent_pid(tag: str) -> int:
    """The PID the children themselves name as their parent.

    `Popen.pid` is **not** it. `.venv/Scripts/python.exe` is a stub that re-execs the real
    interpreter, so `Popen` holds the stub and the process running `run_load` is the
    stub's child. Force-killing the stub would leave the real parent alive, its job handle
    open and its children burning -- a test that killed the wrapper and concluded the
    tear-down worked would be `i13-day/stop-at-2130.ps1` written a second time.

    Each child carries `--parent <pid>` in its own command line, which is the PID the
    parent reported for itself. That is the one to kill.
    """
    for row in _children(tag):
        found = re.search(r"--parent (\d+)", str(row["command_line"]))
        if found:
            return int(found.group(1))
    raise AssertionError(f"no child carrying {tag} named a parent")


def _wait_for(predicate, patience: float = PATIENCE, what: str = "condition"):
    """Wait on a condition, never on a duration -- AGENTS.md's rule, and this file's
    processes are exactly the kind of thing whose timing the OS scheduler settles."""
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    table = loadgen.host_processes()
    ours = [r for r in table if "loadgen.py" in str(r["command_line"])]
    raise AssertionError(
        f"waited {patience}s for {what} and it never happened. "
        f"host table had {len(table)} processes, {len(ours)} mentioning loadgen.py: "
        + "; ".join(str(r["command_line"])[-90:] for r in ours[:6])
    )


@pytest.fixture
def tag() -> str:
    """A tag unique to one test, so four worktrees running at once cannot collide."""
    return f"dxp99-{os.getpid()}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def reaper(tag: str):
    """Kill anything this test raised, whatever the test did.

    The fixture exists because a test file about tear-down that leaks processes when it
    fails would be writing this ticket's own defect into this ticket.
    """
    yield tag
    for row in _children(tag):
        pid = str(row["pid"])
        kill = ["taskkill", "/F", "/PID", pid] if WINDOWS else ["kill", "-9", pid]
        subprocess.run(kill, capture_output=True, check=False)


# ------------------------------------------------------------------ layers 2 and 3


def test_burn_stops_at_its_deadline_without_consulting_anything_else() -> None:
    moments = iter([100.0, 100.0, 200.0])
    stopped = loadgen.burn(150.0, lambda: False, now=lambda: next(moments))
    assert stopped == "deadline"


def test_burn_stops_when_it_is_orphaned_before_its_deadline() -> None:
    stopped = loadgen.burn(time.time() + 3600.0, lambda: True)
    assert stopped == "orphaned"


def test_a_child_that_cannot_open_its_parent_treats_itself_as_orphaned() -> None:
    """PID 0 cannot be opened, so the watch must report orphaned rather than assume alive.

    The failure this pins is the optimistic one: a watch that cannot see its parent and
    concludes the parent is fine is a watch that never fires.
    """
    assert loadgen._parent_watch(0)() is True


# ------------------------------------------------------------------- the pre-flight


def test_the_preflight_finds_a_burn_py_and_does_not_find_itself() -> None:
    table = [
        {"pid": 1, "name": "py", "command_line": "py C:/x/burn.py", "started": ""},
        {
            "pid": 2,
            "name": "python.exe",
            "command_line": 'python.exe "D:/x/tools/loadgen.py" child --parent 9',
            "started": "",
        },
        {"pid": 3, "name": "py", "command_line": "py loadgen.py check", "started": ""},
        {"pid": 4, "name": "node.exe", "command_line": "node next dev", "started": ""},
    ]
    found = {row["pid"] for row in loadgen.suspects(table)}
    assert found == {1, 2}


def test_the_preflight_excludes_the_pid_it_is_told_to() -> None:
    table = [{"pid": 7, "name": "python.exe", "command_line": "burn.py", "started": ""}]
    assert loadgen.suspects(table, exclude=(7,)) == []


def test_the_preflight_honours_an_extra_pattern() -> None:
    table = [
        {"pid": 8, "name": "py.exe", "command_line": "stress-ng --cpu 8", "started": ""}
    ]
    assert len(loadgen.suspects(table, extra=("stress-ng",))) == 1


# ------------------------------------------------------------------------ the tool


def test_help_exits_zero_with_path_emptied() -> None:
    """`docs/handoff.md`: a green suite does not prove a script runs.

    `tools/smoke_stack.py` had `main(argv)` then `del argv`, so `--help` built six
    containers while 1,214 tests passed. Every new tool is probed this way.
    """
    env = {**os.environ, "PATH": ""}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(not WINDOWS, reason="the job object guarantee is a Windows mechanism")
def test_run_reaps_every_process_it_raised_on_a_clean_exit(reaper: str) -> None:
    report = loadgen.run_load(count=COUNT, seconds=2.0, tag=reaper)
    assert report["spawned"] == COUNT
    assert report["raised"] >= COUNT, "the host must have seen at least one process each"
    assert report["reaped"] == report["raised"]
    assert report["survivors"] == []
    assert _children(reaper) == []


@pytest.mark.skipif(not WINDOWS, reason="the job object guarantee is a Windows mechanism")
def test_children_are_reaped_when_the_parent_is_force_killed(reaper: str) -> None:
    """**The criterion.** Kill the parent the way a session ending kills it, and look.

    `--seconds 600` is far longer than this test's patience on purpose: if the children
    were only bounded by their own deadline, this test would time out rather than pass,
    so a pass is evidence of the job object and not of layer 2.
    """
    parent = subprocess.Popen(
        [sys.executable, str(SCRIPT), "run", "--count", str(COUNT), "--seconds", "600",
         "--tag", reaper]
    )
    try:
        _wait_for(
            lambda: len(_children(reaper)) >= COUNT,
            what=f"at least {COUNT} processes carrying {reaper}",
        )
        real = _real_parent_pid(reaper)
        killed = subprocess.run(
            ["taskkill", "/F", "/PID", str(real)], capture_output=True, text=True
        )
        assert killed.returncode == 0, killed.stderr
        _wait_for(
            lambda: not loadgen.pid_alive(real) or None,
            what=f"the real parent {real} to leave the process table",
        )
    finally:
        if parent.poll() is None:
            parent.kill()
    survivors = _wait_for(
        lambda: _children(reaper) == [] or None,
        what="every child to leave the host process table",
    )
    assert survivors is not None
    assert _children(reaper) == []


@pytest.mark.skipif(not WINDOWS, reason="the job object guarantee is a Windows mechanism")
@pytest.mark.parametrize(
    ("switch_off", "expect_reaped"),
    [
        pytest.param({"LOADGEN_DISABLE_JOB": "1"}, True, id="job-object-removed"),
        pytest.param(
            {"LOADGEN_DISABLE_ORPHAN_WATCH": "1"}, True, id="orphan-watch-removed"
        ),
        pytest.param(
            {"LOADGEN_DISABLE_JOB": "1", "LOADGEN_DISABLE_ORPHAN_WATCH": "1"},
            False,
            id="job-object-and-orphan-watch-removed",
        ),
    ],
)
def test_removing_a_layer_brings_the_leak_back(
    reaper: str, switch_off: dict[str, str], expect_reaped: bool
) -> None:
    """The mutation control. Without it the test above is a claim, not a measurement.

    Take layer 1 away and the orphan watch still reaps, slower. Take both away and the
    children outlive the parent exactly as #96's 112 did -- which is what proves
    `test_children_are_reaped_when_the_parent_is_force_killed` is capable of failing.

    `--seconds 45` bounds the deliberate leak: even if this test is itself interrupted
    between the kill and the cleanup, layer 2 ends it inside a minute.
    """
    env = {**os.environ, **switch_off}
    parent = subprocess.Popen(
        [sys.executable, str(SCRIPT), "run", "--count", str(COUNT), "--seconds", "45",
         "--tag", reaper],
        env=env,
    )
    try:
        _wait_for(lambda: len(_children(reaper)) >= COUNT, what="the load to come up")
        before = len(_children(reaper))
        real = _real_parent_pid(reaper)
        subprocess.run(["taskkill", "/F", "/PID", str(real)], capture_output=True)
        _wait_for(
            lambda: not loadgen.pid_alive(real) or None,
            what=f"the real parent {real} to leave the process table",
        )
    finally:
        if parent.poll() is None:
            parent.kill()

    if expect_reaped:
        _wait_for(lambda: _children(reaper) == [] or None, what="the layer to fire")
        assert _children(reaper) == []
    else:
        time.sleep(5.0)
        assert len(_children(reaper)) >= before, (
            "with every reaping layer removed the children must outlive their parent; "
            "if they do not, the kill-the-parent test above proves nothing"
        )


@pytest.mark.skipif(not WINDOWS, reason="the job object guarantee is a Windows mechanism")
def test_the_preflight_sees_real_load_and_exits_nonzero(reaper: str) -> None:
    """End to end: raise load, and confirm one command would have caught it."""
    parent = subprocess.Popen(
        [sys.executable, str(SCRIPT), "run", "--count", str(COUNT), "--seconds", "20",
         "--tag", reaper]
    )
    try:
        _wait_for(lambda: len(_children(reaper)) >= COUNT, what="the load to come up")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "check"], capture_output=True, text=True
        )
        assert proc.returncode == 1, proc.stdout
        assert "LOAD-GENERATOR PROCESSES ARE RUNNING" in proc.stdout
        assert reaper in proc.stdout
    finally:
        parent.terminate()
        parent.wait(timeout=30)
    _wait_for(lambda: _children(reaper) == [] or None, what="the load to go away")

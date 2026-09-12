"""#92: two pytest sessions must never share a test Redis.

Before this ticket, `conftest.py` fixed the container name, the port and therefore the
whole Redis instance as module constants (`deltapayoff-tests-redis-6399` on `6399`).
Two sessions in two worktrees shared one container; the second session's teardown
(`docker rm -f`) removed the first session's Redis while its reader was still on it,
deleting the stream keys under it -- the `NOGROUP` failure the ticket quotes.

This file proves two things the fixture change alone does not make visible:

1. Two sessions built in the same process never get the same container, port or url
   (`new_redis_test_session`, no Docker needed -- pure arithmetic and a socket bind).
2. With real containers, tearing one session's Redis down never touches a second,
   independent session's keys, and the removal guard refuses (rather than silently
   no-ops) a container this session did not start.

Both need Docker for their real half; both skip loudly, the same way `redis_server`
does, rather than passing as zero tests on a machine without it.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from contextlib import ExitStack

import pytest

import conftest
from conftest import (
    RedisTestSession,
    _remove_own_container,
    new_redis_test_session,
    redis_test_session,
)


def _docker_or_skip() -> str:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("SKIPPED LOUDLY: no docker on PATH")
    probe = subprocess.run(
        [docker, "version", "--format", "{{.Server.Version}}"],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        pytest.skip(
            "SKIPPED LOUDLY: docker is installed but its daemon is not answering "
            f"({probe.stderr.strip() or probe.stdout.strip()})"
        )
    return docker


def _container_running(docker: str, name: str) -> bool:
    result = subprocess.run(
        [docker, "ps", "-q", "--filter", f"name=^{name}$"],
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


# --------------------------------------------------------- no docker needed


def test_two_sessions_built_in_one_process_never_share_a_name_port_or_url() -> None:
    """The namespacing itself: nothing here starts a container, so it runs everywhere
    `redis_server` would otherwise skip. Two independent calls -- one per hypothetical
    worktree -- must disagree on every field that a fixed constant used to fix."""
    first = new_redis_test_session()
    second = new_redis_test_session(avoid_ports=frozenset({first.port}))

    assert first.session_id != second.session_id
    assert first.container != second.container
    assert first.port != second.port
    assert first.url != second.url
    # Not just distinct -- each still carries the OLD collision points, so a reader of
    # `docker ps` can tell them apart by name alone, same as before #92.
    assert first.container.startswith("deltapayoff-tests-redis-")
    assert second.container.startswith("deltapayoff-tests-redis-")


def test_a_foreign_container_is_named_and_refused_not_silently_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard's decision function in isolation: told about a container it did not
    start, it must refuse by naming the real owner -- not return quietly as if there
    were nothing to remove. No `docker inspect` here; `_owner_label` is stood in for,
    so this runs with no Docker at all.
    """
    mine = RedisTestSession(session_id="mine", container="doesnt-matter", port=1)

    monkeypatch.setattr(
        conftest,
        "_owner_label",
        lambda docker, container: (True, "someone-elses-session"),
    )

    with pytest.raises(RuntimeError, match="someone-elses-session"):
        conftest._remove_own_container("docker", mine)


# ------------------------------------------------------------- needs docker


@pytest.fixture
def two_independent_sessions() -> Iterator[tuple[RedisTestSession, RedisTestSession]]:
    """Two real, independently-started Redis containers -- as close as one process can
    get to "two worktrees running `pytest` at once" without actually forking two."""
    docker = _docker_or_skip()
    with ExitStack() as stack:
        session_a = stack.enter_context(redis_test_session())
        session_b = stack.enter_context(
            redis_test_session(
                new_redis_test_session(avoid_ports=frozenset({session_a.port}))
            )
        )
        assert session_a.container != session_b.container, (
            "the fixture handed back two sessions sharing a container -- the exact "
            "collision #92 closes"
        )
        yield session_a, session_b
    # ExitStack tears both down in reverse order; either container left running here
    # would be this test leaking, same as the rule the fixture itself follows.
    assert not _container_running(docker, session_a.container)
    assert not _container_running(docker, session_b.container)


def test_tearing_down_one_session_leaves_the_others_keys_alone(
    two_independent_sessions: tuple[RedisTestSession, RedisTestSession],
) -> None:
    """The reported failure, reproduced and then proven closed.

    #92's evidence was `deltapayoff-tests-redis-6399` recreated by a different
    worktree's fixture, and a reader losing the stream and group `XTRIM`/removal took
    with it. Here: two real containers, one stream key written to each, one torn down
    early -- the survivor's key must still answer exactly what was written.
    """
    import redis

    docker = _docker_or_skip()
    session_a, session_b = two_independent_sessions

    client_a = redis.Redis.from_url(session_a.url, socket_connect_timeout=2)
    client_b = redis.Redis.from_url(session_b.url, socket_connect_timeout=2)
    try:
        client_a.xadd("alert", {"who": "session-a"})
        client_b.xadd("alert", {"who": "session-b"})
        assert client_a.xlen("alert") == 1
        assert client_b.xlen("alert") == 1

        # Session A's session ends -- its container is removed, as a real pytest
        # session's `redis_server` teardown would do at the end of its run.
        _remove_own_container(docker, session_a)
        assert not _container_running(docker, session_a.container)

        # Session B never asked for anything and must not have lost anything.
        entries = client_b.xrange("alert")
        assert len(entries) == 1
        assert entries[0][1][b"who"] == b"session-b"
    finally:
        client_a.close()
        client_b.close()


def test_a_session_never_removes_a_container_it_did_not_start(
    two_independent_sessions: tuple[RedisTestSession, RedisTestSession],
) -> None:
    """The guard end to end: asked to remove session A under session B's identity, it
    must refuse and leave A's container running."""
    docker = _docker_or_skip()
    session_a, session_b = two_independent_sessions

    impostor = RedisTestSession(
        session_id=session_b.session_id,
        container=session_a.container,
        port=session_a.port,
    )

    with pytest.raises(RuntimeError, match=session_a.session_id):
        _remove_own_container(docker, impostor)

    assert _container_running(docker, session_a.container), (
        "the guard let a container be removed under someone else's session id"
    )

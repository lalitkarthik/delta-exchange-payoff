"""The sentence the store writes when a replay gap cannot be counted.

`RedisBus.replay_gaps` answers `None` for the lost count when the stream is absent or its
consumer group is gone, and `None` for the first retained id when nothing survives. **Both
values are correct and neither may become `0`** -- what a stream held before a saved
position is genuinely unknowable once the group that was reading it is gone, and
`CONTEXT.md` section 7 refuses `0` for absent. The defect was the rendering: `!r` put the
`None` straight into the operator's sentence.

`measured` on the live restarted stack, `docker logs dxp-store` at 2026-09-12T15:52:33Z,
four times, once per stream:

    stream computed.chain:DELTA:BTC lost None entries before saved position
    1789210919555-2; first retained id is None

Record [0010](../../docs/design/decisions/0010-store-replay.md) R5 requires that case to
read *stream absent or empty*, never *lost None entries*.

`_gap_detail` is a pure function of one `ReplayGap`, so it is tested as one -- no Redis,
no process. The `ReplayGap`s below are the exact shapes `replay_gaps` constructs: see
`redis_bus.RedisBus.replay_gaps`, where an absent stream gives `(None, None)` and a stream
whose group is gone gives a real first retained id beside a `None` count.
"""

from __future__ import annotations

from deltapayoff.redis_bus import ReplayGap
from deltapayoff.store_main import _gap_detail

STREAM = "computed.chain:DELTA:BTC"
SAVED = "1789210919555-2"


def absent_stream() -> ReplayGap:
    """What `replay_gaps` builds when `XINFO STREAM` does not answer with a dict."""
    return ReplayGap(STREAM, SAVED, first_retained_id=None, lost=None, trimmed=0)


def group_gone() -> ReplayGap:
    """The stream is there and holding entries; the group that read it is not."""
    return ReplayGap(
        STREAM, SAVED, first_retained_id="1789210920000-0", lost=None, trimmed=41
    )


def counted() -> ReplayGap:
    """The ordinary case: Redis answered and the loss is exact."""
    return ReplayGap(
        STREAM, SAVED, first_retained_id="1789210920000-0", lost=17, trimmed=41
    )


def test_an_uncountable_gap_never_renders_the_word_none() -> None:
    """The whole defect, as an assertion. A record that says `None` sends an operator
    looking for a stream state that does not exist."""
    for gap in (absent_stream(), group_gone()):
        detail = _gap_detail(gap)
        assert "None" not in detail, detail
        assert "lost None entries" not in detail, detail


def test_an_uncountable_gap_says_the_stream_is_absent_or_empty() -> None:
    """R5's required wording, and the two facts that are still known: which stream, and
    the position the store saved."""
    detail = _gap_detail(absent_stream())
    assert "absent or empty" in detail, detail
    assert STREAM in detail
    assert SAVED in detail
    assert "cannot be counted" in detail, detail


def test_an_uncountable_gap_still_reports_a_retained_bound_when_there_is_one() -> None:
    """A missing group does not make the stream's own contents unknown. The count is what
    cannot be answered; the bound that survives is still worth printing, and saying
    nothing about it would be a second absent value hidden rather than named."""
    detail = _gap_detail(group_gone())
    assert "1789210920000-0" in detail, detail


def test_a_counted_gap_still_reports_the_count_and_both_bounds() -> None:
    """The countable case is untouched. This is the record an operator reads in the case
    that actually has a number, and #107 must not have moved it."""
    detail = _gap_detail(counted())
    assert "lost 17 entries" in detail, detail
    assert SAVED in detail
    assert "1789210920000-0" in detail, detail
    assert "absent or empty" not in detail, detail

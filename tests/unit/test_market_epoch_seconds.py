"""Regression test for a real bug found during the live-price audit:
apps/api/routers/market.py used to call naive_datetime.timestamp() to
serialize candle/signal times for the chart. Every timestamp column in
this codebase is naive UTC (Column(DateTime), datetime.utcnow() defaults)
-- but datetime.timestamp() on a naive value silently assumes the LOCAL
system timezone, not UTC. On a machine whose OS timezone isn't UTC (this
dev machine is IST, UTC+5:30 -- confirmed via time.timezone during this
audit), that shifted every chart bar's reported time by the local offset
(a real 08:00:00 UTC candle was reported to the frontend as if it were
02:30:00 UTC). apps/api/routers/market.py's _epoch_seconds() fixes this
by using calendar.timegm(), which reads the naive value's fields directly
as UTC with no local-timezone conversion -- this test locks that in
independent of whatever timezone the test runner itself is in.
"""
from datetime import datetime

from apps.api.routers.market import _epoch_seconds


def test_epoch_seconds_treats_naive_datetime_as_utc_not_local():
    # 2026-08-27 08:00:00 UTC is a known, independently-verifiable epoch
    # value -- computed once via `date -u -d @1787817600` / online converter,
    # not derived from the function under test.
    dt = datetime(2026, 8, 27, 8, 0, 0)
    assert _epoch_seconds(dt) == 1787817600


def test_epoch_seconds_does_not_reproduce_the_ist_offset_bug():
    # The real bug this test guards against: naive_dt.timestamp() on an
    # IST (UTC+5:30) machine would have returned 1787797800 (02:30:00 UTC)
    # for this exact input -- 5.5 hours off. Assert the fixed function does
    # NOT reproduce that wrong value.
    dt = datetime(2026, 8, 27, 8, 0, 0)
    assert _epoch_seconds(dt) != 1787797800


def test_epoch_seconds_matches_expected_delta_for_a_4h_bucket():
    earlier = datetime(2026, 8, 27, 4, 0, 0)
    later = datetime(2026, 8, 27, 8, 0, 0)
    assert _epoch_seconds(later) - _epoch_seconds(earlier) == 4 * 3600

"""Tests for _StartupRetrySupervisor (services/market_data/live_state.py)
-- the production-hardening fix for a real observed gap: the live
market-data WebSocket's INITIAL connection attempt could fail with no
automatic retry (python-socketio's own reconnection only activates AFTER
a connection has succeeded once). All timing is injected via a fake
wait_fn (never a global asyncio monkeypatch, never a real sleep) so these
run instantly and deterministically -- never a real connection."""
import asyncio

import pytest

from services.market_data.live_state import (
    STARTUP_RETRY_DELAYS_SECONDS,
    _StartupRetrySupervisor,
)


class _FakeWaiter:
    """Records every backoff delay it was asked to wait for and returns
    instantly, as if the full delay always elapsed normally (never
    interrupted) -- unless the supervisor's stop_event is already set, in
    which case it returns instantly too (mirroring a real already-set
    Event resolving .wait() immediately)."""

    def __init__(self):
        self.delays_seen: list[float] = []

    async def __call__(self, stop_event: asyncio.Event, timeout: float) -> None:
        self.delays_seen.append(timeout)
        # A real yield point (not a true sleep) so this cooperates with the
        # event loop instead of letting _run()'s while-loop spin forever
        # inside a single scheduler step (neither a failing connect_fn nor
        # an instant return here would otherwise ever suspend) -- the real
        # _run() checks stop_event.is_set() itself right after this returns.
        await asyncio.sleep(0)


def _flaky_connect(fail_times: int):
    """Returns an async connect_fn that raises ConnectionError `fail_times`
    times, then succeeds."""
    calls = {"count": 0}

    async def connect():
        calls["count"] += 1
        if calls["count"] <= fail_times:
            raise ConnectionError("Connection error")
        return None

    return connect, calls


def _supervisor(connect_fn):
    waiter = _FakeWaiter()
    supervisor = _StartupRetrySupervisor(connect_fn=connect_fn, wait_fn=waiter)
    return supervisor, waiter


# ---- 1. Initial connection succeeds immediately ----

@pytest.mark.asyncio
async def test_initial_connection_succeeds_immediately():
    connect, calls = _flaky_connect(fail_times=0)
    supervisor, waiter = _supervisor(connect)
    supervisor.start()
    await supervisor._task
    assert calls["count"] == 1
    assert waiter.delays_seen == []


# ---- 2. Fails once, then succeeds ----

@pytest.mark.asyncio
async def test_initial_connection_fails_once_then_succeeds():
    connect, calls = _flaky_connect(fail_times=1)
    supervisor, waiter = _supervisor(connect)
    supervisor.start()
    await supervisor._task
    assert calls["count"] == 2
    assert waiter.delays_seen == [STARTUP_RETRY_DELAYS_SECONDS[0]]


# ---- 3. Fails twice, then succeeds ----

@pytest.mark.asyncio
async def test_initial_connection_fails_twice_then_succeeds():
    connect, calls = _flaky_connect(fail_times=2)
    supervisor, waiter = _supervisor(connect)
    supervisor.start()
    await supervisor._task
    assert calls["count"] == 3
    assert waiter.delays_seen == [STARTUP_RETRY_DELAYS_SECONDS[0], STARTUP_RETRY_DELAYS_SECONDS[1]]


# ---- 4. Repeatedly fails and remains retrying ----

@pytest.mark.asyncio
async def test_repeated_failure_keeps_retrying_and_holds_at_max_backoff():
    connect, calls = _flaky_connect(fail_times=10**9)  # never succeeds within this test
    supervisor, waiter = _supervisor(connect)
    supervisor.start()

    # Let it run through more attempts than the backoff table has entries.
    for _ in range(500):
        if len(waiter.delays_seen) >= len(STARTUP_RETRY_DELAYS_SECONDS) + 2:
            break
        await asyncio.sleep(0)
    assert supervisor.is_running()
    assert calls["count"] >= len(STARTUP_RETRY_DELAYS_SECONDS) + 1
    await supervisor.stop()


# ---- 5. Backoff timing is correct ----

@pytest.mark.asyncio
async def test_backoff_sequence_matches_the_documented_schedule():
    connect, calls = _flaky_connect(fail_times=6)
    supervisor, waiter = _supervisor(connect)
    supervisor.start()
    await supervisor._task
    assert waiter.delays_seen == [2, 4, 8, 16, 30, 30]  # holds at 30 after the table is exhausted


# ---- 6. No duplicate retry loops ----

@pytest.mark.asyncio
async def test_start_is_idempotent_while_already_running():
    connect, calls = _flaky_connect(fail_times=10**9)
    supervisor, waiter = _supervisor(connect)
    supervisor.start()
    first_task = supervisor._task
    supervisor.start()  # duplicate start while already running
    second_task = supervisor._task
    assert first_task is second_task  # no second task was created
    await supervisor.stop()


# ---- 7. Shutdown cancels retry loop ----

@pytest.mark.asyncio
async def test_stop_cancels_the_retry_loop_promptly():
    connect, calls = _flaky_connect(fail_times=10**9)
    supervisor, waiter = _supervisor(connect)
    supervisor.start()
    await asyncio.sleep(0)  # let it make at least one attempt
    calls_before_stop = calls["count"]
    await supervisor.stop()
    assert supervisor.is_running() is False
    assert supervisor._task is None
    # No further attempts after stop (the task is gone, not just marked done).
    calls_after_stop = calls["count"]
    assert calls_after_stop == calls_before_stop


# ---- 8. Successful connection stops startup retries ----

@pytest.mark.asyncio
async def test_successful_connection_ends_the_supervisor_task():
    connect, calls = _flaky_connect(fail_times=1)
    supervisor, waiter = _supervisor(connect)
    supervisor.start()
    await supervisor._task
    assert supervisor._task.done()
    assert supervisor.is_running() is False


# ---- 11. No credentials appear in logs ----

@pytest.mark.asyncio
async def test_retry_log_calls_never_include_credential_looking_fields(monkeypatch):
    logged = []

    def fake_warning(msg, **kwargs):
        logged.append((msg, kwargs))

    monkeypatch.setattr("services.market_data.live_state.logger.warning", fake_warning)
    connect, calls = _flaky_connect(fail_times=2)
    supervisor, waiter = _supervisor(connect)
    supervisor.start()
    await supervisor._task

    assert len(logged) == 2
    for msg, kwargs in logged:
        combined = (msg + " " + " ".join(f"{k}={v}" for k, v in kwargs.items())).lower()
        assert "api_key" not in combined
        assert "api_secret" not in combined
        assert "token" not in combined

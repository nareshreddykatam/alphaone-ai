"""Phase 5: scheduler runner ticks, tested individually (not the infinite
loop) against an in-memory DB and monkeypatched job functions -- proves
circuit-breaker gating and start/stop lifecycle work without needing a
real CoinDCX connection or real sleeps.
"""
import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from services.market_data.live_state import live_candle_aggregators
from services.scheduler.circuit_breaker import CircuitState
from services.scheduler.runner import SchedulerRunner


@pytest.fixture
async def patched_session(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("services.scheduler.runner.async_session", session_maker)
    yield session_maker
    await engine.dispose()


@pytest.mark.asyncio
async def test_account_sync_tick_records_success_with_no_credentials(patched_session):
    runner = SchedulerRunner()  # no api key/secret -- provider reports NOT_CONFIGURED, not an exception
    await runner.run_once_account_sync()
    assert runner.account_sync_breaker.state == CircuitState.CLOSED
    assert runner.account_sync_breaker.consecutive_failures == 0


@pytest.mark.asyncio
async def test_account_sync_tick_records_failure_on_exception(patched_session, monkeypatch):
    async def _boom(session, provider):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr("services.scheduler.runner.account_sync_job", _boom)
    runner = SchedulerRunner()
    for _ in range(runner.account_sync_breaker.config.failure_threshold):
        await runner.run_once_account_sync()
    assert runner.account_sync_breaker.state.value == "OPEN"


@pytest.mark.asyncio
async def test_account_sync_tick_skips_when_circuit_open(patched_session, monkeypatch):
    calls = {"n": 0}

    async def _boom(session, provider):
        calls["n"] += 1
        raise RuntimeError("simulated failure")

    monkeypatch.setattr("services.scheduler.runner.account_sync_job", _boom)
    runner = SchedulerRunner()
    for _ in range(runner.account_sync_breaker.config.failure_threshold):
        await runner.run_once_account_sync()
    calls_after_open = calls["n"]

    await runner.run_once_account_sync()  # circuit should now be open -- job must not be called
    assert calls["n"] == calls_after_open


@pytest.mark.asyncio
async def test_exit_alert_tick_is_a_noop_with_no_price_data(patched_session):
    runner = SchedulerRunner()
    await runner.run_once_exit_alerts()
    assert runner.exit_alert_breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_signal_generation_tick_handles_no_data_gracefully(patched_session):
    runner = SchedulerRunner()
    await runner.run_once_signal_generation()
    assert runner.signal_breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_outcome_evaluation_tick_handles_no_data_gracefully(patched_session):
    runner = SchedulerRunner()
    await runner.run_once_outcome_evaluation()
    assert runner.outcome_breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_start_and_stop_lifecycle_creates_and_cancels_all_jobs(patched_session):
    runner = SchedulerRunner()
    runner.start()
    assert len(runner._tasks) == 6
    assert all(not t.done() for t in runner._tasks)

    await runner.stop()
    assert runner._tasks == []
    assert runner._running is False


@pytest.mark.asyncio
async def test_candle_ingestion_tick_records_success(patched_session, monkeypatch):
    async def _fake_job(session, exchange, **kwargs):
        return 0  # nothing new to store -- still a success, not a failure

    monkeypatch.setattr("services.scheduler.runner.candle_ingestion_job", _fake_job)
    runner = SchedulerRunner()
    await runner.run_once_candle_ingestion()
    assert runner.candle_ingestion_breaker.state == CircuitState.CLOSED
    assert runner.candle_ingestion_breaker.consecutive_failures == 0


@pytest.mark.asyncio
async def test_candle_ingestion_tick_closes_the_exchange_even_on_success(patched_session, monkeypatch):
    closed = {"called": False}

    async def _fake_job(session, exchange, **kwargs):
        return 3

    async def _fake_close(self):
        closed["called"] = True

    monkeypatch.setattr("services.scheduler.runner.candle_ingestion_job", _fake_job)
    monkeypatch.setattr("services.market_data.binance.BinanceExchange.close", _fake_close)
    runner = SchedulerRunner()
    await runner.run_once_candle_ingestion()
    assert closed["called"] is True


@pytest.mark.asyncio
async def test_candle_ingestion_tick_records_failure_on_exception(patched_session, monkeypatch):
    async def _boom(session, exchange, **kwargs):
        raise RuntimeError("simulated Binance failure")

    async def _fake_close(self):
        return None

    monkeypatch.setattr("services.scheduler.runner.candle_ingestion_job", _boom)
    monkeypatch.setattr("services.market_data.binance.BinanceExchange.close", _fake_close)
    runner = SchedulerRunner()
    for _ in range(runner.candle_ingestion_breaker.config.failure_threshold):
        await runner.run_once_candle_ingestion()
    assert runner.candle_ingestion_breaker.state.value == "OPEN"


@pytest.mark.asyncio
async def test_candle_ingestion_tick_skips_when_circuit_open(patched_session, monkeypatch):
    calls = {"n": 0}

    async def _boom(session, exchange, **kwargs):
        calls["n"] += 1
        raise RuntimeError("simulated Binance failure")

    async def _fake_close(self):
        return None

    monkeypatch.setattr("services.scheduler.runner.candle_ingestion_job", _boom)
    monkeypatch.setattr("services.market_data.binance.BinanceExchange.close", _fake_close)
    runner = SchedulerRunner()
    for _ in range(runner.candle_ingestion_breaker.config.failure_threshold):
        await runner.run_once_candle_ingestion()
    calls_after_open = calls["n"]

    await runner.run_once_candle_ingestion()  # circuit should now be open -- job must not be called
    assert calls["n"] == calls_after_open


@pytest.mark.asyncio
async def test_candle_ingestion_failure_does_not_affect_other_breakers(patched_session, monkeypatch):
    """One job's failure must never cascade into another -- each has its
    own independent CircuitBreaker."""
    async def _boom(session, exchange, **kwargs):
        raise RuntimeError("simulated Binance failure")

    async def _fake_close(self):
        return None

    monkeypatch.setattr("services.scheduler.runner.candle_ingestion_job", _boom)
    monkeypatch.setattr("services.market_data.binance.BinanceExchange.close", _fake_close)
    runner = SchedulerRunner()
    for _ in range(runner.candle_ingestion_breaker.config.failure_threshold):
        await runner.run_once_candle_ingestion()
    assert runner.candle_ingestion_breaker.state.value == "OPEN"

    await runner.run_once_signal_generation()
    assert runner.signal_breaker.state == CircuitState.CLOSED


# ---- 6th job: live_breakout ----

@pytest.mark.asyncio
async def test_live_breakout_tick_records_success(patched_session, monkeypatch):
    async def _fake_job(session, market_ws, aggregator, **kwargs):
        return None  # no breakout this tick -- still a success, not a failure

    monkeypatch.setattr("services.scheduler.runner.live_breakout_job", _fake_job)
    runner = SchedulerRunner()
    await runner.run_once_live_breakout()
    assert runner.live_breakout_breaker.state == CircuitState.CLOSED
    assert runner.live_breakout_breaker.consecutive_failures == 0


@pytest.mark.asyncio
async def test_live_breakout_tick_records_failure_on_exception(patched_session, monkeypatch):
    async def _boom(session, market_ws, aggregator, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr("services.scheduler.runner.live_breakout_job", _boom)
    runner = SchedulerRunner()
    for _ in range(runner.live_breakout_breaker.config.failure_threshold):
        await runner.run_once_live_breakout()
    assert runner.live_breakout_breaker.state.value == "OPEN"


@pytest.mark.asyncio
async def test_live_breakout_tick_skips_when_circuit_open(patched_session, monkeypatch):
    calls = {"n": 0}

    async def _boom(session, market_ws, aggregator, **kwargs):
        calls["n"] += 1
        raise RuntimeError("simulated failure")

    monkeypatch.setattr("services.scheduler.runner.live_breakout_job", _boom)
    runner = SchedulerRunner()
    for _ in range(runner.live_breakout_breaker.config.failure_threshold):
        await runner.run_once_live_breakout()
    calls_after_open = calls["n"]

    await runner.run_once_live_breakout()  # circuit should now be open -- job must not be called
    assert calls["n"] == calls_after_open


@pytest.mark.asyncio
async def test_live_breakout_failure_does_not_affect_other_breakers(patched_session, monkeypatch):
    async def _boom(session, market_ws, aggregator, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr("services.scheduler.runner.live_breakout_job", _boom)
    runner = SchedulerRunner()
    for _ in range(runner.live_breakout_breaker.config.failure_threshold):
        await runner.run_once_live_breakout()
    assert runner.live_breakout_breaker.state.value == "OPEN"

    await runner.run_once_candle_ingestion()
    assert runner.candle_ingestion_breaker.state == CircuitState.CLOSED


@pytest.mark.asyncio
async def test_live_breakout_uses_a_persistent_aggregator_shared_across_ticks(patched_session, monkeypatch):
    """The forming-candle aggregator must be the SAME object across ticks
    (unlike the per-tick-fresh CoinDCX/Binance clients) -- otherwise it
    could never remember a running high/low within one still-forming bar."""
    seen_aggregators = []

    async def _fake_job(session, market_ws, aggregator, **kwargs):
        seen_aggregators.append(aggregator)
        return None

    monkeypatch.setattr("services.scheduler.runner.live_breakout_job", _fake_job)
    runner = SchedulerRunner()
    await runner.run_once_live_breakout()
    await runner.run_once_live_breakout()
    assert seen_aggregators[0] is seen_aggregators[1]
    assert seen_aggregators[0] is runner._live_candle_aggregator


# ---- Watchdog + heartbeat (added after a real production incident where
# every DB-writing scheduler job went silent for 12+ minutes with no way
# to tell, from the outside, whether the job LOOP had stopped iterating
# versus every attempt merely failing before writing anything) ----

@pytest.mark.asyncio
async def test_loop_watchdog_times_out_a_hung_tick_and_keeps_iterating(monkeypatch):
    """A tick_fn that hangs forever must never block _loop() forever -- the
    watchdog times it out (logged, not raised) and the loop proceeds to
    its next scheduled iteration instead of staying stuck."""
    import services.scheduler.runner as runner_module

    monkeypatch.setattr(runner_module, "JOB_TICK_TIMEOUT_SECONDS", 0.05)
    runner = SchedulerRunner()
    runner._running = True
    call_count = {"n": 0}

    async def _hangs_forever():
        call_count["n"] += 1
        await asyncio.sleep(999)  # would block the loop forever without the watchdog

    loop_task = asyncio.create_task(runner._loop("test_job", _hangs_forever, 0.01))
    try:
        await asyncio.sleep(0.3)  # several watchdog-timeout cycles
    finally:
        runner._running = False
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

    assert call_count["n"] >= 2, "the loop must re-invoke tick_fn after each timeout, not stay stuck on the first hang"


@pytest.mark.asyncio
async def test_loop_watchdog_does_not_fire_for_a_normal_fast_tick(monkeypatch):
    """A tick_fn that completes quickly must run to completion normally --
    the watchdog must never interrupt legitimate, fast work."""
    import services.scheduler.runner as runner_module

    monkeypatch.setattr(runner_module, "JOB_TICK_TIMEOUT_SECONDS", 5.0)
    runner = SchedulerRunner()
    runner._running = True
    completed = {"n": 0}

    async def _fast():
        completed["n"] += 1

    loop_task = asyncio.create_task(runner._loop("test_job", _fast, 0.01))
    try:
        await asyncio.sleep(0.1)
    finally:
        runner._running = False
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

    assert completed["n"] >= 2


@pytest.mark.asyncio
async def test_heartbeat_is_null_before_any_tick():
    runner = SchedulerRunner()
    hb = runner.get_heartbeat()
    assert hb["scheduler_running"] is False
    for job_name in ("account_sync", "exit_alerts", "signal_generation", "outcome_evaluation", "candle_ingestion", "live_breakout"):
        assert hb["jobs"][job_name]["last_tick_at"] is None
        assert hb["jobs"][job_name]["seconds_since_last_tick"] is None
        assert hb["jobs"][job_name]["circuit_state"] == "CLOSED"


@pytest.mark.asyncio
async def test_heartbeat_last_tick_at_advances_as_the_loop_iterates(patched_session, monkeypatch):
    """last_tick_at is stamped at the START of every _loop iteration --
    proving loop liveness independently of whether the underlying job
    succeeds, fails, or times out."""
    async def _fake_job(session, provider):
        return {"balance": {"status": "OK"}, "positions": None}

    monkeypatch.setattr("services.scheduler.runner.account_sync_job", _fake_job)
    runner = SchedulerRunner()
    runner._running = True

    assert runner.get_heartbeat()["jobs"]["account_sync"]["last_tick_at"] is None

    loop_task = asyncio.create_task(runner._loop("account_sync", runner.run_once_account_sync, 0.01))
    try:
        await asyncio.sleep(0.1)
    finally:
        runner._running = False
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

    hb = runner.get_heartbeat()["jobs"]["account_sync"]
    assert hb["last_tick_at"] is not None
    assert hb["seconds_since_last_tick"] < 10
    assert hb["circuit_state"] == "CLOSED"


@pytest.mark.asyncio
async def test_heartbeat_reflects_circuit_breaker_failures(patched_session, monkeypatch):
    async def _boom(session, provider):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr("services.scheduler.runner.account_sync_job", _boom)
    runner = SchedulerRunner()
    for _ in range(3):
        await runner.run_once_account_sync()

    hb = runner.get_heartbeat()["jobs"]["account_sync"]
    assert hb["consecutive_failures"] == 3
    assert hb["circuit_state"] == "CLOSED"  # below failure_threshold (5) -- not yet OPEN


def test_scheduler_singleton_is_wired_into_main_and_health_router():
    """apps/api/main.py and apps/api/routers/health.py must both reference
    the SAME process-wide scheduler instance (services/scheduler/runner.py)
    -- never a second, independently-constructed SchedulerRunner that would
    silently duplicate jobs or report a heartbeat for the wrong instance."""
    from services.scheduler.runner import scheduler
    from apps.api.main import scheduler as main_scheduler
    from apps.api.routers.health import scheduler as health_scheduler

    assert main_scheduler is scheduler
    assert health_scheduler is scheduler


def test_live_breakout_aggregator_is_specifically_the_4h_registry_entry():
    """The scheduler's live-signal detection stays 4h-only even though the
    live_state registry now holds an aggregator per supported timeframe
    (1m/5m/15m/1h/4h/1d) -- the validated strategy must never silently pick
    up a different timeframe's forming candle."""
    runner = SchedulerRunner()
    assert runner._live_candle_aggregator is live_candle_aggregators["4h"]
    assert runner._live_candle_aggregator is not live_candle_aggregators["1h"]

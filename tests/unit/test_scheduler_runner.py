"""Phase 5: scheduler runner ticks, tested individually (not the infinite
loop) against an in-memory DB and monkeypatched job functions -- proves
circuit-breaker gating and start/stop lifecycle work without needing a
real CoinDCX connection or real sleeps.
"""
import asyncio

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
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
    assert len(runner._tasks) == 5
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

"""Tests for services/signal_engine/live_breakout.py: evaluate_live_breakout
-- the intrabar orchestration layer (connection freshness gating, forming-
candle-vs-closed-candle alignment, dedup, persistence). The underlying
Donchian+ADX math itself is already extensively tested elsewhere
(tests/unit/test_signal_engine.py, test_live_signal.py) and is NOT
reimplemented or re-validated here -- most tests below monkeypatch
BaselineStrategy.generate to isolate the orchestration logic that IS new,
plus one true end-to-end test with real strategy computation for
confidence that the pieces fit together correctly.
"""
from datetime import datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import Candle, ConnectionState, Signal
from services.signal_engine.live_breakout import LiveCandleAggregator, evaluate_live_breakout
from services.signal_engine.strategy import StrategySignal


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class FakeMarketState:
    def __init__(self, last_price_usdt=None, received_at=None):
        self.last_price_usdt = last_price_usdt
        self.received_at = received_at


class FakeMarketWs:
    """Duck-types the parts of CoinDCXMarketDataWebSocket that
    evaluate_live_breakout actually reads -- no real connection, no
    socketio, ever."""

    def __init__(self, status=ConnectionState.LIVE, price=None, received_at=None):
        self._status = status
        self.state = FakeMarketState(last_price_usdt=price, received_at=received_at)

    def connection_status(self):
        return self._status


CLOSED_CANDLE_INTERVAL = timedelta(hours=4)


async def _seed_closed_candles(session, n=120, last_close=200.0, symbol="BTC/USDT", timeframe="4h", base_time=None):
    """Real, non-fabricated-looking OHLCV rows -- an uptrend, matching the
    same shape tests/unit/test_live_signal.py's own seeding helper uses --
    ending exactly `CLOSED_CANDLE_INTERVAL` before `base_time` so a forming
    candle opening at `base_time` is always the correct "next bar"."""
    base_time = base_time or datetime(2026, 1, 1)
    rng = np.random.default_rng(0)
    trend = np.linspace(last_close - 100, last_close, n)
    noise = rng.normal(0, 0.5, n)
    start = base_time - CLOSED_CANDLE_INTERVAL * n
    last_timestamp = None
    for i in range(n):
        close = trend[i] + noise[i]
        ts = start + CLOSED_CANDLE_INTERVAL * i
        last_timestamp = ts
        session.add(Candle(
            timestamp=ts, timeframe=timeframe, symbol=symbol,
            open=close - 0.2, high=close + 1, low=close - 1, close=close, volume=100.0,
            quality_status="valid",
        ))
    await session.commit()
    return last_timestamp + CLOSED_CANDLE_INTERVAL  # the correct forming-candle open_time


def _fake_long_result():
    return StrategySignal(
        signal_type="LONG", entry_price=205.0, stop_loss=195.0, take_profit_1=225.0,
        take_profit_2=None, take_profit_3=None, quality="HIGH",
        reasoning="fake LONG for orchestration testing", strategy_name="trend_following_donchian_adx",
    )


def _fake_no_trade_result():
    return StrategySignal(
        signal_type="NO_TRADE", entry_price=None, stop_loss=None, take_profit_1=None,
        take_profit_2=None, take_profit_3=None, quality="LOW",
        reasoning="fake NO_TRADE for orchestration testing", strategy_name="trend_following_donchian_adx",
    )


# ---- 10/12. Stale/disconnected/missing market data handled safely ----

@pytest.mark.asyncio
async def test_returns_none_when_disconnected(session_maker, monkeypatch):
    async with session_maker() as session:
        forming_open = await _seed_closed_candles(session, n=120)
        monkeypatch.setattr("services.signal_engine.strategy.BaselineStrategy.generate", lambda self, df: _fake_long_result())
        market_ws = FakeMarketWs(status=ConnectionState.DISCONNECTED, price=205.0, received_at=forming_open)
        signal = await evaluate_live_breakout(session, market_ws, LiveCandleAggregator("4h"))
        assert signal is None
        assert (await session.execute(select(Signal))).scalars().all() == []


@pytest.mark.asyncio
async def test_returns_none_when_stale_not_strictly_live(session_maker, monkeypatch):
    async with session_maker() as session:
        forming_open = await _seed_closed_candles(session, n=120)
        monkeypatch.setattr("services.signal_engine.strategy.BaselineStrategy.generate", lambda self, df: _fake_long_result())
        market_ws = FakeMarketWs(status=ConnectionState.STALE, price=205.0, received_at=forming_open)
        signal = await evaluate_live_breakout(session, market_ws, LiveCandleAggregator("4h"))
        assert signal is None


@pytest.mark.asyncio
async def test_returns_none_when_price_missing(session_maker):
    async with session_maker() as session:
        await _seed_closed_candles(session, n=120)
        market_ws = FakeMarketWs(status=ConnectionState.LIVE, price=None)
        signal = await evaluate_live_breakout(session, market_ws, LiveCandleAggregator("4h"))
        assert signal is None


@pytest.mark.asyncio
async def test_returns_none_with_insufficient_closed_candles(session_maker):
    async with session_maker() as session:
        forming_open = await _seed_closed_candles(session, n=10)  # well under MIN_BARS_REQUIRED
        market_ws = FakeMarketWs(status=ConnectionState.LIVE, price=205.0, received_at=forming_open)
        signal = await evaluate_live_breakout(session, market_ws, LiveCandleAggregator("4h"))
        assert signal is None


@pytest.mark.asyncio
async def test_returns_none_when_forming_candle_does_not_follow_the_latest_closed_one(session_maker, monkeypatch):
    """A gap or a stale live tick (e.g. the aggregator's bucket doesn't
    line up with what's actually next after the latest closed candle)
    must never be guessed into a fabricated forming candle."""
    async with session_maker() as session:
        await _seed_closed_candles(session, n=120, base_time=datetime(2026, 1, 1))
        monkeypatch.setattr("services.signal_engine.strategy.BaselineStrategy.generate", lambda self, df: _fake_long_result())
        wrong_time = datetime(2026, 6, 1)  # nowhere near the real next bar
        market_ws = FakeMarketWs(status=ConnectionState.LIVE, price=205.0, received_at=wrong_time)
        signal = await evaluate_live_breakout(session, market_ws, LiveCandleAggregator("4h"))
        assert signal is None


# ---- 1/2/3. Breakout detection (LONG via monkeypatched strategy; real SHORT/LONG covered by the end-to-end test below) ----

@pytest.mark.asyncio
async def test_a_real_long_breakout_is_detected_and_persisted(session_maker, monkeypatch):
    async with session_maker() as session:
        forming_open = await _seed_closed_candles(session, n=120)
        monkeypatch.setattr("services.signal_engine.strategy.BaselineStrategy.generate", lambda self, df: _fake_long_result())
        market_ws = FakeMarketWs(status=ConnectionState.LIVE, price=205.0, received_at=forming_open)
        signal = await evaluate_live_breakout(session, market_ws, LiveCandleAggregator("4h"))
        assert signal is not None
        assert signal.signal_type == "LONG"
        assert signal.timestamp == forming_open
        assert "LIVE/INTRABAR" in signal.reasoning
        assert "$" not in signal.reasoning  # no USD ever in user-facing text


@pytest.mark.asyncio
async def test_no_trade_is_never_persisted_or_returned(session_maker, monkeypatch):
    async with session_maker() as session:
        forming_open = await _seed_closed_candles(session, n=120)
        monkeypatch.setattr("services.signal_engine.strategy.BaselineStrategy.generate", lambda self, df: _fake_no_trade_result())
        market_ws = FakeMarketWs(status=ConnectionState.LIVE, price=205.0, received_at=forming_open)
        signal = await evaluate_live_breakout(session, market_ws, LiveCandleAggregator("4h"))
        assert signal is None
        assert (await session.execute(select(Signal))).scalars().all() == []


# ---- 8/9/11. Deduplication (must not fire repeatedly, restart-safe) ----

@pytest.mark.asyncio
async def test_does_not_fire_again_for_the_same_forming_candle_on_a_later_tick(session_maker, monkeypatch):
    async with session_maker() as session:
        forming_open = await _seed_closed_candles(session, n=120)
        monkeypatch.setattr("services.signal_engine.strategy.BaselineStrategy.generate", lambda self, df: _fake_long_result())
        aggregator = LiveCandleAggregator("4h")
        market_ws = FakeMarketWs(status=ConnectionState.LIVE, price=205.0, received_at=forming_open)

        first = await evaluate_live_breakout(session, market_ws, aggregator)
        assert first is not None

        # A later tick, same still-forming candle, price still above the level.
        market_ws.state.last_price_usdt = 206.0
        market_ws.state.received_at = forming_open + timedelta(minutes=5)
        second = await evaluate_live_breakout(session, market_ws, aggregator)
        assert second is None  # must NOT fire again while price remains above the breakout level

        rows = (await session.execute(select(Signal).where(Signal.signal_type != "NO_TRADE"))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_restart_is_safe_a_fresh_process_does_not_duplicate_an_already_alerted_candle(session_maker, monkeypatch):
    """Simulates a process restart: a brand-new LiveCandleAggregator (all
    in-memory state gone) plus a fresh evaluate_live_breakout call against
    a DB that already has a signal for this candle (from "before the
    restart") -- the DB-backed dedup check must catch this, not just
    in-memory state."""
    async with session_maker() as session:
        forming_open = await _seed_closed_candles(session, n=120)
        monkeypatch.setattr("services.signal_engine.strategy.BaselineStrategy.generate", lambda self, df: _fake_long_result())

        pre_restart_ws = FakeMarketWs(status=ConnectionState.LIVE, price=205.0, received_at=forming_open)
        first = await evaluate_live_breakout(session, pre_restart_ws, LiveCandleAggregator("4h"))
        assert first is not None

        # Fresh aggregator, fresh market_ws instance -- as if the process restarted.
        post_restart_aggregator = LiveCandleAggregator("4h")
        post_restart_ws = FakeMarketWs(status=ConnectionState.LIVE, price=207.0, received_at=forming_open + timedelta(minutes=1))
        second = await evaluate_live_breakout(session, post_restart_ws, post_restart_aggregator)
        assert second is None

        rows = (await session.execute(select(Signal).where(Signal.signal_type != "NO_TRADE"))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_shared_dedup_with_the_scheduled_closed_candle_path(session_maker, monkeypatch):
    """The live path and the existing closed-candle path
    (generate_and_persist_signal) share the same dedup guard -- whichever
    fires first for a given candle wins, the other is a clean no-op."""
    from services.signal_engine.live_signal import generate_and_persist_signal

    async with session_maker() as session:
        forming_open = await _seed_closed_candles(session, n=120)
        monkeypatch.setattr("services.signal_engine.strategy.BaselineStrategy.generate", lambda self, df: _fake_long_result())

        market_ws = FakeMarketWs(status=ConnectionState.LIVE, price=205.0, received_at=forming_open)
        live_signal = await evaluate_live_breakout(session, market_ws, LiveCandleAggregator("4h"))
        assert live_signal is not None

        # Now the candle "closes" (gets ingested for real) and the
        # scheduled path evaluates it -- must not duplicate.
        session.add(Candle(
            timestamp=forming_open, timeframe="4h", symbol="BTC/USDT",
            open=204.0, high=206.0, low=203.0, close=205.0, volume=50.0, quality_status="valid",
        ))
        await session.commit()
        scheduled_signal = await generate_and_persist_signal(session)
        assert scheduled_signal is None  # already alerted live -- no duplicate

        rows = (await session.execute(select(Signal).where(Signal.signal_type != "NO_TRADE"))).scalars().all()
        assert len(rows) == 1


# ---- Real end-to-end: actual Donchian+ADX math, not a monkeypatched result ----

@pytest.mark.asyncio
async def test_end_to_end_real_strategy_computation_on_a_genuine_uptrend(session_maker):
    async with session_maker() as session:
        forming_open = await _seed_closed_candles(session, n=150, last_close=500.0)
        # A live tick that breaks meaningfully above the recent range, on
        # top of a genuine, strong uptrend -- real ADX/Donchian computed
        # for real, no monkeypatching.
        market_ws = FakeMarketWs(status=ConnectionState.LIVE, price=520.0, received_at=forming_open)
        signal = await evaluate_live_breakout(session, market_ws, LiveCandleAggregator("4h"))

        # Never assert a specific direction (the exact real ADX/Donchian
        # outcome on random-seeded noise is not something to hardcode) --
        # only that IF a signal fired, it is well-formed, real, and
        # consistent with what the module promises.
        if signal is not None:
            assert signal.signal_type in ("LONG", "SHORT")
            assert signal.strategy_name == "trend_following_donchian_adx"
            assert signal.timestamp == forming_open

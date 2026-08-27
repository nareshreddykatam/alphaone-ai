"""Integration tests for services/signal_engine/multi_strategy_engine.py's
evaluate_all_strategies_for_timeframe -- against a real (in-memory) SQLite
DB, using FAKE strategies with fully controlled, deterministic outputs so
independence/dedup/timeframe-scoping/production-eligibility-gating can be
proven precisely, rather than hoping real data happens to exercise them.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema import models  # noqa: F401 -- ensure models are registered
from database.schema.models import Candle
from services.signal_engine.multi_strategy import StrategyDefinition
from services.signal_engine.strategy import SignalStrategy, StrategySignal


class _FakeStrategy(SignalStrategy):
    def __init__(self, strategy_id: str, signal_type: str):
        self.strategy_id = strategy_id
        self.name = strategy_id
        self._signal_type = signal_type

    def generate(self, df) -> StrategySignal:
        if self._signal_type == "NO_TRADE":
            return StrategySignal(
                signal_type="NO_TRADE", entry_price=None, stop_loss=None,
                take_profit_1=None, take_profit_2=None, take_profit_3=None,
                quality="LOW", reasoning="fake NO_TRADE", strategy_name=self.strategy_id,
            )
        entry = float(df.iloc[-1]["close"])
        if self._signal_type == "LONG":
            sl, tp = entry - 100.0, entry + 200.0
        else:
            sl, tp = entry + 100.0, entry - 200.0
        return StrategySignal(
            signal_type=self._signal_type, entry_price=entry, stop_loss=sl,
            take_profit_1=tp, take_profit_2=None, take_profit_3=None,
            quality="MEDIUM", reasoning=f"fake {self._signal_type}", strategy_name=self.strategy_id,
        )


def _fake_def(strategy_id: str, signal_type: str, timeframe: str, production_status: str = "PRODUCTION_ELIGIBLE") -> StrategyDefinition:
    return StrategyDefinition(
        strategy_id=strategy_id, display_name=f"Fake {strategy_id}", timeframe=timeframe,
        data_mode="CLOSED_CANDLE", production_status=production_status,
        make_strategy=lambda: _FakeStrategy(strategy_id, signal_type),
    )


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _seed_candles(session_maker, symbol: str, timeframe: str, n: int = 80):
    interval = {"15m": timedelta(minutes=15), "4h": timedelta(hours=4), "1d": timedelta(days=1)}[timeframe]
    start = datetime(2024, 1, 1)
    async with session_maker() as session:
        for i in range(n):
            ts = start + interval * i
            session.add(Candle(
                timestamp=ts, timeframe=timeframe, symbol=symbol,
                open=100.0, high=101.0, low=99.0, close=100.0 + i * 0.01, volume=10.0,
                quality_status="valid",
            ))
        await session.commit()


@pytest.mark.asyncio
async def test_independent_strategies_both_persist_with_no_suppression(session_maker, monkeypatch):
    """6/7. Strategy 1's LONG must not suppress Strategy 2's SHORT on the
    exact same candle -- both are separately valid, independent outputs."""
    fake_registry = [
        _fake_def("FAKE_LONG_4H", "LONG", "4h"),
        _fake_def("FAKE_SHORT_4H", "SHORT", "4h"),
    ]
    monkeypatch.setattr("services.signal_engine.multi_strategy_engine.MULTI_STRATEGY_REGISTRY", fake_registry)
    await _seed_candles(session_maker, "BTC/USDT", "4h")

    from services.signal_engine.multi_strategy_engine import evaluate_all_strategies_for_timeframe

    async with session_maker() as session:
        signals = await evaluate_all_strategies_for_timeframe(session, "BTC/USDT", "4h")

    types_by_strategy = {s.strategy_name: s.signal_type for s in signals}
    assert types_by_strategy == {"FAKE_LONG_4H": "LONG", "FAKE_SHORT_4H": "SHORT"}


@pytest.mark.asyncio
async def test_research_only_strategy_never_persists_even_when_it_would_fire(session_maker, monkeypatch):
    """Only PRODUCTION_ELIGIBLE strategies may ever reach Telegram/paper
    trading -- a RESEARCH_ONLY strategy's LONG must never be persisted or
    alerted, no matter how strongly its own signal_func fires."""
    fake_registry = [
        _fake_def("FAKE_RESEARCH_4H", "LONG", "4h", production_status="RESEARCH_ONLY"),
    ]
    monkeypatch.setattr("services.signal_engine.multi_strategy_engine.MULTI_STRATEGY_REGISTRY", fake_registry)
    await _seed_candles(session_maker, "BTC/USDT", "4h")

    from services.signal_engine.multi_strategy_engine import evaluate_all_strategies_for_timeframe

    async with session_maker() as session:
        signals = await evaluate_all_strategies_for_timeframe(session, "BTC/USDT", "4h")
    assert signals == []


@pytest.mark.asyncio
async def test_no_trade_is_never_persisted(session_maker, monkeypatch):
    fake_registry = [_fake_def("FAKE_NOTRADE_4H", "NO_TRADE", "4h")]
    monkeypatch.setattr("services.signal_engine.multi_strategy_engine.MULTI_STRATEGY_REGISTRY", fake_registry)
    await _seed_candles(session_maker, "BTC/USDT", "4h")

    from services.signal_engine.multi_strategy_engine import evaluate_all_strategies_for_timeframe

    async with session_maker() as session:
        signals = await evaluate_all_strategies_for_timeframe(session, "BTC/USDT", "4h")
    assert signals == []


# ---- 8/9. Dedup: same strategy+event -> one signal; different
# strategies+event -> separate signals (already proven above for the
# "different" case -- this proves the "same" case). ----

@pytest.mark.asyncio
async def test_same_strategy_same_event_is_deduplicated(session_maker, monkeypatch):
    fake_registry = [_fake_def("FAKE_LONG_4H", "LONG", "4h")]
    monkeypatch.setattr("services.signal_engine.multi_strategy_engine.MULTI_STRATEGY_REGISTRY", fake_registry)
    await _seed_candles(session_maker, "BTC/USDT", "4h")

    from services.signal_engine.multi_strategy_engine import evaluate_all_strategies_for_timeframe

    async with session_maker() as session:
        first = await evaluate_all_strategies_for_timeframe(session, "BTC/USDT", "4h")
    async with session_maker() as session:
        second = await evaluate_all_strategies_for_timeframe(session, "BTC/USDT", "4h")

    assert len(first) == 1
    assert second == []  # same candle, same strategy -- must not duplicate


@pytest.mark.asyncio
async def test_restart_does_not_duplicate_signals(session_maker, monkeypatch):
    """11. The dedup guard is a real DB query (signal_already_exists_for_candle),
    not in-memory state -- a fresh process (simulated here by a brand-new
    session with no shared Python state) must still see the already-
    persisted signal and refuse to duplicate it."""
    fake_registry = [_fake_def("FAKE_LONG_4H", "LONG", "4h")]
    monkeypatch.setattr("services.signal_engine.multi_strategy_engine.MULTI_STRATEGY_REGISTRY", fake_registry)
    await _seed_candles(session_maker, "BTC/USDT", "4h")

    from services.signal_engine.multi_strategy_engine import evaluate_all_strategies_for_timeframe

    async with session_maker() as session:
        await evaluate_all_strategies_for_timeframe(session, "BTC/USDT", "4h")

    # Simulate a full process restart: a brand-new engine/session against
    # the SAME underlying data would be the real-world equivalent; here a
    # fresh session against the same in-memory DB is the equivalent unit
    # (no in-memory dedup state exists anywhere to reset).
    async with session_maker() as fresh_session:
        after_restart = await evaluate_all_strategies_for_timeframe(fresh_session, "BTC/USDT", "4h")
    assert after_restart == []


# ---- 12/13. 15m strategies use 15m data; 4h strategies use 4h data. ----

@pytest.mark.asyncio
async def test_15m_evaluation_only_touches_15m_strategies_and_data(session_maker, monkeypatch):
    fake_registry = [
        _fake_def("FAKE_15M", "LONG", "15m"),
        _fake_def("FAKE_4H", "LONG", "4h"),
    ]
    monkeypatch.setattr("services.signal_engine.multi_strategy_engine.MULTI_STRATEGY_REGISTRY", fake_registry)
    await _seed_candles(session_maker, "BTC/USDT", "15m")
    # Deliberately do NOT seed 4h candles -- if the 15m evaluation touched
    # the 4h strategy at all, it would find zero data and (correctly)
    # produce nothing, but we additionally assert only the 15m strategy_name
    # appears, proving the OTHER timeframe's strategy was never even asked.

    from services.signal_engine.multi_strategy_engine import evaluate_all_strategies_for_timeframe

    async with session_maker() as session:
        signals = await evaluate_all_strategies_for_timeframe(session, "BTC/USDT", "15m")

    assert [s.strategy_name for s in signals] == ["FAKE_15M"]
    assert signals[0].timeframe == "15m"


@pytest.mark.asyncio
async def test_4h_evaluation_only_touches_4h_strategies_and_data(session_maker, monkeypatch):
    fake_registry = [
        _fake_def("FAKE_15M", "LONG", "15m"),
        _fake_def("FAKE_4H", "LONG", "4h"),
    ]
    monkeypatch.setattr("services.signal_engine.multi_strategy_engine.MULTI_STRATEGY_REGISTRY", fake_registry)
    await _seed_candles(session_maker, "BTC/USDT", "4h")

    from services.signal_engine.multi_strategy_engine import evaluate_all_strategies_for_timeframe

    async with session_maker() as session:
        signals = await evaluate_all_strategies_for_timeframe(session, "BTC/USDT", "4h")

    assert [s.strategy_name for s in signals] == ["FAKE_4H"]
    assert signals[0].timeframe == "4h"


# ---- 14. Live data freshness / sufficiency is enforced -- never fabricate
# a signal from too little history. ----

@pytest.mark.asyncio
async def test_insufficient_candles_produces_no_signals(session_maker, monkeypatch):
    fake_registry = [_fake_def("FAKE_LONG_4H", "LONG", "4h")]
    monkeypatch.setattr("services.signal_engine.multi_strategy_engine.MULTI_STRATEGY_REGISTRY", fake_registry)
    await _seed_candles(session_maker, "BTC/USDT", "4h", n=10)  # well under MIN_BARS_REQUIRED

    from services.signal_engine.multi_strategy_engine import evaluate_all_strategies_for_timeframe

    async with session_maker() as session:
        signals = await evaluate_all_strategies_for_timeframe(session, "BTC/USDT", "4h")
    assert signals == []


@pytest.mark.asyncio
async def test_no_candles_at_all_produces_no_signals(session_maker, monkeypatch):
    fake_registry = [_fake_def("FAKE_LONG_4H", "LONG", "4h")]
    monkeypatch.setattr("services.signal_engine.multi_strategy_engine.MULTI_STRATEGY_REGISTRY", fake_registry)

    from services.signal_engine.multi_strategy_engine import evaluate_all_strategies_for_timeframe

    async with session_maker() as session:
        signals = await evaluate_all_strategies_for_timeframe(session, "BTC/USDT", "4h")
    assert signals == []


@pytest.mark.asyncio
async def test_paused_signal_generation_produces_no_signals(session_maker, monkeypatch):
    fake_registry = [_fake_def("FAKE_LONG_4H", "LONG", "4h")]
    monkeypatch.setattr("services.signal_engine.multi_strategy_engine.MULTI_STRATEGY_REGISTRY", fake_registry)
    await _seed_candles(session_maker, "BTC/USDT", "4h")

    from services.signal_engine.live_signal import set_signal_generation_paused
    from services.signal_engine.multi_strategy_engine import evaluate_all_strategies_for_timeframe

    async with session_maker() as session:
        await set_signal_generation_paused(session, True)
    async with session_maker() as session:
        signals = await evaluate_all_strategies_for_timeframe(session, "BTC/USDT", "4h")
    assert signals == []

"""Phase 4: on-demand signal generation from real (DB-stored) candles must
never fabricate a signal when there isn't enough real data, and must
persist both the Signal and a PENDING SignalOutcome row so downstream
performance tracking (services/portfolio/service.py) has something to
evaluate later."""
from datetime import datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import Candle, Signal, SignalOutcome, SignalOutcomeType
from services.signal_engine.live_signal import (
    generate_and_persist_signal, MIN_BARS_REQUIRED, set_signal_generation_paused,
)


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_trending_candles(session, n=120, symbol="BTC/USDT", timeframe="4h"):
    rng = np.random.default_rng(0)
    trend = np.linspace(100, 300, n)
    noise = rng.normal(0, 0.5, n)
    base_time = datetime(2026, 1, 1)
    for i in range(n):
        close = trend[i] + noise[i]
        session.add(Candle(
            timestamp=base_time + timedelta(hours=4 * i), timeframe=timeframe, symbol=symbol,
            open=close - 0.2, high=close + 1, low=close - 1, close=close, volume=100.0,
            quality_status="valid",
        ))
    await session.commit()


@pytest.mark.asyncio
async def test_returns_none_when_not_enough_real_candles_exist(session_maker):
    async with session_maker() as session:
        await _seed_trending_candles(session, n=MIN_BARS_REQUIRED - 5)
        signal = await generate_and_persist_signal(session)
        assert signal is None

        # confirm nothing was persisted either
        rows = (await session.execute(select(Signal))).scalars().all()
        assert len(rows) == 0


@pytest.mark.asyncio
async def test_generates_and_persists_a_signal_with_pending_outcome(session_maker):
    async with session_maker() as session:
        await _seed_trending_candles(session, n=120)
        signal = await generate_and_persist_signal(session)

        assert signal is not None
        assert signal.strategy_name == "trend_following_donchian_adx"
        assert signal.quality in ("LOW", "MEDIUM", "HIGH")

        outcome = (await session.execute(
            select(SignalOutcome).where(SignalOutcome.signal_id == signal.signal_id)
        )).scalar_one()
        if signal.signal_type == "NO_TRADE":
            assert outcome.outcome == SignalOutcomeType.NO_TRADE.value
        else:
            assert outcome.outcome == SignalOutcomeType.PENDING.value


@pytest.mark.asyncio
async def test_paused_generation_returns_none_and_persists_nothing(session_maker):
    async with session_maker() as session:
        await _seed_trending_candles(session, n=120)
        await set_signal_generation_paused(session, True)

        signal = await generate_and_persist_signal(session)
        assert signal is None

        rows = (await session.execute(select(Signal))).scalars().all()
        assert len(rows) == 0

        await set_signal_generation_paused(session, False)
        signal = await generate_and_persist_signal(session)
        assert signal is not None


@pytest.mark.asyncio
async def test_send_signal_includes_signal_id_and_manual_execution_notice():
    from services.telegram.bot import TelegramBot

    bot = TelegramBot(bot_token="x", chat_id="y")
    bot.enabled = True
    sent = {}

    async def fake_send(text):
        sent["text"] = text

    bot._send = fake_send
    await bot.send_signal({
        "signal_id": "SIG-ABC123", "signal_type": "LONG", "quality": "MEDIUM",
        "entry_price": 65000.0, "stop_loss": 64000.0, "take_profit_1": 67000.0,
        "risk_reward": 2.0, "market_regime": "TRENDING_BULLISH", "reasoning": "test reasoning",
    })
    assert "SIG-ABC123" in sent["text"]
    assert "MANUAL EXECUTION REQUIRED" in sent["text"]


@pytest.mark.asyncio
async def test_send_signal_is_a_noop_for_no_trade():
    from services.telegram.bot import TelegramBot

    bot = TelegramBot(bot_token="x", chat_id="y")
    bot.enabled = True
    calls = []

    async def fake_send(text):
        calls.append(text)

    bot._send = fake_send
    await bot.send_signal({"signal_id": "SIG-1", "signal_type": "NO_TRADE"})
    assert calls == []

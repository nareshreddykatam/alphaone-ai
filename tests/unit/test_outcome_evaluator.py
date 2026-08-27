"""Phase 4J: pending signal outcomes must resolve against REAL subsequent
candles, using the same stop-wins-ties convention as the backtester, and
must stay PENDING (not guessed) when there isn't enough data yet."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import Candle, Signal, SignalOutcome, SignalOutcomeType
from services.signal_engine.outcome_evaluator import evaluate_pending_signal_outcomes


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _add_signal(session, signal_id, signal_type, entry, sl, tp1, ts):
    session.add(Signal(signal_id=signal_id, timestamp=ts, symbol="BTC/USDT", signal_type=signal_type, confidence=0.0, entry_price=entry, stop_loss=sl, take_profit_1=tp1))
    session.add(SignalOutcome(signal_id=signal_id, outcome=SignalOutcomeType.PENDING.value))


async def _add_candle(session, ts, o, h, l, c, timeframe="4h"):
    session.add(Candle(timestamp=ts, timeframe=timeframe, symbol="BTC/USDT", open=o, high=h, low=l, close=c, volume=1.0, quality_status="valid"))


@pytest.mark.asyncio
async def test_long_signal_resolves_win_when_target_hit_first(session_maker):
    from sqlalchemy import select

    async with session_maker() as session:
        base = datetime(2026, 1, 1)
        await _add_signal(session, "S1", "LONG", 100.0, 90.0, 120.0, base)
        await _add_candle(session, base + timedelta(hours=4), 100, 121, 99, 120)  # touches TP
        await session.commit()

        updated = await evaluate_pending_signal_outcomes(session)
        assert updated == 1

        outcome = (await session.execute(select(SignalOutcome).where(SignalOutcome.signal_id == "S1"))).scalar_one()
        assert outcome.outcome == SignalOutcomeType.WIN.value
        assert outcome.hypothetical_exit_price == 120.0


@pytest.mark.asyncio
async def test_loss_when_stop_hit_before_target(session_maker):
    from sqlalchemy import select
    from database.schema.models import SignalOutcome

    async with session_maker() as session:
        base = datetime(2026, 1, 1)
        await _add_signal(session, "LOSS1", "LONG", 100.0, 90.0, 120.0, base)
        await _add_candle(session, base + timedelta(hours=4), 100, 105, 89, 95)  # stop breached
        await _add_candle(session, base + timedelta(hours=8), 95, 125, 94, 120)  # would've hit target too late
        await session.commit()

        updated = await evaluate_pending_signal_outcomes(session)
        assert updated == 1

        outcome = (await session.execute(select(SignalOutcome).where(SignalOutcome.signal_id == "LOSS1"))).scalar_one()
        assert outcome.outcome == SignalOutcomeType.LOSS.value
        assert outcome.hypothetical_pnl_pct == pytest.approx(-10.0)


@pytest.mark.asyncio
async def test_stays_pending_when_not_enough_bars_yet(session_maker):
    from sqlalchemy import select
    from database.schema.models import SignalOutcome

    async with session_maker() as session:
        base = datetime(2026, 1, 1)
        await _add_signal(session, "PEND1", "LONG", 100.0, 90.0, 120.0, base)
        await _add_candle(session, base + timedelta(hours=4), 100, 105, 98, 102)  # neither level touched
        await session.commit()

        updated = await evaluate_pending_signal_outcomes(session, max_horizon_bars=30)
        assert updated == 0

        outcome = (await session.execute(select(SignalOutcome).where(SignalOutcome.signal_id == "PEND1"))).scalar_one()
        assert outcome.outcome == SignalOutcomeType.PENDING.value


@pytest.mark.asyncio
async def test_expires_when_neither_level_touched_within_horizon(session_maker):
    from sqlalchemy import select
    from database.schema.models import SignalOutcome

    async with session_maker() as session:
        base = datetime(2026, 1, 1)
        await _add_signal(session, "EXP1", "LONG", 100.0, 50.0, 200.0, base)
        for i in range(1, 31):
            await _add_candle(session, base + timedelta(hours=4 * i), 100, 101, 99, 100)
        await session.commit()

        updated = await evaluate_pending_signal_outcomes(session, max_horizon_bars=30)
        assert updated == 1

        outcome = (await session.execute(select(SignalOutcome).where(SignalOutcome.signal_id == "EXP1"))).scalar_one()
        assert outcome.outcome == SignalOutcomeType.EXPIRED.value
        assert outcome.hypothetical_pnl_pct is None


@pytest.mark.asyncio
async def test_simultaneous_touch_resolves_as_loss_stop_wins_ties(session_maker):
    async with session_maker() as session:
        base = datetime(2026, 1, 1)
        await _add_signal(session, "S1", "LONG", 100.0, 90.0, 120.0, base)
        await _add_candle(session, base + timedelta(hours=4), 100, 125, 85, 110)  # touches both SL and TP in one bar
        await session.commit()

        await evaluate_pending_signal_outcomes(session)

        from sqlalchemy import select
        from database.schema.models import SignalOutcome
        outcome = (await session.execute(select(SignalOutcome).where(SignalOutcome.signal_id == "S1"))).scalar_one()
        assert outcome.outcome == SignalOutcomeType.LOSS.value

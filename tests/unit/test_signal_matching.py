"""Phase 4H: signal-to-trade matching by symbol+direction+time+price
proximity. Must never guess when two candidates are close -- that's an
explicit ambiguous case requiring manual confirmation."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import Signal
from services.signal_matching.matcher import find_candidate_signals, pick_confident_match


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _add_signal(session, signal_id, signal_type, entry_price, timestamp, symbol="BTC/USDT"):
    session.add(Signal(signal_id=signal_id, timestamp=timestamp, symbol=symbol, signal_type=signal_type, confidence=0.0, entry_price=entry_price))


@pytest.mark.asyncio
async def test_exact_match_is_confident(session_maker):
    async with session_maker() as session:
        base = datetime(2026, 1, 1, 12, 0)
        await _add_signal(session, "S1", "LONG", 100.0, base)
        await session.commit()

        candidates = await find_candidate_signals(session, symbol="BTC/USDT", side="LONG", entry_price=100.0, timestamp=base)
        assert len(candidates) == 1
        match = pick_confident_match(candidates)
        assert match is not None
        assert match.signal_id == "S1"
        assert match.confidence > 0.9


@pytest.mark.asyncio
async def test_wrong_direction_is_excluded(session_maker):
    async with session_maker() as session:
        base = datetime(2026, 1, 1, 12, 0)
        await _add_signal(session, "S1", "SHORT", 100.0, base)
        await session.commit()

        candidates = await find_candidate_signals(session, symbol="BTC/USDT", side="LONG", entry_price=100.0, timestamp=base)
        assert candidates == []


@pytest.mark.asyncio
async def test_price_outside_tolerance_is_excluded(session_maker):
    async with session_maker() as session:
        base = datetime(2026, 1, 1, 12, 0)
        await _add_signal(session, "S1", "LONG", 100.0, base)
        await session.commit()

        # 5% away, tolerance defaults to 2%
        candidates = await find_candidate_signals(session, symbol="BTC/USDT", side="LONG", entry_price=105.0, timestamp=base)
        assert candidates == []


@pytest.mark.asyncio
async def test_outside_time_window_is_excluded(session_maker):
    async with session_maker() as session:
        base = datetime(2026, 1, 1, 12, 0)
        await _add_signal(session, "S1", "LONG", 100.0, base)
        await session.commit()

        candidates = await find_candidate_signals(
            session, symbol="BTC/USDT", side="LONG", entry_price=100.0,
            timestamp=base + timedelta(hours=10), time_window_hours=4.0,
        )
        assert candidates == []


@pytest.mark.asyncio
async def test_two_close_candidates_are_ambiguous_not_auto_picked(session_maker):
    async with session_maker() as session:
        base = datetime(2026, 1, 1, 12, 0)
        await _add_signal(session, "S1", "LONG", 100.0, base)
        await _add_signal(session, "S2", "LONG", 100.1, base + timedelta(minutes=1))
        await session.commit()

        candidates = await find_candidate_signals(session, symbol="BTC/USDT", side="LONG", entry_price=100.05, timestamp=base)
        assert len(candidates) == 2
        assert pick_confident_match(candidates) is None  # ambiguous -- must surface both to the user


@pytest.mark.asyncio
async def test_clearly_better_candidate_wins_even_with_a_second_candidate_present(session_maker):
    async with session_maker() as session:
        base = datetime(2026, 1, 1, 12, 0)
        await _add_signal(session, "close_match", "LONG", 100.0, base)
        await _add_signal(session, "far_match", "LONG", 101.9, base + timedelta(hours=3, minutes=50))
        await session.commit()

        candidates = await find_candidate_signals(session, symbol="BTC/USDT", side="LONG", entry_price=100.0, timestamp=base)
        match = pick_confident_match(candidates)
        assert match is not None
        assert match.signal_id == "close_match"

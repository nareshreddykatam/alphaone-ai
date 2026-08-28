"""Live Futures Auto-Trading V1, Phase 15: the daily loss limit, applied
against REAL CoinDCX account equity (never a notional/fabricated figure)
using the EXISTING RiskConfig.max_daily_loss_pct (2.0%) -- preserved, not
reinvented, per this task's own instruction."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import Trade, TradeStatus
from services.exchange.base import ExchangeAccountProvider
from services.live_execution.daily_loss import check_daily_loss_limit, DEFAULT_MAX_DAILY_LOSS_PCT


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class _FakeProvider(ExchangeAccountProvider):
    def __init__(self, equity):
        self._equity = equity

    async def get_connection_status(self):
        return {"status": "OK"}

    async def get_balance(self):
        return {"total_equity": self._equity}

    async def get_open_positions(self):
        return []

    async def get_trade_history(self):
        return []


def _closed_live_trade(pnl, exit_time, trade_id="T1"):
    return Trade(
        trade_id=trade_id, symbol="BTC/USDT", side="LONG", status=TradeStatus.CLOSED.value,
        mode="live", entry_price=80000.0, exit_price=80000.0 + pnl, pnl=pnl, exit_time=exit_time,
        quantity=0.01, entry_time=exit_time,
    )


def test_default_pct_is_reused_from_the_existing_risk_engine_not_reinvented():
    assert DEFAULT_MAX_DAILY_LOSS_PCT == 2.0


async def test_no_equity_available_blocks_rather_than_assuming_safe(session_maker):
    async with session_maker() as session:
        provider = _FakeProvider(equity=None)
        result = await check_daily_loss_limit(session, provider)
        assert result.approved is False
        assert "equity is unavailable" in result.reason


async def test_approved_when_no_trades_closed_today(session_maker):
    async with session_maker() as session:
        provider = _FakeProvider(equity=100000.0)
        result = await check_daily_loss_limit(session, provider)
        assert result.approved is True
        assert result.realized_pnl_inr_today == 0.0
        assert result.account_equity_inr == 100000.0


async def test_approved_when_realized_loss_is_under_the_limit(session_maker):
    now = datetime.utcnow()
    async with session_maker() as session:
        # 2% of 100,000 == 2,000 -- a 500 loss must still pass.
        session.add(_closed_live_trade(pnl=-500.0, exit_time=now))
        await session.commit()
        provider = _FakeProvider(equity=100000.0)
        result = await check_daily_loss_limit(session, provider, now=now)
        assert result.approved is True
        assert result.realized_pnl_inr_today == -500.0


async def test_rejected_when_realized_loss_reaches_the_limit(session_maker):
    now = datetime.utcnow()
    async with session_maker() as session:
        session.add(_closed_live_trade(pnl=-2000.0, exit_time=now))
        await session.commit()
        provider = _FakeProvider(equity=100000.0)
        result = await check_daily_loss_limit(session, provider, now=now)
        assert result.approved is False
        assert "daily loss limit" in result.reason


async def test_rejected_when_realized_loss_exceeds_the_limit(session_maker):
    now = datetime.utcnow()
    async with session_maker() as session:
        session.add(_closed_live_trade(pnl=-5000.0, exit_time=now))
        await session.commit()
        provider = _FakeProvider(equity=100000.0)
        result = await check_daily_loss_limit(session, provider, now=now)
        assert result.approved is False


async def test_yesterdays_losses_do_not_count_against_todays_limit(session_maker):
    now = datetime.utcnow()
    yesterday = now - timedelta(days=1)
    async with session_maker() as session:
        session.add(_closed_live_trade(pnl=-50000.0, exit_time=yesterday))
        await session.commit()
        provider = _FakeProvider(equity=100000.0)
        result = await check_daily_loss_limit(session, provider, now=now)
        assert result.approved is True
        assert result.realized_pnl_inr_today == 0.0


async def test_paper_trade_losses_never_count_against_the_live_loss_limit(session_maker):
    """mode='paper' rows must be completely invisible to this real-money
    gate -- paper losses are not real money and must never trigger a real
    trading halt."""
    now = datetime.utcnow()
    async with session_maker() as session:
        session.add(Trade(
            trade_id="P1", symbol="BTC/USDT", side="LONG", status=TradeStatus.CLOSED.value,
            mode="paper", entry_price=80000.0, exit_price=70000.0, pnl=-50000.0, exit_time=now,
            quantity=0.01, entry_time=now,
        ))
        await session.commit()
        provider = _FakeProvider(equity=100000.0)
        result = await check_daily_loss_limit(session, provider, now=now)
        assert result.approved is True
        assert result.realized_pnl_inr_today == 0.0


async def test_open_live_trades_are_not_counted_as_realized_loss(session_maker):
    now = datetime.utcnow()
    async with session_maker() as session:
        session.add(Trade(
            trade_id="O1", symbol="BTC/USDT", side="LONG", status=TradeStatus.OPEN.value,
            mode="live", entry_price=80000.0, pnl=None, quantity=0.01, entry_time=now,
        ))
        await session.commit()
        provider = _FakeProvider(equity=100000.0)
        result = await check_daily_loss_limit(session, provider, now=now)
        assert result.approved is True
        assert result.realized_pnl_inr_today == 0.0


async def test_profitable_day_never_blocks(session_maker):
    now = datetime.utcnow()
    async with session_maker() as session:
        session.add(_closed_live_trade(pnl=5000.0, exit_time=now))
        await session.commit()
        provider = _FakeProvider(equity=100000.0)
        result = await check_daily_loss_limit(session, provider, now=now)
        assert result.approved is True
        assert result.realized_pnl_inr_today == 5000.0

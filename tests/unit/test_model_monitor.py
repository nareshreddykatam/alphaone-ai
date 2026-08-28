"""AI Trading V1: model/paper-trading health status must be computed from
real, already-persisted Trade/Prediction rows -- never a hardcoded or
fabricated status -- and only once a minimum sample exists."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import Trade, TradeSource, TradeStatus, Prediction
from services.model_monitor.monitor import evaluate_model_health, ModelHealthStatus


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _paper_trade(i, pnl, entry_time):
    return Trade(
        trade_id=f"PAPER-{i:06d}", symbol="BTC/USDT", side="LONG", status=TradeStatus.CLOSED.value,
        mode="paper", source=TradeSource.AI_PAPER.value, entry_price=100, exit_price=100 + pnl,
        quantity=1, entry_time=entry_time, exit_time=entry_time + timedelta(hours=4), pnl=pnl,
    )


async def test_too_few_trades_stays_healthy_by_default(session_maker):
    async with session_maker() as session:
        for i in range(5):
            session.add(_paper_trade(i, pnl=-50, entry_time=datetime(2026, 1, 1) + timedelta(days=i)))
        await session.commit()
        report = await evaluate_model_health(session)
        assert report.status == ModelHealthStatus.HEALTHY
        assert report.recent_profit_factor is None  # never computed on too small a sample


async def test_consistently_profitable_trades_report_healthy(session_maker):
    async with session_maker() as session:
        for i in range(20):
            pnl = 100 if i % 3 != 0 else -20  # mostly winning
            session.add(_paper_trade(i, pnl=pnl, entry_time=datetime(2026, 1, 1) + timedelta(days=i)))
        await session.commit()
        report = await evaluate_model_health(session)
        assert report.status == ModelHealthStatus.HEALTHY
        assert report.recent_profit_factor > 1.0


async def test_severe_losses_trigger_disabled_not_just_degraded(session_maker):
    async with session_maker() as session:
        for i in range(20):
            session.add(_paper_trade(i, pnl=-100, entry_time=datetime(2026, 1, 1) + timedelta(days=i)))
        await session.commit()
        report = await evaluate_model_health(session)
        assert report.status == ModelHealthStatus.DISABLED
        assert report.reasons


async def test_moderate_losses_trigger_degraded(session_maker):
    async with session_maker() as session:
        # PF around 0.6 (below DEGRADED_PF_THRESHOLD=0.7, above DISABLED_PF_THRESHOLD=0.4)
        for i in range(20):
            pnl = 60 if i % 2 == 0 else -100
            session.add(_paper_trade(i, pnl=pnl, entry_time=datetime(2026, 1, 1) + timedelta(days=i)))
        await session.commit()
        report = await evaluate_model_health(session)
        assert report.status in (ModelHealthStatus.DEGRADED, ModelHealthStatus.WARNING)


async def test_prediction_class_imbalance_flags_warning(session_maker):
    async with session_maker() as session:
        for i in range(20):
            session.add(Prediction(
                signal_id=f"SIG-{i}", timestamp=datetime(2026, 1, 1) + timedelta(hours=4 * i),
                symbol="BTC/USDT", signal_type="LONG", long_probability=0.9,
                short_probability=0.05, no_trade_probability=0.05, confidence=0.9,
                market_regime="TRENDING_BULLISH",
            ))
        await session.commit()
        report = await evaluate_model_health(session)
        assert report.prediction_class_distribution["LONG"] >= 0.9
        assert report.status != ModelHealthStatus.HEALTHY

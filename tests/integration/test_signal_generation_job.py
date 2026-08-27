"""Tests for services/scheduler/jobs.py::signal_generation_job -- now backed
by the multi-strategy orchestrator instead of a single BaselineStrategy
call, and (a real, necessary fix made alongside that change) now actually
notifies Telegram for every newly-persisted signal. Before this change,
signal_generation_job silently persisted signals with NO Telegram alert
ever sent -- harmless for S05 (which also has live_breakout_job's intrabar
path to alert from), but would have been a complete dead end for any
CLOSED_CANDLE-only production strategy (e.g. S06, which has no intrabar
detector at all).
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema import models  # noqa: F401
from database.schema.models import Candle, NotificationLog
from services.signal_engine.multi_strategy import StrategyDefinition
from services.signal_engine.strategy import SignalStrategy, StrategySignal


class _FakeStrategy(SignalStrategy):
    def __init__(self, strategy_id: str, signal_type: str):
        self.strategy_id = strategy_id
        self.name = strategy_id
        self._signal_type = signal_type

    def generate(self, df) -> StrategySignal:
        entry = float(df.iloc[-1]["close"])
        sl = entry - 100.0 if self._signal_type == "LONG" else entry + 100.0
        tp = entry + 200.0 if self._signal_type == "LONG" else entry - 200.0
        return StrategySignal(
            signal_type=self._signal_type, entry_price=entry, stop_loss=sl,
            take_profit_1=tp, take_profit_2=None, take_profit_3=None,
            quality="MEDIUM", reasoning="fake", strategy_name=self.strategy_id,
        )


def _fake_def(strategy_id: str, signal_type: str) -> StrategyDefinition:
    return StrategyDefinition(
        strategy_id=strategy_id, display_name=strategy_id, timeframe="4h",
        data_mode="CLOSED_CANDLE", production_status="PRODUCTION_ELIGIBLE",
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


async def _seed_candles(session_maker, n: int = 80):
    start = datetime(2024, 1, 1)
    async with session_maker() as session:
        for i in range(n):
            session.add(Candle(
                timestamp=start + timedelta(hours=4 * i), timeframe="4h", symbol="BTC/USDT",
                open=100.0, high=101.0, low=99.0, close=100.0 + i * 0.01, volume=10.0,
                quality_status="valid",
            ))
        await session.commit()


@pytest.mark.asyncio
async def test_signal_generation_job_notifies_telegram_for_each_new_signal(session_maker, monkeypatch):
    fake_registry = [
        _fake_def("FAKE_A_4H", "LONG"),
        _fake_def("FAKE_B_4H", "SHORT"),
    ]
    monkeypatch.setattr("services.signal_engine.multi_strategy_engine.MULTI_STRATEGY_REGISTRY", fake_registry)

    sent = []

    async def fake_send_signal(self, signal_dict):
        sent.append(signal_dict["strategy_name"])

    monkeypatch.setattr("services.telegram.bot.TelegramBot.send_signal", fake_send_signal)

    await _seed_candles(session_maker)

    from services.scheduler.jobs import signal_generation_job

    async with session_maker() as session:
        signals = await signal_generation_job(session, symbol="BTC/USDT", timeframe="4h")

    assert len(signals) == 2
    assert set(sent) == {"FAKE_A_4H", "FAKE_B_4H"}  # both independently notified, one message each


@pytest.mark.asyncio
async def test_signal_generation_job_creates_one_notification_log_row_per_signal(session_maker, monkeypatch):
    """10. Telegram messages are independently deduplicated -- two
    DIFFERENT strategies' signals must produce two SEPARATE NotificationLog
    rows (keyed by their own distinct signal_id), never sharing or
    colliding with each other's dedup key."""
    fake_registry = [_fake_def("FAKE_A_4H", "LONG"), _fake_def("FAKE_B_4H", "SHORT")]
    monkeypatch.setattr("services.signal_engine.multi_strategy_engine.MULTI_STRATEGY_REGISTRY", fake_registry)

    async def fake_send_signal(self, signal_dict):
        pass

    monkeypatch.setattr("services.telegram.bot.TelegramBot.send_signal", fake_send_signal)

    await _seed_candles(session_maker)

    from sqlalchemy import select
    from services.scheduler.jobs import signal_generation_job

    async with session_maker() as session:
        await signal_generation_job(session, symbol="BTC/USDT", timeframe="4h")

    async with session_maker() as session:
        rows = (await session.execute(select(NotificationLog))).scalars().all()
    assert len(rows) == 2
    assert len({r.signal_id for r in rows}) == 2  # distinct signal_ids, not deduped against each other

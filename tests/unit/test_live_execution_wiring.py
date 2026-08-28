"""services/live_execution/wiring.py -- the real (but gated) signal-to-
live-execution integration point (Contract Audit V2, Phase 11). The
single most important property here: with AUTOMATIC_TRADING_ENABLED at
its real production default (False), calling maybe_attempt_live_execution
must be a no-op with ZERO database writes and ZERO calls into any
live-execution or CoinDCX module -- not just "ends up rejected three
layers down", but never even starts.
"""
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from database.schema.models import ExternalSignal, ExternalSignalStatus
from services.live_execution.wiring import maybe_attempt_live_execution


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _valid_signal(**overrides):
    defaults = dict(
        message_id="00000000-0000-0000-0000-000000000000", source_channel="@suncrypto_trading_alerts",
        status=ExternalSignalStatus.VALID.value, symbol="BTC/USDT", direction="LONG",
        entry_price=80000.0, stop_loss=79000.0, take_profit_1=83000.0,
    )
    defaults.update(overrides)
    return ExternalSignal(**defaults)


async def test_disabled_by_default_returns_none_with_zero_db_writes(session_maker, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    assert settings.automatic_trading_enabled is False  # the real production default

    async with session_maker() as session:
        result = await maybe_attempt_live_execution(session, _valid_signal(), "@suncrypto_trading_alerts")
        assert result is None

        from sqlalchemy import select
        from database.schema.models import LiveExecution
        rows = (await session.execute(select(LiveExecution))).scalars().all()
        assert rows == []


async def test_disabled_makes_zero_calls_into_the_coindcx_provider_or_executor(session_maker, monkeypatch):
    """Proves the guard fires BEFORE any of the heavier imports/calls this
    module would otherwise make -- patches process_live_execution_candidate
    itself with a mock and asserts it is never even imported/called."""
    executor_mock = MagicMock()
    monkeypatch.setattr("services.live_execution.executor.process_live_execution_candidate", executor_mock)

    async with session_maker() as session:
        await maybe_attempt_live_execution(session, _valid_signal(), "@suncrypto_trading_alerts")

    executor_mock.assert_not_called()


async def test_enabled_constructs_a_candidate_and_calls_the_executor(session_maker, monkeypatch):
    """The 'enabled' branch is real code, not a stub -- this proves it
    actually gathers inputs and reaches the executor (which, per
    tests/unit/test_live_execution_safety.py, always ends REJECTED
    regardless)."""
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)

    from services.scanner.multi_coin import InstrumentAvailability

    async def fake_get_connection_status(self):
        return {"status": "OK"}

    async def fake_check_instrument_availability(symbols, client=None):
        return [InstrumentAvailability(symbol=symbols[0], instrument="B-BTC_USDT", available=True, last_price=80100.0, tick_age_seconds=1.0)]

    async def fake_get_instrument_metadata(pair, margin_currency="USDT", client=None, force_refresh=False):
        return None  # deliberately absent -- proves the executor path still runs (and rejects) even with partial real-world gaps

    async def fake_get_futures_conversion_rate(self, margin_currency="USDT"):
        return {"rate": 88.0, "margin_currency": "USDT", "target_currency": "INR", "last_updated_at": 1}

    from services.live_execution.daily_loss import DailyLossCheck

    async def fake_check_daily_loss_limit(session, provider, now=None, max_daily_loss_pct=2.0):
        return DailyLossCheck(approved=True, reason="OK")

    monkeypatch.setattr("services.exchange.coindcx.CoinDCXReadOnlyAccountProvider.get_connection_status", fake_get_connection_status)
    monkeypatch.setattr("services.exchange.coindcx.CoinDCXReadOnlyAccountProvider.get_futures_conversion_rate", fake_get_futures_conversion_rate)
    monkeypatch.setattr("services.scanner.multi_coin.check_instrument_availability", fake_check_instrument_availability)
    monkeypatch.setattr("services.exchange.coindcx_instruments.get_instrument_metadata", fake_get_instrument_metadata)
    monkeypatch.setattr("services.live_execution.daily_loss.check_daily_loss_limit", fake_check_daily_loss_limit)

    async with session_maker() as session:
        signal = _valid_signal()
        result = await maybe_attempt_live_execution(session, signal, "@suncrypto_trading_alerts")

    assert result is not None
    assert result.status == "REJECTED"  # AUTOMATIC_TRADING_ENABLED alone is never sufficient -- see gates.py
    assert result.source == "TELEGRAM_EXTERNAL"
    assert result.symbol == "BTC/USDT"

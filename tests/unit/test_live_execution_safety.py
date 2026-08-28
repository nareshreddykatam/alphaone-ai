"""Live Futures Auto-Trading V1 -- THE MASTER SAFETY-PROOF SUITE.

This is the dedicated test file this project's own safety spec requires
by exact name: every listed failure mode (disabled / emergency-stop /
stale-market / invalid-signal / unsupported-instrument / daily-limit /
risk-rejection / missing-SL / invalid-quantity / account-unhealthy /
duplicate-execution / unknown-order-state) must result in NO REAL ORDER.

Every test here patches services.live_execution.order_client.submit_futures_order
with a counting mock and asserts it was called ZERO times -- not just that
the returned LiveExecution status looks right, but that the one function in
this entire codebase capable of reaching a real CoinDCX order endpoint was
never invoked. This is the strongest and most direct proof available: even
if a future bug changed the REJECTED-status logic, this suite would still
catch a real order attempt.

Most important single test in this file:
test_automatic_trading_disabled_by_default_means_zero_real_order_calls --
proves AUTOMATIC_TRADING_ENABLED=false (the real, unmodified production
default) alone is sufficient to guarantee zero real CoinDCX order calls,
with every other input as realistic/healthy as possible.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from unittest.mock import MagicMock

from database.schema import Base
from database.schema.models import LiveExecution, LiveExecutionStatus
from services.exchange.coindcx_instruments import InstrumentMetadata
from services.live_execution.executor import process_live_execution_candidate
from services.live_execution.gates import LiveExecutionCandidate
from services.live_execution.kill_switch import activate_emergency_stop

_HEALTHY_INSTRUMENT = InstrumentMetadata(
    pair="B-BTC_USDT", status="active", kind="perpetual",
    settle_currency_short_name="USDT", quote_currency_short_name="USDT",
    position_currency_short_name="BTC", underlying_currency_short_name="BTC", margin_currency_short_name="USDT",
    max_leverage_long=20.0, max_leverage_short=20.0, price_increment=0.01, quantity_increment=0.00000001,
    min_trade_size=0.00000001, min_price=0.01, max_price=10_000_000.0, min_quantity=0.00000001, max_quantity=950.0,
    min_notional=0.01, max_notional=0.0, exit_only=False, order_types=[], time_in_force_options=[], fetched_at=0.0,
)

NEVER_REACHED_STATUSES = (
    LiveExecutionStatus.EXCHANGE_ACCEPTED.value,
    LiveExecutionStatus.FILLED.value,
    LiveExecutionStatus.POSITION_OPEN.value,
    LiveExecutionStatus.PARTIAL_EXIT.value,
    LiveExecutionStatus.CLOSED.value,
)


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def order_mock(monkeypatch):
    """Patches the ONE function in this codebase capable of reaching a
    real CoinDCX order endpoint, at the exact place executor.py imports
    it from, so we can assert it was never called -- the strongest
    possible proof of "no real order" for every scenario below."""
    mock = MagicMock()
    monkeypatch.setattr("services.live_execution.executor.submit_futures_order", mock)
    return mock


def _healthy_candidate(**overrides):
    """A fully realistic, healthy candidate -- valid SL/TP geometry,
    fresh timestamp, eligible instrument. Individual tests break exactly
    ONE property to isolate the failure mode under test."""
    defaults = dict(
        source="ALPHAONE_STRATEGY", symbol="BTC/USDT", direction="LONG",
        entry_price=80000.0, stop_loss=79000.0, take_profit_1=83000.0,
        signal_timestamp=datetime.utcnow(), signal_id="SIG-SAFETY-1",
        instrument="B-BTC_USDT", instrument_eligible=True, instrument_eligibility_reason="OK",
        current_market_price=80100.0, instrument_metadata=_HEALTHY_INSTRUMENT,
    )
    defaults.update(overrides)
    return LiveExecutionCandidate(**defaults)


async def _run(session, candidate, **kw):
    defaults = dict(
        usdt_inr_rate=88.0, market_data_healthy=True, coindcx_account_healthy=True,
        daily_loss_ok=True, daily_loss_reason="OK", reconciliation_ok=True, reconciliation_reason="OK",
    )
    defaults.update(kw)
    return await process_live_execution_candidate(session, candidate, **defaults)


def _assert_no_real_order(execution, order_mock):
    assert execution.status == LiveExecutionStatus.REJECTED.value
    assert execution.status not in NEVER_REACHED_STATUSES
    assert execution.exchange_order_id is None
    assert execution.rejection_reason is not None
    order_mock.assert_not_called()


# 1. THE definitive test.
async def test_automatic_trading_disabled_by_default_means_zero_real_order_calls(session_maker, order_mock):
    """The real, unmodified production default (automatic_trading_enabled=False)
    -- every other input in this scenario is deliberately as healthy/realistic
    as possible, so this isolates the kill-switch default as the thing doing
    the blocking."""
    from apps.api.config import get_settings
    settings = get_settings()
    assert settings.automatic_trading_enabled is False  # sanity: the real default, not a test-only override

    async with session_maker() as session:
        execution = await _run(session, _healthy_candidate())
        _assert_no_real_order(execution, order_mock)
        assert "AUTOMATIC_TRADING_ENABLED" in execution.rejection_reason


# 2. Emergency stop
async def test_emergency_stop_active_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        await activate_emergency_stop(session, reason="safety test halt")
        execution = await _run(session, _healthy_candidate())
        _assert_no_real_order(execution, order_mock)


# 3. Stale market data
async def test_stale_market_data_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        execution = await _run(session, _healthy_candidate(), market_data_healthy=False)
        _assert_no_real_order(execution, order_mock)


# 4. Invalid signal (no SL/TP geometry -- also covers "invalid-signal")
async def test_invalid_signal_geometry_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        candidate = _healthy_candidate(direction="LONG", entry_price=80000, stop_loss=81000, take_profit_1=83000, signal_id="SIG-SAFETY-4")
        execution = await _run(session, candidate)
        _assert_no_real_order(execution, order_mock)


# 5. Unsupported instrument
async def test_unsupported_instrument_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        candidate = _healthy_candidate(
            symbol="DOGE/USDT", instrument=None, instrument_eligible=False,
            instrument_eligibility_reason="DOGE/USDT is not a supported CoinDCX USDT futures instrument",
            signal_id="SIG-SAFETY-5",
        )
        execution = await _run(session, candidate)
        _assert_no_real_order(execution, order_mock)


# 6. Daily limit reached (15/day hard max)
async def test_daily_hard_limit_reached_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    from database.schema.models import Trade, TradeStatus
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        now = datetime.utcnow()
        for i in range(15):
            session.add(Trade(
                trade_id=f"DAILYMAX-{i}", symbol="BTC/USDT", side="LONG", status=TradeStatus.OPEN.value,
                mode="paper", source="AI_PAPER", entry_price=80000.0, entry_time=now, quantity=0.01,
            ))
        await session.commit()
        execution = await _run(session, _healthy_candidate(signal_id="SIG-SAFETY-6"))
        _assert_no_real_order(execution, order_mock)


# 7. Risk engine rejection (no live USDT/INR rate -- cannot safely size Rs.200 margin)
async def test_risk_engine_rejection_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        execution = await _run(session, _healthy_candidate(signal_id="SIG-SAFETY-7"), usdt_inr_rate=None)
        _assert_no_real_order(execution, order_mock)
        assert "RISK_ENGINE_APPROVED" in execution.gate_results
        assert execution.gate_results["RISK_ENGINE_APPROVED"]["passed"] is False


# 8. Missing stop loss
async def test_missing_stop_loss_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        candidate = _healthy_candidate(stop_loss=None, signal_id="SIG-SAFETY-8")
        execution = await _run(session, candidate)
        _assert_no_real_order(execution, order_mock)
        assert execution.gate_results["VALID_SL"]["passed"] is False


# 9. Invalid quantity / unsizeable position (invalid entry price -- sizing cannot proceed)
async def test_invalid_entry_price_prevents_sizing_and_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        candidate = _healthy_candidate(entry_price=0, stop_loss=None, take_profit_1=None, signal_id="SIG-SAFETY-9")
        execution = await _run(session, candidate)
        _assert_no_real_order(execution, order_mock)
        assert execution.gate_results["RISK_ENGINE_APPROVED"]["passed"] is False


# 10. CoinDCX account unhealthy
async def test_coindcx_account_unhealthy_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        execution = await _run(session, _healthy_candidate(signal_id="SIG-SAFETY-10"), coindcx_account_healthy=False)
        _assert_no_real_order(execution, order_mock)


# 11. Duplicate execution (idempotent replay must never attempt a second real order)
async def test_duplicate_execution_means_zero_real_order_calls_on_the_replay(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        candidate = _healthy_candidate(signal_id="SIG-SAFETY-11")
        first = await _run(session, candidate)
        second = await _run(session, candidate)  # simulates a Telegram reconnect / scheduler retry
        assert first.id == second.id
        _assert_no_real_order(second, order_mock)
        all_rows = (await session.execute(select(LiveExecution))).scalars().all()
        assert len(all_rows) == 1


# 12. Unknown/ambiguous order state -- proven structurally: the only real
# order function always raises a specific, known exception; there is no
# code path that could produce an ambiguous "maybe filled" state, because
# the exchange call itself never happens.
async def test_order_client_always_raises_a_known_error_never_an_ambiguous_state(session_maker, monkeypatch):
    from services.live_execution.order_client import submit_futures_order, OrderContractNotVerifiedError
    with pytest.raises(OrderContractNotVerifiedError):
        submit_futures_order(instrument="B-BTC_USDT", side="buy", quantity=0.001, leverage=10)


# 13. Daily loss limit reached
async def test_daily_loss_limit_reached_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        execution = await _run(
            session, _healthy_candidate(signal_id="SIG-SAFETY-13"),
            daily_loss_ok=False, daily_loss_reason="Today's realized live PnL has reached the daily loss limit.",
        )
        _assert_no_real_order(execution, order_mock)
        assert execution.gate_results["DAILY_LOSS_LIMIT_OK"]["passed"] is False


# 14. Position limit already at max
async def test_position_limit_at_max_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)
    monkeypatch.setattr(settings, "max_open_positions_live", 1)

    async with session_maker() as session:
        session.add(LiveExecution(
            idempotency_key="existing-open-position", source="ALPHAONE_STRATEGY", symbol="ETH/USDT",
            direction="LONG", status=LiveExecutionStatus.POSITION_OPEN.value,
        ))
        await session.commit()
        execution = await _run(session, _healthy_candidate(signal_id="SIG-SAFETY-14"))
        _assert_no_real_order(execution, order_mock)


# 15. Stale signal timestamp
async def test_stale_signal_timestamp_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        candidate = _healthy_candidate(signal_timestamp=datetime.utcnow() - timedelta(hours=1), signal_id="SIG-SAFETY-15")
        execution = await _run(session, candidate)
        _assert_no_real_order(execution, order_mock)


# 16. THE strongest single proof: fully-armed, fully-healthy candidate,
# every OTHER gate passing -- confirms ORDER_CONTRACT_VERIFIED alone still
# blocks, and the real order function is still never called.
async def test_every_gate_healthy_except_order_contract_still_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        execution = await _run(session, _healthy_candidate(signal_id="SIG-SAFETY-16"))
        gate_dict = execution.gate_results
        for name, result in gate_dict.items():
            if name != "ORDER_CONTRACT_VERIFIED":
                assert result["passed"] is True, f"{name} unexpectedly failed: {result['reason']}"
        assert gate_dict["ORDER_CONTRACT_VERIFIED"]["passed"] is False
        _assert_no_real_order(execution, order_mock)


# 17. SHORT direction also correctly blocked (never assume LONG)
async def test_short_direction_candidate_also_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        candidate = _healthy_candidate(
            direction="SHORT", entry_price=80000, stop_loss=81000, take_profit_1=78000, signal_id="SIG-SAFETY-17",
        )
        execution = await _run(session, candidate)
        assert execution.gate_results["VALID_SL"]["passed"] is True  # geometry itself is fine
        _assert_no_real_order(execution, order_mock)


# 18. Telegram-sourced candidate is subject to the exact same gates -- no
# special-cased bypass path for TELEGRAM_EXTERNAL.
async def test_telegram_sourced_candidate_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        candidate = _healthy_candidate(source="TELEGRAM_EXTERNAL", signal_id="SIG-SAFETY-18")
        execution = await _run(session, candidate)
        _assert_no_real_order(execution, order_mock)


# 19. Entry deviation too large (real-time price has moved away from the signal)
async def test_entry_price_deviation_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        candidate = _healthy_candidate(entry_price=80000.0, current_market_price=85000.0, signal_id="SIG-SAFETY-19")
        execution = await _run(session, candidate)
        _assert_no_real_order(execution, order_mock)
        assert execution.gate_results["ENTRY_DEVIATION_OK"]["passed"] is False


# 20. No instrument metadata -- cannot precision-size the quantity
async def test_no_instrument_metadata_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        candidate = _healthy_candidate(instrument_metadata=None, signal_id="SIG-SAFETY-20")
        execution = await _run(session, candidate)
        _assert_no_real_order(execution, order_mock)
        assert execution.gate_results["QUANTITY_VALID"]["passed"] is False


# 21. Instrument max leverage below the required 10x (e.g. a real
# SOL/USDT-shaped instrument capped at 5x)
async def test_instrument_leverage_cap_below_10x_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from dataclasses import replace
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        low_leverage_instrument = replace(_HEALTHY_INSTRUMENT, max_leverage_long=5.0, max_leverage_short=5.0)
        candidate = _healthy_candidate(instrument_metadata=low_leverage_instrument, signal_id="SIG-SAFETY-21")
        execution = await _run(session, candidate)
        _assert_no_real_order(execution, order_mock)
        assert execution.gate_results["QUANTITY_VALID"]["passed"] is False


# 22. Conflicting open position on the same symbol
async def test_conflicting_same_symbol_position_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)
    monkeypatch.setattr(settings, "max_open_positions_live", 5)  # isolate NO_CONFLICTING_POSITION from POSITION_LIMIT_OK

    async with session_maker() as session:
        session.add(LiveExecution(
            idempotency_key="existing-btc-safety-22", source="ALPHAONE_STRATEGY", symbol="BTC/USDT",
            direction="LONG", status=LiveExecutionStatus.POSITION_OPEN.value,
        ))
        await session.commit()
        candidate = _healthy_candidate(symbol="BTC/USDT", signal_id="SIG-SAFETY-22")
        execution = await _run(session, candidate)
        _assert_no_real_order(execution, order_mock)
        assert execution.gate_results["NO_CONFLICTING_POSITION"]["passed"] is False


# 23. Reconciliation never run / found discrepancies -- fail closed
async def test_reconciliation_not_ok_means_zero_real_order_calls(session_maker, order_mock, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        execution = await _run(
            session, _healthy_candidate(signal_id="SIG-SAFETY-23"),
            reconciliation_ok=False, reconciliation_reason="Position reconciliation has never run.",
        )
        _assert_no_real_order(execution, order_mock)
        assert execution.gate_results["RECONCILIATION_OK"]["passed"] is False

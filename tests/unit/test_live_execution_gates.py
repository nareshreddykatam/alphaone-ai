"""Live Futures Auto-Trading V1: the multi-gate safety check. Every gate
is evaluated and recorded even after an earlier one fails -- these tests
prove each gate independently, and that approval requires ALL of them."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from database.schema import Base
from services.live_execution.gates import (
    LiveExecutionCandidate, check_all_live_execution_gates, validate_sl_tp_structure,
    ORDER_CONTRACT_VERIFIED,
)


@pytest.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _candidate(**overrides):
    defaults = dict(
        source="ALPHAONE_STRATEGY", symbol="BTC/USDT", direction="LONG",
        entry_price=80000.0, stop_loss=79000.0, take_profit_1=83000.0,
        signal_timestamp=datetime.utcnow(), signal_id="SIG-1",
        instrument="B-BTC_USDT", instrument_eligible=True, instrument_eligibility_reason="OK",
    )
    defaults.update(overrides)
    return LiveExecutionCandidate(**defaults)


async def _run_gates(session, candidate, **kw):
    defaults = dict(usdt_inr_rate=88.0, market_data_healthy=True, coindcx_account_healthy=True, daily_loss_ok=True, daily_loss_reason="OK")
    defaults.update(kw)
    return await check_all_live_execution_gates(session, candidate, **defaults)


def test_order_contract_verified_is_permanently_false():
    """The core safety invariant of this entire system today."""
    assert ORDER_CONTRACT_VERIFIED is False


async def test_never_approved_even_with_every_other_gate_passing(session_maker, monkeypatch):
    """With automatic trading + arming ALSO enabled, approval must still
    be False, because ORDER_CONTRACT_VERIFIED can never pass."""
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    monkeypatch.setattr(settings, "live_execution_armed", True)

    async with session_maker() as session:
        report = await _run_gates(session, _candidate())
        assert report.approved is False
        gate_dict = report.as_dict()
        assert gate_dict["ORDER_CONTRACT_VERIFIED"]["passed"] is False
        # every OTHER gate should have passed in this fully-healthy scenario
        for name, result in gate_dict.items():
            if name != "ORDER_CONTRACT_VERIFIED":
                assert result["passed"] is True, f"{name} unexpectedly failed: {result['reason']}"


async def test_disabled_by_default(session_maker):
    async with session_maker() as session:
        report = await _run_gates(session, _candidate())
        assert report.approved is False
        assert report.as_dict()["AUTOMATIC_TRADING_ENABLED"]["passed"] is False


async def test_armed_alone_is_not_sufficient(session_maker, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "live_execution_armed", True)
    # automatic_trading_enabled left False
    async with session_maker() as session:
        report = await _run_gates(session, _candidate())
        assert report.approved is False


async def test_enabled_alone_is_not_sufficient(session_maker, monkeypatch):
    from apps.api.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "automatic_trading_enabled", True)
    # live_execution_armed left False
    async with session_maker() as session:
        report = await _run_gates(session, _candidate())
        assert report.approved is False


async def test_emergency_stop_blocks_new_entries(session_maker):
    from services.live_execution.kill_switch import activate_emergency_stop
    async with session_maker() as session:
        await activate_emergency_stop(session, reason="test halt")
        report = await _run_gates(session, _candidate())
        assert report.as_dict()["EMERGENCY_STOP_CLEAR"]["passed"] is False


async def test_market_data_unhealthy_blocks(session_maker):
    async with session_maker() as session:
        report = await _run_gates(session, _candidate(), market_data_healthy=False)
        assert report.as_dict()["MARKET_DATA_HEALTHY"]["passed"] is False


async def test_coindcx_account_unhealthy_blocks(session_maker):
    async with session_maker() as session:
        report = await _run_gates(session, _candidate(), coindcx_account_healthy=False)
        assert report.as_dict()["COINDCX_ACCOUNT_HEALTHY"]["passed"] is False


async def test_unsupported_instrument_blocks(session_maker):
    async with session_maker() as session:
        candidate = _candidate(instrument_eligible=False, instrument_eligibility_reason="DOGE/USDT not in whitelist")
        report = await _run_gates(session, candidate)
        assert report.as_dict()["INSTRUMENT_ELIGIBLE"]["passed"] is False


async def test_stale_signal_blocks(session_maker):
    async with session_maker() as session:
        candidate = _candidate(signal_timestamp=datetime.utcnow() - timedelta(minutes=30))
        report = await _run_gates(session, candidate)
        assert report.as_dict()["SIGNAL_FRESH"]["passed"] is False


async def test_missing_signal_timestamp_blocks(session_maker):
    async with session_maker() as session:
        candidate = _candidate(signal_timestamp=None)
        report = await _run_gates(session, candidate)
        assert report.as_dict()["SIGNAL_FRESH"]["passed"] is False


async def test_missing_sl_blocks(session_maker):
    async with session_maker() as session:
        candidate = _candidate(stop_loss=None)
        report = await _run_gates(session, candidate)
        assert report.as_dict()["VALID_SL"]["passed"] is False


async def test_missing_tp_blocks(session_maker):
    async with session_maker() as session:
        candidate = _candidate(take_profit_1=None)
        report = await _run_gates(session, candidate)
        assert report.as_dict()["VALID_TP"]["passed"] is False


async def test_invalid_long_sl_geometry_blocks(session_maker):
    async with session_maker() as session:
        candidate = _candidate(direction="LONG", entry_price=80000, stop_loss=81000, take_profit_1=83000)
        report = await _run_gates(session, candidate)
        assert report.as_dict()["VALID_SL"]["passed"] is False


async def test_invalid_short_sl_geometry_blocks(session_maker):
    async with session_maker() as session:
        candidate = _candidate(direction="SHORT", entry_price=80000, stop_loss=79000, take_profit_1=78000)
        report = await _run_gates(session, candidate)
        assert report.as_dict()["VALID_SL"]["passed"] is False


async def test_short_direction_valid_geometry_passes_sl_tp_gates(session_maker):
    async with session_maker() as session:
        candidate = _candidate(direction="SHORT", entry_price=80000, stop_loss=81000, take_profit_1=78000)
        report = await _run_gates(session, candidate)
        d = report.as_dict()
        assert d["VALID_SL"]["passed"] is True
        assert d["VALID_TP"]["passed"] is True


async def test_no_live_inr_rate_blocks_risk_engine_gate(session_maker):
    async with session_maker() as session:
        report = await _run_gates(session, _candidate(), usdt_inr_rate=None)
        assert report.as_dict()["RISK_ENGINE_APPROVED"]["passed"] is False


async def test_position_limit_ok_when_under_max(session_maker):
    async with session_maker() as session:
        report = await _run_gates(session, _candidate())
        assert report.as_dict()["POSITION_LIMIT_OK"]["passed"] is True


async def test_position_limit_blocks_when_at_max(session_maker, monkeypatch):
    from apps.api.config import get_settings
    from database.schema.models import LiveExecution, LiveExecutionStatus
    settings = get_settings()
    monkeypatch.setattr(settings, "max_open_positions_live", 1)

    async with session_maker() as session:
        session.add(LiveExecution(
            idempotency_key="k1", source="ALPHAONE_STRATEGY", symbol="ETH/USDT", direction="LONG",
            status=LiveExecutionStatus.POSITION_OPEN.value,
        ))
        await session.commit()
        report = await _run_gates(session, _candidate())
        assert report.as_dict()["POSITION_LIMIT_OK"]["passed"] is False


async def test_daily_loss_gate_reflects_passed_in_value(session_maker):
    async with session_maker() as session:
        report = await _run_gates(session, _candidate(), daily_loss_ok=False, daily_loss_reason="down 3% today")
        d = report.as_dict()
        assert d["DAILY_LOSS_LIMIT_OK"]["passed"] is False
        assert d["DAILY_LOSS_LIMIT_OK"]["reason"] == "down 3% today"


def test_validate_sl_tp_structure_unknown_direction():
    ok, reason = validate_sl_tp_structure("SIDEWAYS", 100, 90, 110)
    assert ok is False
    assert "Unknown direction" in reason


def test_first_failure_reason_reports_the_first_failing_gate_in_order():
    from services.live_execution.gates import GateReport, GateResult
    report = GateReport(approved=False, results=[
        GateResult("A", True, "OK"), GateResult("B", False, "nope"), GateResult("C", False, "also nope"),
    ])
    assert report.first_failure_reason() == "B: nope"

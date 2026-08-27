import pytest
from services.risk_engine.engine import RiskEngine, RiskConfig


@pytest.fixture
def risk_engine():
    config = RiskConfig(
        risk_per_trade_pct=0.5,
        max_daily_loss_pct=2.0,
        max_drawdown_pct=10.0,
        max_leverage=5,
        max_positions=1,
        max_daily_trades=10,
    )
    return RiskEngine(config, initial_equity=10000)


def test_can_trade_initial(risk_engine):
    can, reason = risk_engine.can_trade()
    assert can is True
    assert reason == "OK"


def test_kill_switch_blocks_trades(risk_engine):
    risk_engine.activate_kill_switch()
    can, reason = risk_engine.can_trade()
    assert can is False
    assert "Kill switch" in reason


def test_max_positions_blocks(risk_engine):
    risk_engine.state.positions_open = 1
    can, reason = risk_engine.can_trade()
    assert can is False
    assert "Max positions" in reason


def test_position_size_calculation(risk_engine):
    size = risk_engine.calculate_position_size(entry_price=42000, stop_loss=41500, leverage=1)
    assert size > 0
    expected = (10000 * 0.005) / abs(42000 - 41500)
    assert size == pytest.approx(expected, rel=0.01)


def test_validate_trade_valid(risk_engine):
    valid, reason, qty = risk_engine.validate_trade(
        entry_price=42000, stop_loss=41500, take_profit=43000, side="LONG"
    )
    assert valid is True
    assert qty > 0


def test_validate_trade_low_rr(risk_engine):
    valid, reason, qty = risk_engine.validate_trade(
        entry_price=42000, stop_loss=41500, take_profit=42100, side="LONG"
    )
    assert valid is False
    assert "Risk/reward" in reason


def test_drawdown_activation(risk_engine):
    risk_engine.record_trade_result(-0.3)
    risk_engine.record_trade_result(0.1)
    risk_engine.record_trade_result(-0.3)
    can, _ = risk_engine.can_trade()
    assert can is True

    risk_engine.record_trade_result(-1.5)
    can, reason = risk_engine.can_trade()
    assert can is False
    assert "kill switch" in reason.lower() or "daily" in reason.lower()


def test_consecutive_loss_cooldown(risk_engine):
    for _ in range(3):
        risk_engine.record_trade_result(-0.1)
    can, reason = risk_engine.can_trade()
    assert can is False
    assert "Cooldown" in reason


def test_get_status(risk_engine):
    status = risk_engine.get_status()
    assert "risk_per_trade_pct" in status
    assert "current_drawdown_pct" in status
    assert "kill_switch_active" in status

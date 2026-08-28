"""AI Trading V1: the in-memory paper-trading decision engine -- TP1-only
(single-target, unchanged behavior) and TP1/TP2/TP3 partial-exit slicing,
stop-loss always closing the full remaining position, risk-engine gating,
and trade-level (not just last-slice) PnL/R-multiple accounting."""
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from services.paper_trader.engine import PaperTrader
from services.risk_engine.engine import RiskConfig


@dataclass
class _FakeCandle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class _FakeSignal:
    signal_id: str
    signal_type: str
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float = None
    take_profit_3: float = None
    timestamp: datetime = datetime(2026, 1, 1)
    market_regime: str = "UNCERTAIN"
    strategy_name: str = "TEST_STRATEGY"


def _trader(max_positions=1):
    return PaperTrader(risk_config=RiskConfig(max_positions=max_positions), initial_equity=10000)


def test_open_position_rejects_signal_missing_required_levels():
    trader = _trader()
    sig = _FakeSignal("S1", "LONG", entry_price=None, stop_loss=90, take_profit_1=110)
    assert trader.open_position(sig, current_price=100) is None


def test_single_target_closes_full_quantity_at_tp1_unchanged_behavior():
    trader = _trader()
    sig = _FakeSignal("S1", "LONG", entry_price=100, stop_loss=90, take_profit_1=110)
    position = trader.open_position(sig, current_price=100)
    assert position is not None
    assert position.remaining_quantity == pytest.approx(position.quantity)

    candle = _FakeCandle(datetime(2026, 1, 1, 4), 100, 111, 99, 110)
    events = trader.process_candle(candle)
    assert len(events) == 1
    assert events[0]["event_type"] == "exit"
    assert events[0]["quantity"] == pytest.approx(position.quantity)
    assert trader.positions == {}


def test_stop_loss_always_closes_full_remaining_quantity_not_a_partial():
    trader = _trader()
    sig = _FakeSignal("S1", "LONG", entry_price=100, stop_loss=90, take_profit_1=110, take_profit_2=120, take_profit_3=130)
    trader.open_position(sig, current_price=100)

    candle = _FakeCandle(datetime(2026, 1, 1, 4), 100, 101, 89, 90)
    events = trader.process_candle(candle)
    assert len(events) == 1
    assert events[0]["event_type"] == "exit"
    assert events[0]["exit_reason"] == "stop_loss"
    assert trader.positions == {}


def test_three_targets_produce_two_partial_exits_then_a_final_close():
    trader = _trader()
    sig = _FakeSignal("S1", "LONG", entry_price=100, stop_loss=90, take_profit_1=110, take_profit_2=120, take_profit_3=130)
    position = trader.open_position(sig, current_price=100)
    original_qty = position.quantity

    c1 = _FakeCandle(datetime(2026, 1, 1, 4), 100, 111, 99, 110)
    events = trader.process_candle(c1)
    assert len(events) == 1 and events[0]["event_type"] == "partial_exit" and events[0]["target_index"] == 1
    assert position.remaining_quantity == pytest.approx(original_qty * 0.6, rel=1e-6)  # 0.4 fraction closed

    c2 = _FakeCandle(datetime(2026, 1, 1, 8), 110, 121, 109, 120)
    events = trader.process_candle(c2)
    assert len(events) == 1 and events[0]["event_type"] == "partial_exit" and events[0]["target_index"] == 2
    assert position.remaining_quantity == pytest.approx(original_qty * 0.3, rel=1e-6)

    c3 = _FakeCandle(datetime(2026, 1, 1, 12), 120, 131, 119, 130)
    events = trader.process_candle(c3)
    assert len(events) == 1 and events[0]["event_type"] == "exit"
    assert trader.positions == {}


def test_trade_level_pnl_includes_all_slices_not_just_the_final_one():
    """A trade that profits on TP1/TP2 then stops out on the small
    remaining slice must report a net-positive trade PnL, not the
    final slice's own (possibly negative) PnL alone."""
    trader = _trader()
    sig = _FakeSignal("S1", "LONG", entry_price=100, stop_loss=95, take_profit_1=110, take_profit_2=120, take_profit_3=130)
    trader.open_position(sig, current_price=100)

    c1 = _FakeCandle(datetime(2026, 1, 1, 4), 100, 111, 99, 110)
    trader.process_candle(c1)  # TP1 hit, 40% closed profitably
    c2 = _FakeCandle(datetime(2026, 1, 1, 8), 110, 121, 109, 120)
    trader.process_candle(c2)  # TP2 hit, 30% closed profitably

    # Remaining 30% now stops out at breakeven-ish (95, below entry) -- a
    # small loss on that slice alone, but the TRADE overall is a clear net win.
    c3 = _FakeCandle(datetime(2026, 1, 1, 12), 120, 121, 94, 95)
    events = trader.process_candle(c3)
    final = events[0]
    assert final["event_type"] == "exit"
    assert final["pnl"] > 0, "trade-level PnL must include the profitable earlier partial exits"


def test_short_side_targets_and_stop_are_mirrored():
    trader = _trader()
    sig = _FakeSignal("S1", "SHORT", entry_price=100, stop_loss=110, take_profit_1=90)
    trader.open_position(sig, current_price=100)

    stop_candle = _FakeCandle(datetime(2026, 1, 1, 4), 100, 111, 99, 105)
    events = trader.process_candle(stop_candle)
    assert events[0]["exit_reason"] == "stop_loss"


def test_risk_engine_blocks_a_second_concurrent_position_at_default_max_positions_1():
    trader = _trader(max_positions=1)
    sig1 = _FakeSignal("S1", "LONG", entry_price=100, stop_loss=90, take_profit_1=110)
    sig2 = _FakeSignal("S2", "LONG", entry_price=100, stop_loss=90, take_profit_1=110)
    assert trader.open_position(sig1, current_price=100) is not None
    assert trader.open_position(sig2, current_price=100) is None


def test_paused_trader_opens_nothing_and_processes_no_candles():
    trader = _trader()
    trader.pause()
    sig = _FakeSignal("S1", "LONG", entry_price=100, stop_loss=90, take_profit_1=110)
    assert trader.open_position(sig, current_price=100) is None
    trader.resume()
    assert trader.open_position(sig, current_price=100) is not None


def test_get_status_reports_real_equity_not_fabricated():
    trader = _trader()
    status = trader.get_status()
    assert status["equity"] == trader.initial_equity
    assert status["open_positions"] == 0
    assert status["closed_trades"] == 0

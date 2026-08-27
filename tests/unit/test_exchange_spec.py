from services.backtester.exchange_spec import ExchangeSpec
from services.backtester.engine import BacktestConfig


def test_default_spec_has_documented_research_values():
    spec = ExchangeSpec()
    assert spec.taker_fee == 0.0004
    assert spec.maker_fee == 0.0002
    assert spec.funding_interval_hours == 8


def test_slippage_and_spread_rate_conversion():
    spec = ExchangeSpec(slippage_bps=25, spread_bps=10)
    assert spec.slippage_rate == 0.0025
    assert spec.spread_rate == 0.001


def test_round_qty_enforces_min_qty():
    spec = ExchangeSpec(qty_precision=3, min_qty=0.01)
    assert spec.round_qty(0.0001) == 0.0
    assert spec.round_qty(0.5) == 0.5


def test_round_price_snaps_to_tick_size():
    spec = ExchangeSpec(tick_size=0.5)
    assert spec.round_price(100.3) == 100.5
    assert spec.round_price(100.1) == 100.0


def test_backtest_config_exposes_backward_compatible_aliases():
    config = BacktestConfig(exchange_spec=ExchangeSpec(taker_fee=0.001, slippage_bps=5, funding_interval_hours=4))
    assert config.fee_rate == 0.001
    assert config.slippage_rate == 0.0005
    assert config.funding_interval_hours == 4

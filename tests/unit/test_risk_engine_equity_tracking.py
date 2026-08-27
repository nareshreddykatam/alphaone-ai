"""Phase 2.6: the risk engine's notion of equity/drawdown must track the
REAL dollar equity curve, not each trade's return-on-its-own-notional.

Found while gathering Phase 2.6 lockout statistics: `Backtester.run()` used
to pass `trade.pnl_pct` (pnl / (entry_price * quantity), i.e. percent
return on the TRADE's notional) straight into
`RiskEngine.record_trade_result()`, which then does
`current_equity *= (1 + pnl_pct / 100)` as if that percentage applied to
the WHOLE ACCOUNT. That's only correct for a trade that risks 100% of
equity (position_pct=1.0, e.g. buy-and-hold). Any risk-based-sized trade
(the normal case, via `calculate_position_size`, which caps risk at a small
fraction of equity per trade) has `pnl_pct` far larger in magnitude than
its actual equity impact -- so the risk engine's drawdown/daily-loss
tracking was wildly overstated relative to the real equity curve, AND
(since `calculate_position_size` reads `risk_engine.state.current_equity`)
every subsequent trade's position size was computed off a progressively
wrong number.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from services.backtester.engine import Backtester, BacktestConfig
from services.backtester.exchange_spec import ExchangeSpec
from services.risk_engine.engine import RiskConfig


def _bars(specs, start=datetime(2024, 1, 1)):
    rows = []
    for i, (o, h, l, c) in enumerate(specs):
        rows.append({"timestamp": start + timedelta(hours=i), "open": o, "high": h, "low": l, "close": c})
    return pd.DataFrame(rows)


def test_risk_engine_equity_matches_real_dollar_equity_for_a_risk_sized_trade():
    """A trade sized to risk only 0.5% of equity (risk_per_trade_pct=0.5)
    that moves 10% against its stop should cost the ACCOUNT roughly 0.5%,
    not 10%. The risk engine's internal equity must reflect the former."""
    df = _bars([
        (100, 101, 99, 100),      # bar 0 (unused, loop starts at i=1)
        (100, 101, 99, 100),      # signal bar
        (100, 101, 99, 100),      # fill bar: entry = 100
        (99, 99.5, 90, 91),       # SL=90 hit (10% adverse move from entry)
    ])
    fired = {"done": False}

    def signal_func(d):
        if len(d) == 2 and not fired["done"]:
            fired["done"] = True
            # stop is 10% below entry -- a real, meaningfully-sized stop
            return {"signal_type": "LONG", "stop_loss": 90, "take_profit_1": 500, "leverage": 1}
        return None

    risk_config = RiskConfig(risk_per_trade_pct=0.5, max_daily_loss_pct=50, max_drawdown_pct=50)
    config = BacktestConfig(
        initial_capital=10000,
        exchange_spec=ExchangeSpec(taker_fee=0, slippage_bps=0),
        funding_rate_avg=0.0,
        risk_config=risk_config,
    )
    bt = Backtester(config)
    result = bt.run(df, signal_func)

    trade = result.trades[0]
    assert trade.pnl_pct < -9.0, "the trade's own return-on-notional should be roughly -10% (sanity on the fixture)"

    real_equity_pct_change = (trade.pnl / 10000) * 100
    assert abs(real_equity_pct_change) < 1.0, (
        f"a 0.5%-risk trade should cost the account under 1%, got {real_equity_pct_change:.2f}%"
    )

    # The risk engine's tracked drawdown/daily-loss must reflect the REAL
    # account-level impact, not the trade's -10% notional return.
    assert bt.risk_engine.state.current_drawdown_pct < 1.0, (
        f"risk engine drawdown should track real equity impact (<1%), "
        f"got {bt.risk_engine.state.current_drawdown_pct:.2f}% -- suggests notional-relative "
        f"pnl_pct is being used instead of equity-relative"
    )
    assert bt.risk_engine.state.daily_pnl_pct == pytest.approx(real_equity_pct_change, abs=0.05)


def test_risk_engine_current_equity_tracks_backtester_equity_curve():
    """After several risk-sized trades, the risk engine's `current_equity`
    should match the backtester's own equity curve (both derived from the
    same real dollar P&L), not diverge from it."""
    rows = [(100, 101, 99, 100)]
    price = 100.0
    import random
    rng = random.Random(7)
    for _ in range(40):
        move = rng.choice([-0.03, -0.02, 0.02, 0.03, 0.04])
        new_price = price * (1 + move)
        rows.append((price, max(price, new_price) * 1.001, min(price, new_price) * 0.999, new_price))
        price = new_price
    df = _bars(rows)

    call_count = [0]

    def signal_func(d):
        call_count[0] += 1
        if call_count[0] % 3 == 0:
            entry = d.iloc[-1]["close"]
            return {"signal_type": "LONG", "stop_loss": entry * 0.95, "take_profit_1": entry * 1.10, "leverage": 1}
        return None

    risk_config = RiskConfig(risk_per_trade_pct=0.5, max_daily_loss_pct=50, max_drawdown_pct=50, cooldown_consecutive_losses=100)
    config = BacktestConfig(
        initial_capital=10000,
        exchange_spec=ExchangeSpec(taker_fee=0, slippage_bps=0),
        funding_rate_avg=0.0,
        risk_config=risk_config,
    )
    bt = Backtester(config)
    result = bt.run(df, signal_func)

    if result.trades:
        assert bt.risk_engine.state.current_equity == pytest.approx(result.final_capital, rel=1e-6)

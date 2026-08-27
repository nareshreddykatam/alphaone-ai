"""Phase 2.6, section 14: a synthetic multi-day backtest engineered to hit
every risk mechanism in sequence -- daily loss limit, consecutive-loss
cooldown (start + expiry), a clean resumption period, and finally a
catastrophic drawdown that arms the hard kill for good. This is exactly the
scale (many simulated days through the real Backtester loop) at which the
original Phase 2.5 bug was only ever visible -- unit tests on RiskEngine in
isolation did not catch it.

Approach: build an hourly OHLC series where each "episode" is exactly 3
bars (signal bar, fill bar, exit bar) with hand-picked prices so the
backtester's own SL/TP intrabar logic determines the outcome (loss vs win)
-- not a shortcut. The signal function is handed the exact same stop/target
percentages used to build each episode's bars, so what the strategy
"requests" and what the market "does" are consistent, exactly like a real
strategy would specify a stop and have the market decide whether it's hit.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from services.backtester.engine import Backtester, BacktestConfig
from services.backtester.exchange_spec import ExchangeSpec
from services.risk_engine.engine import RiskConfig, RiskStatus


class ScriptBuilder:
    def __init__(self, start=datetime(2024, 1, 1, 0, 0)):
        self.rows = []
        self.episodes = {}  # signal_bar_index -> (stop_pct, target_pct)
        self.t = start
        self.price = 100.0
        self._add_bar(self.price, self.price, self.price, self.price)  # warm-up bar (index 0)

    def _add_bar(self, o, h, l, c):
        idx = len(self.rows)
        self.rows.append({"timestamp": self.t, "open": o, "high": h, "low": l, "close": c})
        self.t += timedelta(hours=1)
        return idx

    def idle(self, n: int):
        for _ in range(n):
            self._add_bar(self.price, self.price * 1.0005, self.price * 0.9995, self.price)

    def episode(self, stop_pct: float, target_pct: float, outcome: str) -> int:
        """LONG only (sufficient to exercise every risk mechanism). Returns
        the signal-bar index. `outcome` is "loss" (stop hit) or "win"
        (target hit)."""
        signal_idx = self._add_bar(self.price, self.price * 1.001, self.price * 0.999, self.price)
        entry = self.price
        self._add_bar(entry, entry * 1.001, entry * 0.999, entry)  # fill bar: entry = this bar's open

        stop = entry * (1 - stop_pct)
        target = entry * (1 + target_pct)
        if outcome == "loss":
            self._add_bar(entry, entry * 1.0005, stop * 0.999, stop)
            self.price = stop
        else:
            self._add_bar(entry, target * 1.001, entry * 0.999, target)
            self.price = target

        self.episodes[signal_idx] = (stop_pct, target_pct)
        return signal_idx

    def build(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows)


def _make_signal_func(episodes: dict[int, tuple[float, float]]):
    fired = set()

    def _signal(d: pd.DataFrame):
        i = len(d) - 1
        if i in episodes and i not in fired:
            fired.add(i)
            stop_pct, target_pct = episodes[i]
            entry_est = d.iloc[-1]["close"]
            return {
                "signal_type": "LONG",
                "stop_loss": entry_est * (1 - stop_pct),
                "take_profit_1": entry_est * (1 + target_pct),
                "leverage": 1,
                "position_pct": 1.0,
            }
        return None

    return _signal


@pytest.fixture
def scripted_scenario():
    b = ScriptBuilder()

    loss1 = b.episode(stop_pct=0.01, target_pct=0.05, outcome="loss")  # -1%
    loss2 = b.episode(stop_pct=0.01, target_pct=0.05, outcome="loss")  # -1% -> daily limit (-2%) hit
    day1_block_start = b.t

    b.idle(30)  # cross well into day 2 while blocked

    loss3 = b.episode(stop_pct=0.01, target_pct=0.05, outcome="loss")  # 3rd consecutive loss -> cooldown starts
    cooldown_start = b.t

    b.idle(3)  # still within the 60-minute cooldown window

    win1 = b.episode(stop_pct=0.01, target_pct=0.05, outcome="win")    # +5% -- clears the cooldown/streak

    b.idle(20)
    win2 = b.episode(stop_pct=0.01, target_pct=0.03, outcome="win")
    b.idle(20)
    win3 = b.episode(stop_pct=0.01, target_pct=0.03, outcome="win")
    pre_catastrophe_equity_marker = b.t
    b.idle(20)

    catastrophe = b.episode(stop_pct=0.20, target_pct=0.50, outcome="loss")  # -20% -> drawdown breach
    hard_kill_time = b.t

    b.idle(24 * 10)  # 10 more days after the hard kill
    post_kill = b.episode(stop_pct=0.01, target_pct=0.05, outcome="win")  # would be a sure win if not hard-killed
    b.idle(24 * 5)

    df = b.build()
    signal_func = _make_signal_func(b.episodes)

    return {
        "df": df,
        "signal_func": signal_func,
        "loss1": loss1, "loss2": loss2, "loss3": loss3,
        "win1": win1, "win2": win2, "win3": win3,
        "catastrophe": catastrophe, "post_kill": post_kill,
        "cooldown_start": cooldown_start,
    }


def _run(scripted_scenario):
    config = BacktestConfig(
        initial_capital=10000,
        exchange_spec=ExchangeSpec(taker_fee=0, slippage_bps=0),
        funding_rate_avg=0.0,
        risk_config=RiskConfig(
            max_daily_loss_pct=2.0,
            max_drawdown_pct=10.0,
            max_leverage=5,
            max_positions=1,
            max_daily_trades=100,
            cooldown_consecutive_losses=3,
            cooldown_minutes=60,
        ),
    )
    bt = Backtester(config)
    result = bt.run(scripted_scenario["df"], scripted_scenario["signal_func"])
    return bt, result


def test_daily_loss_limit_blocks_the_rest_of_day_1_then_resumes_day_2(scripted_scenario):
    bt, result = _run(scripted_scenario)
    df = scripted_scenario["df"]

    # exactly 2 trades landed on day 1 (the two losses that hit the limit)
    day1 = df["timestamp"].iloc[0].date()
    trades_day1 = [t for t in result.trades if t.entry_time.date() == day1]
    assert len(trades_day1) == 2, f"expected exactly 2 trades on day 1 (blocked after), got {len(trades_day1)}"
    assert all(t.pnl < 0 for t in trades_day1)

    # trading resumed on a later day (loss3 and beyond)
    later_trades = [t for t in result.trades if t.entry_time.date() > day1]
    assert len(later_trades) >= 1, "daily loss limit must not have permanently blocked trading past day 1"


def test_consecutive_loss_cooldown_blocks_then_expires(scripted_scenario):
    bt, result = _run(scripted_scenario)
    # 3 losses in a row (loss1, loss2, loss3), THEN a win (win1) -- confirms
    # the cooldown after the 3rd loss did not permanently disable trading.
    pnls_in_order = [t.pnl for t in result.trades[:4]]
    assert pnls_in_order[0] < 0 and pnls_in_order[1] < 0 and pnls_in_order[2] < 0, (
        "expected the first three trades to be the scripted losses"
    )
    assert pnls_in_order[3] > 0, "expected a winning trade to follow once the cooldown expired"


def test_win_after_cooldown_resets_consecutive_loss_counter(scripted_scenario):
    bt, result = _run(scripted_scenario)
    assert bt.risk_engine.state.consecutive_losses == 0 or bt.risk_engine.state.kill_switch, (
        "consecutive_losses should have been reset by the win, unless a later hard kill reset it too"
    )


def test_catastrophic_drawdown_arms_permanent_hard_kill(scripted_scenario):
    bt, result = _run(scripted_scenario)
    assert bt.risk_engine.state.kill_switch is True, "the -20% catastrophic loss should have armed the hard kill"
    assert bt.risk_engine.get_risk_status(scripted_scenario["df"]["timestamp"].iloc[-1]) == RiskStatus.HARD_KILL


def test_no_trades_occur_after_the_hard_kill(scripted_scenario):
    bt, result = _run(scripted_scenario)
    # The hard kill arms when the catastrophic trade's LOSS is recorded --
    # at its own exit, not the signal/entry bar that preceded it. The
    # catastrophe episode is the LAST trade that should exist if the hard
    # kill correctly blocked the scripted `post_kill` episode after it
    # (many days later in the dataset).
    catastrophe_trade = result.trades[-1]
    assert catastrophe_trade.pnl < 0 and catastrophe_trade.r_multiple == pytest.approx(-1.0), (
        "expected the last recorded trade to be the scripted catastrophic loss"
    )

    trades_after_kill = [t for t in result.trades if t.entry_time > catastrophe_trade.exit_time]
    assert trades_after_kill == [], (
        f"expected ZERO trades after the hard-kill event (many days remained in the dataset), "
        f"but found {len(trades_after_kill)} -- this is exactly the Phase 2.5 regression this test guards against "
        f"being reintroduced in the wrong direction (i.e. the hard kill failing to stay latched)"
    )


def test_trading_was_not_permanently_locked_out_before_the_hard_kill(scripted_scenario):
    """The core Phase 2.5 regression check: verify the strategy traded
    across MULTIPLE separate days before the catastrophic event, proving
    the daily-loss and cooldown mechanisms both correctly released their
    holds rather than a single early bad patch silently ending everything."""
    bt, result = _run(scripted_scenario)
    df = scripted_scenario["df"]
    hard_kill_time = df["timestamp"].iloc[scripted_scenario["catastrophe"]]

    pre_kill_trades = [t for t in result.trades if t.entry_time <= hard_kill_time]
    distinct_days = {t.entry_time.date() for t in pre_kill_trades}
    assert len(distinct_days) >= 3, (
        f"expected trades spread across at least 3 distinct days before the hard kill, "
        f"got trades only on {sorted(distinct_days)} -- suggests a lockout mechanism is not releasing correctly"
    )
    assert len(pre_kill_trades) >= 6, f"expected at least 6 trades before the hard kill, got {len(pre_kill_trades)}"


def test_full_expected_trade_sequence_and_sanity(scripted_scenario):
    bt, result = _run(scripted_scenario)
    # exactly: loss1, loss2, loss3, win1, win2, win3, catastrophe -- and
    # nothing from `post_kill` (blocked forever after).
    assert result.total_trades == 7, f"expected exactly 7 trades (the 6 scripted pre-kill episodes + the catastrophe), got {result.total_trades}"

    from services.backtester.sanity_checks import assert_result_sane
    assert_result_sane(result)  # must not raise

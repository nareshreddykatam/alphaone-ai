from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import pandas as pd
import numpy as np
import structlog

from services.risk_engine.engine import RiskEngine, RiskConfig
from services.backtester.exchange_spec import ExchangeSpec

logger = structlog.get_logger()


@dataclass
class BacktestConfig:
    initial_capital: float = 10000
    exchange_spec: ExchangeSpec = field(default_factory=ExchangeSpec)
    # Average funding rate assumed over a position's holding period. This is a
    # simplification, not a real per-8h funding series -- see
    # docs/exchange_assumptions.md. Real historical funding (ingested via
    # services/market_data) can be wired in later without changing this shape.
    funding_rate_avg: float = 0.0001
    risk_config: RiskConfig = field(default_factory=RiskConfig)

    @property
    def fee_rate(self) -> float:
        """Backward-compatible alias for the taker fee (backtester assumes
        market/taker fills on entry and exit)."""
        return self.exchange_spec.taker_fee

    @property
    def slippage_rate(self) -> float:
        return self.exchange_spec.slippage_rate

    @property
    def funding_interval_hours(self) -> int:
        return self.exchange_spec.funding_interval_hours


@dataclass
class BacktestTrade:
    trade_id: str
    side: str
    entry_price: float
    entry_time: datetime
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    quantity: float = 0
    leverage: int = 1
    pnl: float = 0
    pnl_pct: float = 0
    fees: float = 0
    funding: float = 0
    r_multiple: float = 0
    exit_reason: str = ""
    market_regime: str = ""


@dataclass
class BacktestResult:
    trades: list[BacktestTrade]
    equity_curve: list[dict]
    total_pnl: float
    total_pnl_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float
    expectancy: float
    average_r: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown: float
    max_drawdown_pct: float
    recovery_factor: float
    average_trade_pnl: float
    average_winning_trade: float
    average_losing_trade: float
    largest_win: float
    largest_loss: float
    consecutive_wins: int
    consecutive_losses: int
    total_fees: float
    total_funding: float
    training_period: str
    test_period: str
    initial_capital: float
    final_capital: float


class Backtester:
    """Event-driven, bar-by-bar backtester.

    Execution semantics (see docs/execution_semantics.md for the full
    write-up): a strategy decides its signal using data through bar T's
    close (`signal_func(df.iloc[:T+1])` is allowed to see T's own OHLCV).
    That decision is NOT filled at T's close -- it becomes eligible for
    execution at bar T+1's open, adjusted for slippage. This is the
    single execution assumption used everywhere in this codebase (baselines,
    the signal engine, walk-forward validation); a signal on the very last
    bar of a dataset has no T+1 to fill at and is simply never executed.
    """

    def __init__(self, config: BacktestConfig | None = None):
        self.config = config or BacktestConfig()
        self.risk_engine = RiskEngine(self.config.risk_config, self.config.initial_capital)

    def run(
        self,
        df: pd.DataFrame,
        signal_func,
        training_period: str = "",
        test_period: str = "",
        funding_rates: Optional[pd.DataFrame] = None,
    ) -> BacktestResult:
        """
        funding_rates: optional DataFrame with columns [timestamp, rate],
        sorted or not (sorted internally). When provided, funding is charged
        event-by-event using the REAL historical rate at each actual funding
        timestamp that occurs while a position is open (point-in-time
        correct -- only rates with timestamp <= the current bar are ever
        used). When omitted, falls back to the old flat-average estimate
        (`BacktestConfig.funding_rate_avg`) for callers/tests that don't have
        real funding data available. See docs/execution_semantics.md.
        """
        trades: list[BacktestTrade] = []
        equity_curve: list[dict] = []
        equity = self.config.initial_capital
        open_trade: Optional[BacktestTrade] = None
        pending_signal: Optional[dict] = None
        trade_counter = 0

        funding_events = None
        funding_idx = 0
        if funding_rates is not None and len(funding_rates) > 0:
            funding_events = funding_rates.sort_values("timestamp").reset_index(drop=True)

        for i in range(1, len(df)):
            current = df.iloc[i]
            timestamp = current["timestamp"]
            open_price = current["open"]
            price = current["close"]
            high = current["high"]
            low = current["low"]

            # 1. Fill any order decided on the PREVIOUS bar's close, at THIS
            #    bar's open. This is what makes the backtest not look ahead:
            #    the fill price was unknown at the moment the signal fired.
            if pending_signal is not None and open_trade is None:
                can_trade, reason = self.risk_engine.can_trade(now=timestamp)
                if can_trade:
                    trade_counter += 1
                    regime = pending_signal.get("market_regime", "UNCERTAIN")
                    open_trade = self._open_trade(
                        pending_signal, open_price, timestamp, trade_counter, regime
                    )
                    self.risk_engine.record_position_open()
                pending_signal = None

            # 2. Apply any real funding events up through this bar's
            #    timestamp to whatever position is currently open. Must
            #    happen before exit management below so a position that
            #    exits later in this same bar still settles funding it was
            #    actually exposed to.
            if funding_events is not None:
                funding_idx = self._apply_funding_events(
                    open_trade, funding_events, funding_idx, timestamp, open_price,
                )

            # 3. Manage any open position against THIS bar's intrabar range.
            if open_trade is not None:
                equity_before_trade = equity
                open_trade, equity = self._manage_position(
                    open_trade, current["open"], high, low, price, timestamp, equity,
                    use_real_funding=funding_events is not None,
                )
                if open_trade.exit_price is not None:
                    trades.append(open_trade)
                    # IMPORTANT: `open_trade.pnl_pct` is the trade's return
                    # relative to its OWN notional (entry_price * quantity),
                    # not relative to account equity -- these are only the
                    # same number when a trade risks 100% of equity
                    # (position_pct=1.0). Risk-sized trades (the normal
                    # case, via calculate_position_size) typically risk a
                    # small fraction of equity per trade, so passing the
                    # notional-relative percentage into the risk engine
                    # would wildly overstate daily P&L/drawdown relative to
                    # the real equity curve, and (since calculate_position_size
                    # itself reads risk_engine.state.current_equity) would
                    # then cascade into wrong position sizes for every
                    # subsequent trade too. Always convert to an
                    # equity-relative percentage before recording it.
                    equity_relative_pnl_pct = (
                        (open_trade.pnl / equity_before_trade) * 100 if equity_before_trade > 0 else 0
                    )
                    self.risk_engine.record_trade_result(equity_relative_pnl_pct, now=timestamp)
                    open_trade = None

            # 4. Decide a new signal using data through THIS bar's close. It
            #    is only ever filled starting next iteration (step 1 above).
            signal = signal_func(df.iloc[:i + 1])
            if signal is not None and open_trade is None:
                pending_signal = signal

            equity_curve.append({
                "timestamp": timestamp,
                "equity": equity,
                "price": price,
            })

        if open_trade is not None:
            equity_before_trade = equity_curve[-1]["equity"] if equity_curve else self.config.initial_capital
            last_timestamp = df.iloc[-1]["timestamp"]
            last_price = df.iloc[-1]["close"]
            open_trade.exit_price = last_price
            open_trade.exit_time = last_timestamp
            open_trade.exit_reason = "end_of_data"
            open_trade.pnl = self._calculate_pnl(open_trade)
            exit_fee = open_trade.exit_price * open_trade.quantity * self.config.fee_rate
            open_trade.fees += exit_fee
            if funding_events is None:
                open_trade.funding = self._estimate_flat_funding(open_trade)
            open_trade.pnl -= (open_trade.fees + open_trade.funding)
            open_trade.pnl_pct = (
                (open_trade.pnl / (open_trade.entry_price * open_trade.quantity)) * 100
                if open_trade.entry_price * open_trade.quantity > 0 else 0
            )
            trades.append(open_trade)
            equity += open_trade.pnl
            equity_curve.append({"timestamp": last_timestamp, "equity": equity, "price": last_price})

            # Same conversion as the main loop (see the comment above): the
            # risk engine's own equity/drawdown tracking must reflect the
            # trade's real impact on the account, not its return on notional.
            equity_relative_pnl_pct = (open_trade.pnl / equity_before_trade) * 100 if equity_before_trade > 0 else 0
            self.risk_engine.record_trade_result(equity_relative_pnl_pct, now=last_timestamp)
            self.risk_engine.record_position_close()

        return self._compile_results(trades, equity_curve, training_period, test_period)

    def _apply_funding_events(
        self, open_trade: Optional[BacktestTrade], funding_events: pd.DataFrame,
        funding_idx: int, bar_timestamp: datetime, mark_price: float,
    ) -> int:
        """Advance through funding_events up to `bar_timestamp`, charging
        (or crediting) any open trade for each event it was exposed to.

        Sign convention (matches real perpetual-futures funding mechanics):
        a positive rate means longs pay shorts. `trade.funding` accumulates
        in COST terms -- positive means money the trade paid out, negative
        means net credit received -- so `pnl -= trade.funding` at close
        always nets it correctly regardless of direction.

        A position is only charged for an event if it was already open
        strictly before that event's timestamp (`entry_time < event_ts`) --
        a position filled exactly at a funding timestamp was not exposed to
        that snapshot. A position that has already closed by the time this
        is called (open_trade is None) is simply skipped, and the pointer
        still advances so the event is never re-evaluated later.
        """
        n = len(funding_events)
        while funding_idx < n and funding_events.iloc[funding_idx]["timestamp"] <= bar_timestamp:
            event = funding_events.iloc[funding_idx]
            if open_trade is not None and open_trade.entry_time < event["timestamp"]:
                notional = open_trade.quantity * mark_price
                rate = float(event["rate"])
                if open_trade.side == "LONG":
                    funding_cost = notional * rate
                else:
                    funding_cost = -notional * rate
                open_trade.funding += funding_cost
            funding_idx += 1
        return funding_idx

    def _open_trade(
        self, signal: dict, price: float, timestamp: datetime, trade_id: int, regime: str
    ) -> BacktestTrade:
        side = signal.get("signal_type", "LONG")
        sl = signal.get("stop_loss")
        tp = signal.get("take_profit_1")
        leverage = min(signal.get("leverage", 1), self.config.risk_config.max_leverage)

        entry_price = price * (1 + self.config.slippage_rate) if side == "LONG" else price * (1 - self.config.slippage_rate)

        # Most strategies size off risk-to-stop (calculate_position_size).
        # A stop-less reference strategy (e.g. buy-and-hold has no exit by
        # definition) can instead request a fixed fraction of current equity
        # as notional -- still the same engine/fees/execution timing, just a
        # different sizing rule for a strategy that structurally has no stop.
        position_pct = signal.get("position_pct")
        if position_pct is not None:
            notional = self.risk_engine.state.current_equity * position_pct * leverage
            quantity = notional / entry_price if entry_price > 0 else 0
        elif sl:
            quantity = self.risk_engine.calculate_position_size(entry_price, sl, leverage)
        else:
            quantity = 0

        fee = entry_price * quantity * self.config.fee_rate

        return BacktestTrade(
            trade_id=f"BTEST-{trade_id:06d}",
            side=side,
            entry_price=entry_price,
            entry_time=timestamp,
            stop_loss=sl,
            take_profit=tp,
            quantity=quantity,
            leverage=leverage,
            fees=fee,
            market_regime=regime,
        )

    def _manage_position(
        self, trade: BacktestTrade, open_price: float, high: float, low: float, price: float,
        timestamp: datetime, equity: float, use_real_funding: bool = False,
    ) -> tuple[BacktestTrade, float]:
        # When a bar's range touches BOTH the stop and the target, OHLC data
        # alone cannot tell us which was hit first intrabar. We resolve this
        # ambiguity conservatively by always checking the stop first -- see
        # docs/execution_semantics.md ("simultaneous SL/TP" case).
        if trade.side == "LONG":
            if trade.stop_loss and low <= trade.stop_loss:
                trade.exit_price = self._resolve_stop_fill(open_price, trade.stop_loss, "LONG")
                trade.exit_time = timestamp
                trade.exit_reason = "stop_loss"
            elif trade.take_profit and high >= trade.take_profit:
                trade.exit_price = trade.take_profit * (1 - self.config.slippage_rate)
                trade.exit_time = timestamp
                trade.exit_reason = "take_profit"
        else:
            if trade.stop_loss and high >= trade.stop_loss:
                trade.exit_price = self._resolve_stop_fill(open_price, trade.stop_loss, "SHORT")
                trade.exit_time = timestamp
                trade.exit_reason = "stop_loss"
            elif trade.take_profit and low <= trade.take_profit:
                trade.exit_price = trade.take_profit * (1 + self.config.slippage_rate)
                trade.exit_time = timestamp
                trade.exit_reason = "take_profit"

        if trade.exit_price is not None:
            trade.pnl = self._calculate_pnl(trade)
            exit_fee = trade.exit_price * trade.quantity * self.config.fee_rate
            trade.fees += exit_fee
            if not use_real_funding:
                # No real historical funding series was supplied to this
                # backtest -- fall back to the flat-average estimate.
                trade.funding = self._estimate_flat_funding(trade)
            # else: trade.funding was already accumulated event-by-event in
            # _apply_funding_events using real historical rates.
            trade.pnl -= (trade.fees + trade.funding)
            trade.pnl_pct = (trade.pnl / (trade.entry_price * trade.quantity)) * 100 if (trade.entry_price * trade.quantity) > 0 else 0

            risk = abs(trade.entry_price - trade.stop_loss) if trade.stop_loss else 1
            trade.r_multiple = (trade.pnl / (risk * trade.quantity)) if risk > 0 and trade.quantity > 0 else 0

            equity += trade.pnl
            self.risk_engine.record_position_close()

        return trade, equity

    def _resolve_stop_fill(self, open_price: float, stop_loss: float, side: str) -> float:
        """Fill a stop at the true available price, not a price that no
        longer exists once the market has gapped through it.

        If the bar's own OPEN is already beyond the stop (a gap), a stop
        (market) order can only fill at that worse available price -- using
        the old stop level would pretend a fill happened at a price the
        market never offered. If the open is still on the favorable side of
        the stop, the stop fills normally at its own level (the market
        reached it intrabar, which is the realistic assumption for a
        resting stop order). See docs/execution_semantics.md.
        """
        if side == "LONG":
            gapped = open_price <= stop_loss
            reference = open_price if gapped else stop_loss
            return reference * (1 - self.config.slippage_rate)
        else:
            gapped = open_price >= stop_loss
            reference = open_price if gapped else stop_loss
            return reference * (1 + self.config.slippage_rate)

    def _estimate_flat_funding(self, trade: BacktestTrade) -> float:
        """Fallback used only when no real historical funding series is
        available: estimate funding cost from a flat average rate applied
        once per elapsed funding interval over the holding period."""
        funding_periods = 1
        if trade.entry_time and trade.exit_time:
            hours = (trade.exit_time - trade.entry_time).total_seconds() / 3600
            funding_periods = max(1, int(hours / self.config.funding_interval_hours))
        return trade.entry_price * trade.quantity * self.config.funding_rate_avg * funding_periods

    def _calculate_pnl(self, trade: BacktestTrade) -> float:
        if trade.exit_price is None:
            return 0
        if trade.side == "LONG":
            return (trade.exit_price - trade.entry_price) * trade.quantity * trade.leverage
        else:
            return (trade.entry_price - trade.exit_price) * trade.quantity * trade.leverage

    def _compile_results(
        self, trades: list[BacktestTrade], equity_curve: list[dict],
        training_period: str, test_period: str,
    ) -> BacktestResult:
        if not trades:
            return BacktestResult(
                trades=trades, equity_curve=equity_curve,
                total_pnl=0, total_pnl_pct=0, total_trades=0,
                winning_trades=0, losing_trades=0, win_rate=0,
                profit_factor=0, expectancy=0, average_r=0,
                sharpe_ratio=0, sortino_ratio=0, max_drawdown=0,
                max_drawdown_pct=0, recovery_factor=0,
                average_trade_pnl=0, average_winning_trade=0,
                average_losing_trade=0, largest_win=0, largest_loss=0,
                consecutive_wins=0, consecutive_losses=0,
                total_fees=0, total_funding=0,
                training_period=training_period, test_period=test_period,
                initial_capital=self.config.initial_capital,
                final_capital=self.config.initial_capital,
            )

        pnls = [t.pnl for t in trades]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p <= 0]

        total_pnl = sum(pnls)
        wins = len(winners)
        losses = len(losers)
        total = wins + losses

        gross_profit = sum(winners) if winners else 0
        gross_loss = abs(sum(losers)) if losers else 1

        equity_values = np.array([eq["equity"] for eq in equity_curve])

        max_dd = 0
        max_dd_pct = 0
        peak = equity_values[0]
        for val in equity_values:
            if val > peak:
                peak = val
            dd = peak - val
            dd_pct = dd / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct

        # Sharpe/Sortino must be computed on DAILY returns regardless of the
        # underlying candle timeframe -- computing pct_change() directly on
        # a per-bar (e.g. 1h or 1m) equity curve and then annualizing with a
        # flat sqrt(365) silently produces numbers that are wrong by a large,
        # timeframe-dependent factor. Resampling to daily first fixes this
        # for every timeframe uniformly.
        equity_series = pd.Series(
            equity_values,
            index=pd.to_datetime([eq["timestamp"] for eq in equity_curve]),
        )
        daily_equity = equity_series.resample("1D").last().ffill()
        daily_returns = daily_equity.pct_change().dropna()

        sharpe = 0
        sortino = 0
        if len(daily_returns) > 1 and daily_returns.std() > 0:
            sharpe = (daily_returns.mean() / daily_returns.std()) * np.sqrt(365)
            downside = daily_returns[daily_returns < 0]
            if len(downside) > 0 and downside.std() > 0:
                sortino = (daily_returns.mean() / downside.std()) * np.sqrt(365)

        max_consecutive_wins = 0
        max_consecutive_losses = 0
        current_wins = 0
        current_losses = 0
        for p in pnls:
            if p > 0:
                current_wins += 1
                current_losses = 0
                max_consecutive_wins = max(max_consecutive_wins, current_wins)
            else:
                current_losses += 1
                current_wins = 0
                max_consecutive_losses = max(max_consecutive_losses, current_losses)

        avg_r = np.mean([t.r_multiple for t in trades]) if trades else 0

        return BacktestResult(
            trades=trades,
            equity_curve=equity_curve,
            total_pnl=round(total_pnl, 2),
            total_pnl_pct=round(total_pnl / self.config.initial_capital * 100, 2),
            total_trades=total,
            winning_trades=wins,
            losing_trades=losses,
            win_rate=round(wins / total * 100, 2) if total > 0 else 0,
            profit_factor=round(gross_profit / gross_loss, 2) if gross_loss > 0 else 0,
            expectancy=round(total_pnl / total, 2) if total > 0 else 0,
            average_r=round(float(avg_r), 2),
            sharpe_ratio=round(float(sharpe), 2),
            sortino_ratio=round(float(sortino), 2),
            max_drawdown=round(max_dd, 2),
            max_drawdown_pct=round(max_dd_pct, 2),
            recovery_factor=round(total_pnl / max_dd, 2) if max_dd > 0 else 0,
            average_trade_pnl=round(total_pnl / total, 2) if total > 0 else 0,
            average_winning_trade=round(gross_profit / wins, 2) if wins > 0 else 0,
            average_losing_trade=round(-gross_loss / losses, 2) if losses > 0 else 0,
            largest_win=round(max(pnls), 2) if pnls else 0,
            largest_loss=round(min(pnls), 2) if pnls else 0,
            consecutive_wins=max_consecutive_wins,
            consecutive_losses=max_consecutive_losses,
            total_fees=round(sum(t.fees for t in trades), 2),
            total_funding=round(sum(t.funding for t in trades), 2),
            training_period=training_period,
            test_period=test_period,
            initial_capital=self.config.initial_capital,
            final_capital=round(self.config.initial_capital + total_pnl, 2),
        )

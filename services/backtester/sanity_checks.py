"""Automated sanity checks against impossible/inconsistent backtest results.

Per Phase 2.5 requirements: if a check fails, the run must be treated as
untrustworthy -- callers should raise/refuse to publish a report, not
silently show a misleading number.
"""
from dataclasses import dataclass

from services.backtester.engine import BacktestResult


@dataclass
class SanityViolation:
    check: str
    detail: str


class BacktestSanityError(Exception):
    def __init__(self, violations: list[SanityViolation]):
        self.violations = violations
        message = "Backtest failed sanity checks:\n" + "\n".join(f"  - {v.check}: {v.detail}" for v in violations)
        super().__init__(message)


def check_result_sanity(result: BacktestResult, config=None) -> list[SanityViolation]:
    """Returns a list of violations (empty if the result passes every
    check). Pure inspection -- never mutates `result`."""
    violations: list[SanityViolation] = []

    if result.profit_factor < 0:
        violations.append(SanityViolation("profit_factor_non_negative", f"profit_factor={result.profit_factor} is negative"))

    if not (0 <= result.win_rate <= 100):
        violations.append(SanityViolation("win_rate_range", f"win_rate={result.win_rate} outside [0, 100]"))

    if result.max_drawdown < 0 or result.max_drawdown_pct < 0:
        violations.append(SanityViolation("drawdown_non_negative", f"max_drawdown={result.max_drawdown}, max_drawdown_pct={result.max_drawdown_pct}"))

    expected_final = result.initial_capital + result.total_pnl
    if abs(result.final_capital - expected_final) > max(0.01, abs(expected_final) * 1e-6):
        violations.append(SanityViolation(
            "final_capital_matches_equity",
            f"final_capital={result.final_capital} != initial_capital+total_pnl={expected_final}",
        ))

    if result.total_fees < 0:
        violations.append(SanityViolation("fees_non_negative", f"total_fees={result.total_fees} is negative"))

    if result.total_trades != (result.winning_trades + result.losing_trades):
        violations.append(SanityViolation(
            "trade_count_matches_win_loss_split",
            f"total_trades={result.total_trades} != winning({result.winning_trades})+losing({result.losing_trades})",
        ))

    if result.total_trades != len(result.trades):
        violations.append(SanityViolation(
            "trade_count_matches_recorded_trades",
            f"total_trades={result.total_trades} != len(trades)={len(result.trades)}",
        ))

    for t in result.trades:
        if t.entry_time is not None and t.exit_time is not None and t.exit_time < t.entry_time:
            violations.append(SanityViolation(
                "exit_not_before_entry",
                f"trade {t.trade_id}: exit_time {t.exit_time} < entry_time {t.entry_time}",
            ))
        if t.quantity < 0:
            violations.append(SanityViolation("position_size_non_negative", f"trade {t.trade_id}: quantity={t.quantity} is negative"))
        if config is not None and t.leverage > config.exchange_spec.max_leverage:
            violations.append(SanityViolation(
                "leverage_within_configured_limit",
                f"trade {t.trade_id}: leverage={t.leverage} > max_leverage={config.exchange_spec.max_leverage}",
            ))

    # Equity curve must never move except across a recorded trade close (or
    # stay flat between them) -- a jump with no corresponding trade event
    # would mean equity was mutated somewhere it shouldn't have been.
    if result.equity_curve:
        trade_close_equities = set()
        running = result.initial_capital
        for t in result.trades:
            running += t.pnl
            trade_close_equities.add(round(running, 2))
        seen_values = {round(e["equity"], 2) for e in result.equity_curve}
        # every equity value that ever appears must be either the initial
        # capital or a value reachable by some prefix of realized trade pnls
        prefix_values = {round(result.initial_capital, 2)}
        running = result.initial_capital
        for t in result.trades:
            running += t.pnl
            prefix_values.add(round(running, 2))
        unexplained = seen_values - prefix_values
        if unexplained:
            violations.append(SanityViolation(
                "equity_curve_explained_by_trades",
                f"equity curve contains values not reachable from initial_capital + realized trade pnls: {sorted(unexplained)[:5]}",
            ))

    return violations


def assert_result_sane(result: BacktestResult, config=None) -> None:
    violations = check_result_sanity(result, config)
    if violations:
        raise BacktestSanityError(violations)

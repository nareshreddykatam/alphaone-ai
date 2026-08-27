"""The strategy signal registry.

S05 (existing validated Donchian+ADX) is untouched -- reused directly via
services.signal_engine.strategy.BaselineStrategy, never re-implemented, per
explicit instruction to protect it: its production status is not changed
by this file's research, even where the newer, stricter methodology below
found it does not clear the same bar as a fresh candidate would need to.

Classification methodology (v2, stricter than the original pass -- see
reports/STRATEGY_RESEARCH_V2_RIGOROUS_REPORT.txt for the full, real, unedited
output, and scripts/research_v2_rigorous.py for the exact code): every
candidate is evaluated on a chronological, non-shuffled 60/20/20 TRAIN /
VALIDATION / OUT-OF-SAMPLE split of ~3 years of real BTC/USDT candles. Any
free parameter is grid-searched on TRAIN and selected using VALIDATION
performance only (minimum 15 trades to be eligible, maximize profit
factor); the winning parameters are then FROZEN and evaluated exactly once
against the untouched OOS region -- never re-tuned after looking at OOS.
Walk-forward folds, long/short breakdown, regime attribution, a trade-order
bootstrap, and parameter-neighbor sensitivity are all computed on that same
OOS region. `production_status` reflects this evidence: PRODUCTION_ELIGIBLE
requires a positive OOS profit factor, a low absolute drawdown, at least
half of the OOS walk-forward folds profitable, and no sign of the
parameter-sensitivity or small-sample-outlier instability that marks
overfitting. Under this stricter test, only S06 (of the 11 new/existing
candidates re-examined) clears that bar; S05 is exempted from the bar by
explicit instruction, not because this test independently re-validates it
-- see its own finding in the report. Every other candidate, including two
new ones (S11, S12) added specifically to look for a replacement before
accepting "no 15m strategy passes," failed decisively (0/4 OOS
walk-forward folds profitable, all 5 fifteen-minute candidates tested).

Statuses: PRODUCTION_ELIGIBLE, RESEARCH_ONLY, or REJECTED (a candidate that
was specifically investigated as a replacement and found unsuitable -- kept
in the registry, fully implemented and tested, purely for a transparent
record; never evaluated live). Only PRODUCTION_ELIGIBLE strategies are ever
evaluated by the live scheduler (services/signal_engine/multi_strategy_engine.py)
-- both RESEARCH_ONLY and REJECTED strategies exist here for testability/
completeness and the frontend's signal-history filters, but never generate
a live signal, never persist a Signal row, and never reach Telegram.

Every strategy independently returns LONG / SHORT / NO_TRADE. There is no
consensus requirement and no cross-strategy suppression -- see
multi_strategy_engine.py.
"""
from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd

from ml.evaluation.multi_strategy_signals import MULTI_STRATEGIES, precompute_mtf_trend
from services.signal_engine.strategy import BaselineStrategy, SignalStrategy, StrategySignal


def _rr_quality(entry: Optional[float], sl: Optional[float], tp1: Optional[float]) -> str:
    """Quality derived from a REAL, already-computed number (the signal's
    own risk/reward ratio) -- never a fabricated confidence score. These
    rule-based strategies have no probability output to bucket (unlike
    MLStrategy's calibrated probability), so R:R is the only genuine
    per-signal number available; thresholds are documented display tiers,
    not an accuracy claim."""
    if entry is None or sl is None or tp1 is None:
        return "LOW"
    risk = abs(entry - sl)
    if risk <= 0:
        return "LOW"
    rr = abs(tp1 - entry) / risk
    if rr >= 2.5:
        return "HIGH"
    if rr >= 1.5:
        return "MEDIUM"
    return "LOW"


class RuleBasedMultiStrategy(SignalStrategy):
    """Generic wrapper around one of ml/evaluation/multi_strategy_signals.py's
    precompute + signal_func pairs -- the same `signal_func(df) -> dict|None`
    contract Backtester.run() uses, so a strategy's LIVE evaluation and its
    OWN backtest run through byte-for-byte identical logic (only the input
    df differs: live closed candles vs. historical candles)."""

    def __init__(self, strategy_id: str, display_name: str, precompute_fn: Callable, signal_func_factory: Callable, disclaimer: str):
        self.strategy_id = strategy_id
        self.name = strategy_id
        self._display_name = display_name
        self._precompute = precompute_fn
        self._signal_func = signal_func_factory()
        self._disclaimer = disclaimer

    def generate(self, df: pd.DataFrame) -> StrategySignal:
        prepared = self._precompute(df)
        raw = self._signal_func(prepared)

        if raw is None:
            return StrategySignal(
                signal_type="NO_TRADE", entry_price=None, stop_loss=None,
                take_profit_1=None, take_profit_2=None, take_profit_3=None,
                quality="LOW", reasoning=f"No {self._display_name} setup met. {self._disclaimer}",
                strategy_name=self.strategy_id,
            )

        entry = float(prepared.iloc[-1]["close"])
        sl = raw.get("stop_loss")
        tp1 = raw.get("take_profit_1")
        return StrategySignal(
            signal_type=raw["signal_type"],
            entry_price=entry,
            stop_loss=sl,
            take_profit_1=tp1,
            take_profit_2=raw.get("take_profit_2"),
            take_profit_3=raw.get("take_profit_3"),
            quality=_rr_quality(entry, sl, tp1),
            reasoning=f"{raw['signal_type']} -- {self._display_name} setup. {self._disclaimer}",
            strategy_name=self.strategy_id,
        )


class MTFTrendStrategy(RuleBasedMultiStrategy):
    """S10 needs BOTH the 4h df AND a 1d df (for the daily trend filter) --
    the only strategy here that doesn't fit the plain single-df `generate`
    contract, so it takes an extra `df_1d` at call time instead of at
    precompute-registration time."""

    def generate_with_daily(self, df_4h: pd.DataFrame, df_1d: pd.DataFrame) -> StrategySignal:
        prepared = precompute_mtf_trend(df_4h, df_1d)
        raw = self._signal_func(prepared)
        if raw is None:
            return StrategySignal(
                signal_type="NO_TRADE", entry_price=None, stop_loss=None,
                take_profit_1=None, take_profit_2=None, take_profit_3=None,
                quality="LOW", reasoning=f"No {self._display_name} setup met. {self._disclaimer}",
                strategy_name=self.strategy_id,
            )
        entry = float(prepared.iloc[-1]["close"])
        sl = raw.get("stop_loss")
        tp1 = raw.get("take_profit_1")
        return StrategySignal(
            signal_type=raw["signal_type"], entry_price=entry, stop_loss=sl, take_profit_1=tp1,
            take_profit_2=raw.get("take_profit_2"), take_profit_3=raw.get("take_profit_3"),
            quality=_rr_quality(entry, sl, tp1),
            reasoning=f"{raw['signal_type']} -- {self._display_name} setup. {self._disclaimer}",
            strategy_name=self.strategy_id,
        )

    def generate(self, df: pd.DataFrame) -> StrategySignal:
        raise NotImplementedError("S10 requires both 4h and 1d data -- call generate_with_daily() instead.")


# ---------------------------------------------------------------------------
# Disclaimers: each one states the REAL out-of-sample result from
# reports/STRATEGY_RESEARCH_V2_RIGOROUS_REPORT.txt (chronological train/val/
# OOS split, parameters frozen on VALIDATION before ever touching OOS, real
# fees/slippage/funding). Never edit these without re-running
# scripts/research_v2_rigorous.py and updating both the report and this
# text together.
# ---------------------------------------------------------------------------

_DISCLAIMERS = {
    "S01_MOMENTUM_BREAKOUT_15M": (
        "OOS result (train/val/OOS split, frozen params): PF 0.63, return -9.1%, "
        "0/4 OOS walk-forward folds profitable. LONG and SHORT both losing "
        "(PF 0.40 / 0.76). Losing in every regime tested. RESEARCH_ONLY."
    ),
    "S02_EMA_PULLBACK_15M": (
        "OOS result: PF 0.62, return -9.4%, 0/4 OOS walk-forward folds "
        "profitable. LONG and SHORT both losing (PF 0.40 / 0.75). RESEARCH_ONLY."
    ),
    "S03_VWAP_REVERSION_15M": (
        "OOS result: PF 0.67, return -8.8%, 0/4 OOS walk-forward folds "
        "profitable. Sharply asymmetric: LONG near-breakeven (PF 1.02, 38 "
        "trades) but SHORT loses heavily (PF 0.32, 30 trades) -- the losing "
        "side dominates the combined result. RESEARCH_ONLY."
    ),
    "S04_RSI_BB_15M": (
        "OOS result: PF 0.43, return -9.2%, 0/4 OOS walk-forward folds "
        "profitable -- the weakest of the four 15m candidates tested. "
        "RESEARCH_ONLY."
    ),
    "S06_SUPERTREND_ATR_4H": (
        "OOS result (train/val/OOS split, frozen params period=10, "
        "multiplier=3.0): PF 1.10, return +0.4%, 2/4 OOS walk-forward folds "
        "profitable, max drawdown ~1%. The strongest of the 12 candidates "
        "re-examined under this stricter methodology, but the edge is modest, "
        "not decisive: LONG carries the result (PF 1.52) while SHORT is weak "
        "(PF 0.85), and only 2 of 4 OOS folds were profitable. Parameter "
        "neighbors (period 7/14, multiplier 2.5/3.5) ranged PF 0.78-1.33 -- "
        "reasonably stable, not collapsing, but not flat either. A trade-order "
        "bootstrap (500x reshuffle) put the observed ~1% drawdown within the "
        "expected range for this trade set. Still a research heuristic with a "
        "real but modest OOS edge -- never a guaranteed or maximum-profit "
        "outcome."
    ),
    "S07_MACD_MOMENTUM_4H": (
        "OOS result: PF 0.70, return -2.5%, 1/4 OOS walk-forward folds "
        "profitable. RESEARCH_ONLY."
    ),
    "S08_EMA_ADX_4H": (
        "OOS result: PF 0.80, return -2.3%, 2/4 OOS walk-forward folds "
        "profitable. RESEARCH_ONLY."
    ),
    "S09_ATR_BREAKOUT_4H": (
        "OOS result: PF 1.23, return +1.0%, 2/4 OOS walk-forward folds "
        "profitable -- but on only 23 OOS trades, and one fold's PF (139, on "
        "just 4 trades) is a small-sample outlier that inflates the headline "
        "number rather than evidence of a real edge. RESEARCH_ONLY pending a "
        "larger, less fold-dependent sample."
    ),
    "S10_MTF_TREND_4H": (
        "OOS result: PF 0.75, return -2.8%, 2/4 OOS walk-forward folds "
        "profitable. RESEARCH_ONLY."
    ),
    "S11_ZSCORE_REVERSION_15M": (
        "Investigated as a 15m replacement candidate (statistical mean "
        "reversion, distinct from S03's VWAP-anchored approach). OOS result: "
        "PF 0.49, return -9.8%, 0/4 OOS walk-forward folds profitable -- "
        "REJECTED, no replacement of any existing 15m slot."
    ),
    "S12_STRUCTURE_RETEST_4H": (
        "Investigated as a 4h replacement candidate (breakout-then-retest "
        "entry, distinct from S05's immediate-breakout and S06's trailing-"
        "stop-flip mechanisms). OOS result: PF 0.53, return -6.7%, 0/4 OOS "
        "walk-forward folds profitable -- REJECTED, no replacement of any "
        "existing 4h slot."
    ),
}


@dataclass
class StrategyDefinition:
    strategy_id: str
    display_name: str
    timeframe: str
    data_mode: str  # "CLOSED_CANDLE" or "LIVE_INTRABAR"
    production_status: str  # "PRODUCTION_ELIGIBLE" or "RESEARCH_ONLY"
    make_strategy: Callable[[], SignalStrategy]
    # The value actually written to Signal.strategy_name when this strategy
    # fires. Equal to `strategy_id` for every NEW strategy (S01-S04, S06-S10),
    # but S05 persists under BaselineStrategy's own pre-existing, untouched
    # `name` ("trend_following_donchian_adx") -- every existing Signal row
    # and services/signal_engine/live_breakout.py's dedup check already use
    # that string, so it was deliberately NOT renamed to "S05_..." (see
    # multi_strategy_engine.py's dedup comment for why this field exists).
    persisted_name: str = ""

    def __post_init__(self):
        if not self.persisted_name:
            self.persisted_name = self.strategy_id


def _make_rule_based(strategy_id: str) -> RuleBasedMultiStrategy:
    spec = MULTI_STRATEGIES[strategy_id]
    cls = MTFTrendStrategy if strategy_id == "S10_MTF_TREND_4H" else RuleBasedMultiStrategy
    return cls(strategy_id, spec["display_name"], spec["precompute"], spec["factory"], _DISCLAIMERS[strategy_id])


MULTI_STRATEGY_REGISTRY: list[StrategyDefinition] = [
    StrategyDefinition(
        strategy_id="S05_DONCHIAN_ADX_4H", display_name="Donchian(20) + ADX(14)", timeframe="4h",
        data_mode="LIVE_INTRABAR", production_status="PRODUCTION_ELIGIBLE",
        make_strategy=lambda: BaselineStrategy(), persisted_name=BaselineStrategy.name,
    ),
    StrategyDefinition(
        strategy_id="S01_MOMENTUM_BREAKOUT_15M", display_name="Momentum / Volatility Breakout", timeframe="15m",
        data_mode="CLOSED_CANDLE", production_status="RESEARCH_ONLY",
        make_strategy=lambda: _make_rule_based("S01_MOMENTUM_BREAKOUT_15M"),
    ),
    StrategyDefinition(
        strategy_id="S02_EMA_PULLBACK_15M", display_name="EMA Pullback / Trend Continuation", timeframe="15m",
        data_mode="CLOSED_CANDLE", production_status="RESEARCH_ONLY",
        make_strategy=lambda: _make_rule_based("S02_EMA_PULLBACK_15M"),
    ),
    StrategyDefinition(
        strategy_id="S03_VWAP_REVERSION_15M", display_name="VWAP Mean Reversion", timeframe="15m",
        data_mode="CLOSED_CANDLE", production_status="RESEARCH_ONLY",
        make_strategy=lambda: _make_rule_based("S03_VWAP_REVERSION_15M"),
    ),
    StrategyDefinition(
        strategy_id="S04_RSI_BB_15M", display_name="RSI + Bollinger Momentum/Reversal", timeframe="15m",
        data_mode="CLOSED_CANDLE", production_status="RESEARCH_ONLY",
        make_strategy=lambda: _make_rule_based("S04_RSI_BB_15M"),
    ),
    StrategyDefinition(
        strategy_id="S06_SUPERTREND_ATR_4H", display_name="Supertrend + ATR", timeframe="4h",
        data_mode="CLOSED_CANDLE", production_status="PRODUCTION_ELIGIBLE",
        make_strategy=lambda: _make_rule_based("S06_SUPERTREND_ATR_4H"),
    ),
    StrategyDefinition(
        strategy_id="S07_MACD_MOMENTUM_4H", display_name="MACD Trend Momentum", timeframe="4h",
        data_mode="CLOSED_CANDLE", production_status="RESEARCH_ONLY",
        make_strategy=lambda: _make_rule_based("S07_MACD_MOMENTUM_4H"),
    ),
    StrategyDefinition(
        strategy_id="S08_EMA_ADX_4H", display_name="EMA Structure + ADX", timeframe="4h",
        data_mode="CLOSED_CANDLE", production_status="RESEARCH_ONLY",
        make_strategy=lambda: _make_rule_based("S08_EMA_ADX_4H"),
    ),
    StrategyDefinition(
        strategy_id="S09_ATR_BREAKOUT_4H", display_name="ATR Volatility Breakout", timeframe="4h",
        data_mode="CLOSED_CANDLE", production_status="RESEARCH_ONLY",
        make_strategy=lambda: _make_rule_based("S09_ATR_BREAKOUT_4H"),
    ),
    StrategyDefinition(
        strategy_id="S10_MTF_TREND_4H", display_name="Multi-Timeframe Trend Confirmation", timeframe="4h",
        data_mode="CLOSED_CANDLE", production_status="RESEARCH_ONLY",
        make_strategy=lambda: _make_rule_based("S10_MTF_TREND_4H"),
    ),
    StrategyDefinition(
        strategy_id="S11_ZSCORE_REVERSION_15M", display_name="Z-Score Mean Reversion", timeframe="15m",
        data_mode="CLOSED_CANDLE", production_status="REJECTED",
        make_strategy=lambda: _make_rule_based("S11_ZSCORE_REVERSION_15M"),
    ),
    StrategyDefinition(
        strategy_id="S12_STRUCTURE_RETEST_4H", display_name="Structure Breakout + Retest", timeframe="4h",
        data_mode="CLOSED_CANDLE", production_status="REJECTED",
        make_strategy=lambda: _make_rule_based("S12_STRUCTURE_RETEST_4H"),
    ),
]


def get_strategy_definition(strategy_id: str) -> StrategyDefinition:
    for d in MULTI_STRATEGY_REGISTRY:
        if d.strategy_id == strategy_id:
            return d
    raise ValueError(f"Unknown strategy_id: {strategy_id}")


def get_definition_by_persisted_name(persisted_name: Optional[str]) -> Optional[StrategyDefinition]:
    """Reverse lookup from Signal.strategy_name (what's actually in the DB)
    back to the strategy's registry metadata (display name, timeframe) --
    used for Telegram/frontend display. Returns None for a name that
    doesn't match any registered strategy (e.g. a legacy/unknown row) --
    callers must fall back to showing the raw string, never guess."""
    if not persisted_name:
        return None
    for d in MULTI_STRATEGY_REGISTRY:
        if d.persisted_name == persisted_name:
            return d
    return None

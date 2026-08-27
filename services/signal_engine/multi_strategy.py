"""The 10-strategy independent signal registry.

S05 (existing validated Donchian+ADX) is untouched -- reused directly via
services.signal_engine.strategy.BaselineStrategy, never re-implemented.

S01-S04 (15m) and S06-S10 (4h) are the 9 new candidates researched in
scripts/research_multi_strategy.py against 3 years of real BTC/USDT
historical data (see reports/MULTI_STRATEGY_RESEARCH_RESULTS.txt for the
full, real, unedited backtest + walk-forward output this classification is
based on). Each strategy's `production_status` reflects that evidence, not
a hand-wave -- a strategy is PRODUCTION_ELIGIBLE only if BOTH its
full-period backtest AND a majority of its walk-forward folds were
profitable, and it did not show the classic overfitting signature of wild
fold-to-fold instability. A strategy is not eligible merely because one
number looked good.

Only PRODUCTION_ELIGIBLE strategies are ever evaluated by the live
scheduler (services/signal_engine/multi_strategy_engine.py) -- RESEARCH_ONLY
strategies exist here for testability/completeness and for the frontend's
signal-history filters, but never generate a live signal, never persist a
Signal row, and never reach Telegram.

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
# Disclaimers: each one states the REAL result from
# reports/MULTI_STRATEGY_RESEARCH_RESULTS.txt (fee/slippage/funding-inclusive
# backtest + 9-fold walk-forward against ~3 years of real BTC/USDT candles).
# Never edit these without re-running scripts/research_multi_strategy.py and
# updating both the report and this text together.
# ---------------------------------------------------------------------------

_DISCLAIMERS = {
    "S01_MOMENTUM_BREAKOUT_15M": (
        "Research result: full-period losing (PF 0.57, -9.3%), 0/9 walk-forward "
        "folds profitable against real 15m BTC/USDT data. RESEARCH_ONLY."
    ),
    "S02_EMA_PULLBACK_15M": (
        "Research result: full-period losing (PF 0.30, -9.9%), 0/9 walk-forward "
        "folds profitable. RESEARCH_ONLY."
    ),
    "S03_VWAP_REVERSION_15M": (
        "Research result: full-period losing (PF 0.73, -8.1%), 0/9 walk-forward "
        "folds profitable. RESEARCH_ONLY."
    ),
    "S04_RSI_BB_15M": (
        "Research result: full-period losing (PF 0.59, -9.7%), 0/9 walk-forward "
        "folds profitable. RESEARCH_ONLY."
    ),
    "S06_SUPERTREND_ATR_4H": (
        "Research result: full-period PF 1.25 (+4.2%), 6/9 walk-forward folds "
        "profitable, max drawdown ~2% against real 4h BTC/USDT data -- the "
        "strongest of the 9 new candidates tested. Still a research heuristic, "
        "not a guaranteed or maximum-profit outcome."
    ),
    "S07_MACD_MOMENTUM_4H": (
        "Research result: full-period losing (PF 0.81, -6.2%) despite 5/9 "
        "profitable walk-forward folds -- large losses in losing folds outweigh "
        "gains. RESEARCH_ONLY."
    ),
    "S08_EMA_ADX_4H": (
        "Research result: full-period losing (PF 0.92, -5.3%), only 2/9 "
        "walk-forward folds profitable. RESEARCH_ONLY."
    ),
    "S09_ATR_BREAKOUT_4H": (
        "Research result: full-period breakeven (PF 1.00), 4/9 walk-forward "
        "folds profitable with high fold-to-fold variance (PF ranged 4.72 to "
        "0.18 across folds) -- an overfitting/instability signature rather than "
        "a robust edge. RESEARCH_ONLY."
    ),
    "S10_MTF_TREND_4H": (
        "Research result: full-period modestly positive (PF 1.06, +3.2%) but "
        "only 4/9 walk-forward folds profitable with volatile fold-to-fold "
        "swings. Not yet robust enough for production. RESEARCH_ONLY."
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

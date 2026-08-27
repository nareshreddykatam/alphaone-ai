"""Pluggable signal-source abstraction (Phase 4 spec: BaselineStrategy /
MLStrategy / FutureStrategy). The critical constraint this module exists to
enforce: nothing here may claim a validated edge. Phase 2.6's strongest
baseline (Donchian+ADX trend-following) was never confirmed profitable
out-of-sample after costs, and Phase 3 found NO ML model with a robust
out-of-sample edge (see docs/ml_methodology.md, docs/known_limitations.md,
and the Phase 3 report). Every strategy's `reasoning` output says this
plainly rather than implying confidence the research doesn't support.

`quality` is always derived from a REAL, already-computed number (ADX
strength margin for the rule-based baseline, calibrated probability for the
ML strategy) -- never a fabricated confidence score for a strategy that
doesn't actually produce one.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import pandas as pd

from ml.evaluation.baselines import trend_following_signal_func, _precompute_indicators
from services.signal_engine.quality import bucket_signal_quality


@dataclass
class StrategySignal:
    signal_type: str  # LONG / SHORT / NO_TRADE
    entry_price: Optional[float]
    stop_loss: Optional[float]
    take_profit_1: Optional[float]
    take_profit_2: Optional[float]
    take_profit_3: Optional[float]
    quality: str  # LOW / MEDIUM / HIGH
    reasoning: str
    strategy_name: str
    model_version: Optional[str] = None


class SignalStrategy(ABC):
    name: str

    @abstractmethod
    def generate(self, df: pd.DataFrame) -> StrategySignal:
        ...


class BaselineStrategy(SignalStrategy):
    """Wraps the Phase 2.6 rule-based Donchian+ADX trend-following baseline
    -- the strongest candidate found so far, still NOT a verified edge.
    Quality reflects how far the real ADX reading is above the strategy's
    own firing threshold, not a probability (this strategy never produces
    one)."""

    name = "trend_following_donchian_adx"

    def __init__(self, breakout_period: int = 20, adx_threshold: float = 25):
        self._signal_func = trend_following_signal_func(breakout_period, adx_threshold)
        self._adx_threshold = adx_threshold

    def generate(self, df: pd.DataFrame) -> StrategySignal:
        prepared = _precompute_indicators(df)
        raw = self._signal_func(prepared)
        disclaimer = (
            "Rule-based baseline (Donchian+ADX). Walk-forward tested in Phase 2.6 "
            "(18/24 4h folds profitable, mean +0.61%/fold) but never confirmed as a "
            "robust, cost-surviving edge -- treat as a research heuristic, not a "
            "guaranteed outcome."
        )

        if raw is None:
            return StrategySignal(
                signal_type="NO_TRADE", entry_price=None, stop_loss=None,
                take_profit_1=None, take_profit_2=None, take_profit_3=None,
                quality="LOW", reasoning=f"No breakout/trend-strength condition met. {disclaimer}",
                strategy_name=self.name,
            )

        latest = prepared.iloc[-1]
        adx_value = float(latest.get("_adx_14", self._adx_threshold))
        margin = max(0.0, adx_value - self._adx_threshold)
        # A real, computed margin -- not a fabricated probability. Thresholds
        # are arbitrary display tiers (documented), not a validated accuracy claim.
        quality = "HIGH" if margin >= 15 else ("MEDIUM" if margin >= 5 else "LOW")

        return StrategySignal(
            signal_type=raw["signal_type"],
            entry_price=float(latest["close"]),
            stop_loss=raw.get("stop_loss"),
            take_profit_1=raw.get("take_profit_1"),
            take_profit_2=None,
            take_profit_3=None,
            quality=quality,
            reasoning=(
                f"{raw['signal_type']} breakout, ADX={adx_value:.1f} "
                f"({margin:.1f} above the {self._adx_threshold} firing threshold). {disclaimer}"
            ),
            strategy_name=self.name,
        )


class MLStrategy(SignalStrategy):
    """Wraps a Phase 3 calibrated model. Phase 3's own conclusion was that
    NO model tested showed a robust out-of-sample edge (the one promising-
    looking result did not replicate under walk-forward and collapsed under
    cost stress) -- so this strategy is NOT wired into any default live
    endpoint. It exists so a future, genuinely validated model has a slot to
    plug into, without ever being silently presented as proven."""

    name = "ml_model"

    def __init__(self, model, feature_cols: list[str], barrier_config, scaler=None, model_version: str = "unvalidated"):
        from ml.signal import make_ml_signal_func  # local import: only needed if this strategy is actually used

        self._signal_func = make_ml_signal_func(model, feature_cols, barrier_config, scaler=scaler)
        self._model_version = model_version

    def generate(self, df: pd.DataFrame) -> StrategySignal:
        raw = self._signal_func(df)
        disclaimer = (
            "ML-derived signal. Phase 3 found NO model with a robust, cost-surviving "
            "out-of-sample edge -- this is an experimental signal, not a validated one."
        )
        if raw is None or raw.get("signal_type") == "NO_TRADE":
            return StrategySignal(
                signal_type="NO_TRADE", entry_price=None, stop_loss=None,
                take_profit_1=None, take_profit_2=None, take_profit_3=None,
                quality="LOW", reasoning=f"Model did not clear its confidence/EV gate. {disclaimer}",
                strategy_name=self.name, model_version=self._model_version,
            )

        confidence = max(raw.get("long_probability", 0.0), raw.get("short_probability", 0.0))
        return StrategySignal(
            signal_type=raw["signal_type"],
            entry_price=float(df.iloc[-1]["close"]),
            stop_loss=raw.get("stop_loss"),
            take_profit_1=raw.get("take_profit_1"),
            take_profit_2=None,
            take_profit_3=None,
            quality=bucket_signal_quality(confidence),
            reasoning=f"{raw['signal_type']} (calibrated model probability={confidence:.2f}). {disclaimer}",
            strategy_name=self.name,
            model_version=self._model_version,
        )


class FutureStrategy(SignalStrategy):
    """Placeholder slot for a strategy that has not been built/validated
    yet. Deliberately raises rather than silently falling back to another
    strategy or fabricating output."""

    name = "future_strategy"

    def generate(self, df: pd.DataFrame) -> StrategySignal:
        raise NotImplementedError("FutureStrategy has no implementation yet -- this is a placeholder slot.")

"""Bridges a trained, calibrated ML model into the Backtester's signal_func
contract, with an explicit confidence threshold AND an expected-value gate
-- per the Phase 3 brief, the model must not trade every prediction, and
must never decide position size/leverage/risk itself (that remains the
risk engine's job, via Backtester._open_trade -> RiskEngine.calculate_position_size).

The trade's stop/target use the SAME ATR-barrier definition the label was
built from (see ml/labeling.py) -- if the model predicts "this setup
satisfies a 2R:1R barrier", the simulated trade must use that same barrier,
not an arbitrary one, otherwise the backtest would not actually be testing
what the model was trained to predict.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ml.labeling import TripleBarrierConfig, LONG_LABEL, SHORT_LABEL, NO_TRADE_LABEL


@dataclass
class MLSignalConfig:
    probability_threshold: float = 0.45   # minimum calibrated probability for the predicted side
    min_expected_value_r: float = 0.15    # minimum expected value, in R, after a rough cost haircut
    cost_haircut_r: float = 0.05          # rough fee+funding+slippage drag subtracted from EV, in R
    leverage: int = 1


def expected_value_r(p_favorable: float, reward_r: float, risk_r: float, cost_haircut_r: float) -> float:
    """EV in R-multiples: P(favorable) * reward - P(unfavorable) * risk,
    minus a rough cost haircut. Reward/risk come directly from the same
    ATR-barrier ratio the label was trained against."""
    return p_favorable * reward_r - (1 - p_favorable) * risk_r - cost_haircut_r


def make_ml_signal_func(
    model,
    feature_cols: list[str],
    barrier_config: TripleBarrierConfig,
    signal_config: MLSignalConfig | None = None,
    scaler=None,
):
    """model.predict_proba(X) must return columns ordered [SHORT, NO_TRADE, LONG]
    (class labels {-1: 0, 0: 1, 1: 2} -- the same convention
    ml.training.trainer.ModelTrainer.prepare_data uses)."""
    signal_config = signal_config or MLSignalConfig()
    reward_r = barrier_config.tp_atr_multiple / barrier_config.sl_atr_multiple

    def _signal(d: pd.DataFrame) -> dict | None:
        if len(d) < 2:
            return None
        row = d.iloc[-1]
        if row[feature_cols].isna().any():
            return None
        atr_val = row.get(barrier_config.atr_col)
        if not pd.notna(atr_val) or atr_val <= 0:
            return None

        X = row[feature_cols].to_numpy(dtype=float).reshape(1, -1)
        if scaler is not None:
            X = scaler.transform(X)

        proba = model.predict_proba(X)[0]
        short_p, no_trade_p, long_p = float(proba[0]), float(proba[1]), float(proba[2])

        entry_price = float(row["close"])

        best_side = None
        if long_p >= signal_config.probability_threshold and long_p > short_p:
            ev = expected_value_r(long_p, reward_r, 1.0, signal_config.cost_haircut_r)
            if ev >= signal_config.min_expected_value_r:
                best_side = "LONG"
        elif short_p >= signal_config.probability_threshold and short_p > long_p:
            ev = expected_value_r(short_p, reward_r, 1.0, signal_config.cost_haircut_r)
            if ev >= signal_config.min_expected_value_r:
                best_side = "SHORT"

        if best_side is None:
            return None  # NO_TRADE is a valid, expected, frequent output

        if best_side == "LONG":
            stop = entry_price - barrier_config.sl_atr_multiple * atr_val
            target = entry_price + barrier_config.tp_atr_multiple * atr_val
        else:
            stop = entry_price + barrier_config.sl_atr_multiple * atr_val
            target = entry_price - barrier_config.tp_atr_multiple * atr_val

        return {
            "signal_type": best_side,
            "stop_loss": stop,
            "take_profit_1": target,
            "leverage": signal_config.leverage,
            "long_probability": long_p,
            "short_probability": short_p,
            "no_trade_probability": no_trade_p,
        }

    return _signal

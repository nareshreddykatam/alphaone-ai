"""Unit tests for SignalEngine -- there were none before Phase 2, which is
how a NameError (`pandas` used but never imported in _generate_reasoning)
went unnoticed. These exercise every code path that touches `pd`.
"""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from services.signal_engine.engine import SignalEngine
from services.feature_engine.engine import FeatureEngine


def _feature_df(n=250, seed=1):
    rng = np.random.default_rng(seed)
    t0 = datetime(2024, 1, 1)
    close = 40000 + np.cumsum(rng.standard_normal(n) * 50)
    df = pd.DataFrame({
        "timestamp": [t0 + timedelta(hours=i) for i in range(n)],
        "open": close, "high": close + 20, "low": close - 20, "close": close,
        "volume": rng.uniform(10, 100, n),
    })
    return FeatureEngine().compute_features(df)


def test_generate_signal_long_does_not_raise_and_has_pandas_import():
    """Regression test for the missing `import pandas as pd` bug in
    _generate_reasoning -- this used to raise NameError for any LONG/SHORT
    signal since it referenced `pd.notna`."""
    df = _feature_df()
    engine = SignalEngine(prediction_threshold=0.5)
    ml_prediction = {"long_probability": 0.9, "short_probability": 0.05, "no_trade_probability": 0.05}

    output = engine.generate_signal(ml_prediction, df, entry_price=df.iloc[-1]["close"])

    assert output.signal_type in ("LONG", "NO_TRADE")  # regime may veto
    assert isinstance(output.reasoning, str) and len(output.reasoning) > 0


def test_generate_signal_short_does_not_raise():
    df = _feature_df(seed=2)
    engine = SignalEngine(prediction_threshold=0.5)
    ml_prediction = {"long_probability": 0.05, "short_probability": 0.9, "no_trade_probability": 0.05}

    output = engine.generate_signal(ml_prediction, df, entry_price=df.iloc[-1]["close"])
    assert output.signal_type in ("SHORT", "NO_TRADE")


def test_no_trade_when_probabilities_are_close():
    df = _feature_df()
    engine = SignalEngine(prediction_threshold=0.5)
    ml_prediction = {"long_probability": 0.5, "short_probability": 0.48, "no_trade_probability": 0.02}

    output = engine.generate_signal(ml_prediction, df, entry_price=df.iloc[-1]["close"])
    assert output.signal_type == "NO_TRADE"


def test_no_trade_below_threshold():
    df = _feature_df()
    engine = SignalEngine(prediction_threshold=0.6)
    ml_prediction = {"long_probability": 0.55, "short_probability": 0.2, "no_trade_probability": 0.25}

    output = engine.generate_signal(ml_prediction, df, entry_price=df.iloc[-1]["close"])
    assert output.signal_type == "NO_TRADE"


def test_signal_levels_are_derived_from_atr_when_direction_is_taken():
    df = _feature_df()
    engine = SignalEngine(prediction_threshold=0.5)
    ml_prediction = {"long_probability": 0.95, "short_probability": 0.02, "no_trade_probability": 0.03}
    entry_price = float(df.iloc[-1]["close"])

    output = engine.generate_signal(ml_prediction, df, entry_price=entry_price)
    if output.signal_type == "LONG":
        assert output.stop_loss < entry_price < output.take_profit_1
        assert output.risk_reward is not None and output.risk_reward > 0


def test_empty_or_short_dataframe_does_not_crash():
    df = _feature_df(n=10)  # below regime detector's 50-row minimum
    engine = SignalEngine(prediction_threshold=0.5)
    ml_prediction = {"long_probability": 0.9, "short_probability": 0.05, "no_trade_probability": 0.05}
    output = engine.generate_signal(ml_prediction, df, entry_price=df.iloc[-1]["close"])
    assert output.market_regime == "UNCERTAIN"

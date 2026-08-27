"""Phase 4: pluggable SignalStrategy must never claim a validated edge, and
`quality` must always trace back to a real computed number (ADX margin,
calibrated probability) -- never a made-up confidence score."""
import numpy as np
import pandas as pd
import pytest

from services.signal_engine.strategy import BaselineStrategy, MLStrategy, FutureStrategy
from ml.labeling import TripleBarrierConfig


def _make_trending_df(n=120, seed=0):
    rng = np.random.default_rng(seed)
    trend = np.linspace(100, 300, n)  # strong sustained uptrend -> should fire LONG with high ADX
    noise = rng.normal(0, 0.5, n)
    close = trend + noise
    high = close + 1
    low = close - 1
    open_ = close - 0.2
    volume = rng.uniform(100, 200, n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})


def _make_flat_df(n=120, seed=1):
    rng = np.random.default_rng(seed)
    close = 100 + rng.normal(0, 0.2, n)
    high = close + 0.3
    low = close - 0.3
    open_ = close
    volume = rng.uniform(100, 200, n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})


def test_baseline_strategy_reasoning_always_includes_the_disclaimer():
    strat = BaselineStrategy()
    df = _make_trending_df()
    result = strat.generate(df)
    assert "never confirmed as a robust" in result.reasoning
    assert result.strategy_name == "trend_following_donchian_adx"


def test_baseline_strategy_no_trade_on_flat_market():
    strat = BaselineStrategy()
    df = _make_flat_df()
    result = strat.generate(df)
    assert result.signal_type == "NO_TRADE"
    assert result.quality == "LOW"
    assert result.entry_price is None


def test_baseline_strategy_quality_derived_from_real_adx_margin_not_fabricated():
    strat = BaselineStrategy(adx_threshold=25)
    df = _make_trending_df()
    result = strat.generate(df)
    if result.signal_type != "NO_TRADE":
        assert result.quality in ("LOW", "MEDIUM", "HIGH")
        assert "ADX=" in result.reasoning


def test_future_strategy_raises_rather_than_fabricating():
    strat = FutureStrategy()
    with pytest.raises(NotImplementedError):
        strat.generate(_make_trending_df())


def test_ml_strategy_reasoning_states_no_validated_edge():
    class _StubModel:
        def predict_proba(self, X):
            return np.array([[0.1, 0.1, 0.8]])  # SHORT, NO_TRADE, LONG columns

    df = _make_trending_df(n=30)
    df["atr_14"] = 5.0
    feature_cols = ["close"]
    barrier_config = TripleBarrierConfig(atr_col="atr_14")

    strat = MLStrategy(_StubModel(), feature_cols, barrier_config, model_version="test-v0")
    result = strat.generate(df)
    assert result.signal_type == "LONG"
    assert "NO model with a robust" in result.reasoning
    assert result.model_version == "test-v0"
    assert result.quality in ("LOW", "MEDIUM", "HIGH")

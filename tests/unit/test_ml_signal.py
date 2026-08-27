"""Phase 3: the ML->Backtester signal bridge -- threshold logic, expected-
value gate, NO_TRADE as a valid frequent output, and confirmation that the
model never touches position sizing/leverage/risk (that stays the risk
engine's job)."""
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from ml.signal import make_ml_signal_func, MLSignalConfig, expected_value_r
from ml.labeling import TripleBarrierConfig
from services.backtester.engine import Backtester, BacktestConfig
from services.backtester.exchange_spec import ExchangeSpec


class _FakeModel:
    """predict_proba returns a fixed [short, no_trade, long] distribution
    regardless of input, so tests can pin exact probabilities."""
    def __init__(self, proba):
        self.proba = np.array([proba])

    def predict_proba(self, X):
        return np.repeat(self.proba, len(X), axis=0)


def _feature_row(atr=10.0, close=100.0):
    return {"timestamp": datetime(2024, 1, 1), "close": close, "atr_14": atr, "f1": 0.5, "f2": -0.3}


def test_expected_value_r_formula():
    # P=0.6, reward=2R, risk=1R, no haircut -> 0.6*2 - 0.4*1 = 0.8
    assert expected_value_r(0.6, reward_r=2.0, risk_r=1.0, cost_haircut_r=0.0) == pytest.approx(0.8)
    # at breakeven probability for a 2:1 reward:risk (P=1/3), EV should be ~0
    assert expected_value_r(1 / 3, reward_r=2.0, risk_r=1.0, cost_haircut_r=0.0) == pytest.approx(0.0, abs=1e-9)


def test_no_trade_when_probability_below_threshold():
    model = _FakeModel([0.2, 0.5, 0.3])  # long_p=0.3, below default threshold 0.45
    barrier = TripleBarrierConfig(tp_atr_multiple=2.0, sl_atr_multiple=1.0)
    signal_func = make_ml_signal_func(model, ["f1", "f2"], barrier)
    df = pd.DataFrame([_feature_row(), _feature_row()])
    assert signal_func(df) is None


def test_no_trade_when_ev_below_minimum_despite_threshold_met():
    # long_p=0.46 clears the 0.45 threshold but EV = 0.46*2 - 0.54*1 - 0.05 haircut = 0.35, still above min 0.15 in this case --
    # use a case where probability barely clears threshold but reward:risk is thin (1:1) so EV is low.
    model = _FakeModel([0.1, 0.44, 0.46])
    barrier = TripleBarrierConfig(tp_atr_multiple=1.0, sl_atr_multiple=1.0)  # 1:1 R:R
    config = MLSignalConfig(probability_threshold=0.45, min_expected_value_r=0.15, cost_haircut_r=0.05)
    signal_func = make_ml_signal_func(model, ["f1", "f2"], barrier, config)
    df = pd.DataFrame([_feature_row(), _feature_row()])
    # EV = 0.46*1 - 0.54*1 - 0.05 = -0.13 -- below min_expected_value_r
    assert signal_func(df) is None


def test_long_signal_emitted_when_both_threshold_and_ev_satisfied():
    model = _FakeModel([0.1, 0.15, 0.75])
    barrier = TripleBarrierConfig(tp_atr_multiple=2.0, sl_atr_multiple=1.0)
    signal_func = make_ml_signal_func(model, ["f1", "f2"], barrier)
    df = pd.DataFrame([_feature_row(atr=10.0, close=95.0), _feature_row(atr=10.0, close=100.0)])
    signal = signal_func(df)
    assert signal is not None
    assert signal["signal_type"] == "LONG"
    assert signal["stop_loss"] == pytest.approx(90.0)   # entry - 1*ATR
    assert signal["take_profit_1"] == pytest.approx(120.0)  # entry + 2*ATR


def test_short_signal_uses_mirrored_barriers():
    model = _FakeModel([0.75, 0.15, 0.1])
    barrier = TripleBarrierConfig(tp_atr_multiple=2.0, sl_atr_multiple=1.0)
    signal_func = make_ml_signal_func(model, ["f1", "f2"], barrier)
    df = pd.DataFrame([_feature_row(atr=10.0, close=95.0), _feature_row(atr=10.0, close=100.0)])
    signal = signal_func(df)
    assert signal["signal_type"] == "SHORT"
    assert signal["stop_loss"] == pytest.approx(110.0)
    assert signal["take_profit_1"] == pytest.approx(80.0)


def test_no_trade_dominant_class_produces_no_signal():
    model = _FakeModel([0.1, 0.8, 0.1])
    barrier = TripleBarrierConfig(tp_atr_multiple=2.0, sl_atr_multiple=1.0)
    signal_func = make_ml_signal_func(model, ["f1", "f2"], barrier)
    df = pd.DataFrame([_feature_row(), _feature_row()])
    assert signal_func(df) is None


def test_missing_feature_values_produce_no_signal_not_a_crash():
    model = _FakeModel([0.1, 0.15, 0.75])
    barrier = TripleBarrierConfig(tp_atr_multiple=2.0, sl_atr_multiple=1.0)
    signal_func = make_ml_signal_func(model, ["f1", "f2"], barrier)
    row = _feature_row()
    row["f1"] = np.nan
    df = pd.DataFrame([_feature_row(), row])
    assert signal_func(df) is None


def test_zero_or_missing_atr_produces_no_signal():
    model = _FakeModel([0.1, 0.15, 0.75])
    barrier = TripleBarrierConfig(tp_atr_multiple=2.0, sl_atr_multiple=1.0)
    signal_func = make_ml_signal_func(model, ["f1", "f2"], barrier)
    df = pd.DataFrame([_feature_row(), _feature_row(atr=0.0)])
    assert signal_func(df) is None


def test_signal_never_specifies_position_size_or_max_risk():
    """The ML signal dict must only ever carry direction/stop/target/
    leverage-hint -- never a raw position size or risk override. Sizing
    remains RiskEngine.calculate_position_size's exclusive responsibility."""
    model = _FakeModel([0.1, 0.15, 0.75])
    barrier = TripleBarrierConfig(tp_atr_multiple=2.0, sl_atr_multiple=1.0)
    signal_func = make_ml_signal_func(model, ["f1", "f2"], barrier)
    df = pd.DataFrame([_feature_row(), _feature_row()])
    signal = signal_func(df)
    forbidden_keys = {"quantity", "position_size", "max_risk", "max_drawdown", "max_leverage_override"}
    assert forbidden_keys.isdisjoint(signal.keys())


def test_ml_signal_integrates_with_real_backtester_and_risk_engine():
    """End-to-end: the ML signal_func drives the SAME Backtester/RiskEngine
    every baseline uses -- no special-cased ML backtester."""
    rows = []
    price = 100.0
    t = datetime(2024, 1, 1)
    for i in range(10):
        rows.append({"timestamp": t, "open": price, "high": price + 1, "low": price - 1,
                     "close": price, "atr_14": 5.0, "f1": 0.1, "f2": 0.1})
        t += timedelta(hours=4)
    df = pd.DataFrame(rows)

    model = _FakeModel([0.1, 0.15, 0.75])  # always confidently LONG
    barrier = TripleBarrierConfig(tp_atr_multiple=2.0, sl_atr_multiple=1.0)
    signal_func = make_ml_signal_func(model, ["f1", "f2"], barrier)

    bt = Backtester(BacktestConfig(initial_capital=10000, exchange_spec=ExchangeSpec(slippage_bps=0)))
    result = bt.run(df, signal_func)
    assert result is not None
    assert result.initial_capital == 10000

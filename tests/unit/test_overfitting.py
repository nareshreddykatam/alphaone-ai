import pandas as pd

from ml.evaluation.overfitting import (
    check_train_test_gap, check_fold_return_concentration,
    check_profitable_fold_rate, check_cost_sensitivity_collapse,
    feature_importance_stability,
)


def test_large_train_test_gap_is_flagged():
    train = {"accuracy": 0.9, "f1_weighted": 0.9, "auc_roc": 0.95}
    test = {"accuracy": 0.5, "f1_weighted": 0.5, "auc_roc": 0.55}
    flags = check_train_test_gap(train, test, max_gap=0.15)
    assert len(flags) == 3


def test_small_train_test_gap_is_not_flagged():
    train = {"accuracy": 0.55, "f1_weighted": 0.5, "auc_roc": 0.55}
    test = {"accuracy": 0.52, "f1_weighted": 0.48, "auc_roc": 0.53}
    flags = check_train_test_gap(train, test, max_gap=0.15)
    assert flags == []


def test_return_concentrated_in_one_fold_is_flagged():
    fold_returns = [10.0, 0.1, 0.1, -0.5, 0.2]
    flags = check_fold_return_concentration(fold_returns, top_n=1)
    assert len(flags) == 1


def test_evenly_spread_returns_not_flagged():
    fold_returns = [1.0, 1.2, 0.9, 1.1, 0.8]
    flags = check_fold_return_concentration(fold_returns, top_n=1)
    assert flags == []


def test_low_profitable_fold_rate_is_flagged():
    fold_returns = [-1, -1, -1, 1, -1]  # 1/5 = 20% profitable
    flags = check_profitable_fold_rate(fold_returns, min_rate=0.4)
    assert len(flags) == 1


def test_cost_sensitivity_collapse_is_flagged():
    flags = check_cost_sensitivity_collapse(base_return=10.0, stressed_return=1.0, max_drop_pct=80.0)
    assert len(flags) == 1


def test_cost_sensitivity_robust_case_not_flagged():
    flags = check_cost_sensitivity_collapse(base_return=10.0, stressed_return=8.0, max_drop_pct=80.0)
    assert flags == []


def test_feature_importance_stability_identifies_consistent_vs_volatile_features():
    fold1 = pd.DataFrame({"feature": ["a", "b"], "importance": [0.5, 0.1]})
    fold2 = pd.DataFrame({"feature": ["a", "b"], "importance": [0.52, 0.9]})
    fold3 = pd.DataFrame({"feature": ["a", "b"], "importance": [0.48, 0.05]})
    stability = feature_importance_stability([fold1, fold2, fold3])

    row_a = stability[stability["feature"] == "a"].iloc[0]
    row_b = stability[stability["feature"] == "b"].iloc[0]
    assert row_a["cv"] < row_b["cv"], "feature 'a' is consistently important across folds, 'b' spikes in one fold"

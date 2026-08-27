"""Phase 3 ML training/evaluation pipeline: chronological split with
purge/embargo, per-model training (with train-only scaling where needed),
validation-only calibration, and real-backtest trading performance via the
shared Backtester -- the same one every baseline uses.
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import structlog

from ml.training.trainer import ModelTrainer
from ml.features.scaling import fit_transform_train_only
from ml.labeling import TripleBarrierConfig
from ml.signal import make_ml_signal_func, MLSignalConfig
from services.backtester.engine import Backtester, BacktestConfig, BacktestResult

logger = structlog.get_logger()

MODEL_NAMES = ["logistic_regression", "random_forest", "xgboost", "lightgbm", "ensemble"]
NEEDS_SCALING = {"logistic_regression"}


def chronological_split_with_embargo(
    df: pd.DataFrame, train_pct: float, val_pct: float, embargo: int,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Chronological train/val/test split with an embargo gap at BOTH
    boundaries, sized to the label horizon so no training or validation
    label's forward-looking window can overlap into the next split's data.
    """
    n = len(df)
    train_end = int(n * train_pct)
    val_start = train_end + embargo
    val_end = val_start + int(n * val_pct)
    test_start = val_end + embargo

    train = df.iloc[:train_end].reset_index(drop=True)
    val = df.iloc[val_start:val_end].reset_index(drop=True)
    test = df.iloc[test_start:].reset_index(drop=True)
    return train, val, test


def train_model(model_name: str, X_tr: np.ndarray, y_tr: np.ndarray, X_val: np.ndarray, y_val: np.ndarray, feature_names: list[str], trainer: ModelTrainer):
    if model_name == "logistic_regression":
        return trainer.train_logistic_regression(X_tr, y_tr)
    if model_name == "random_forest":
        return trainer.train_random_forest(X_tr, y_tr)
    if model_name == "xgboost":
        return trainer.train_xgboost(X_tr, y_tr, X_val, y_val, feature_names)
    if model_name == "lightgbm":
        return trainer.train_lightgbm(X_tr, y_tr, X_val, y_val)
    if model_name == "ensemble":
        trainer.train_random_forest(X_tr, y_tr)
        trainer.train_xgboost(X_tr, y_tr, X_val, y_val, feature_names)
        trainer.train_lightgbm(X_tr, y_tr, X_val, y_val)
        return trainer.train_ensemble(X_tr, y_tr, X_val, y_val)
    raise ValueError(f"Unknown model: {model_name}. Options: {MODEL_NAMES}")


def best_calibration(trainer: ModelTrainer, model, X_val, y_val):
    """Compares sigmoid (Platt) vs isotonic calibration on the VALIDATION
    split only, picks whichever has the lower Brier score there, and
    returns (calibrated_model, method_name, val_brier_by_method). The test
    set is never touched by this decision."""
    candidates = {}
    briers = {}
    for method in ("sigmoid", "isotonic"):
        try:
            calibrated = trainer.calibrate_model(model, X_val, y_val, method=method)
            metrics = trainer.evaluate(calibrated, X_val, y_val)
            candidates[method] = calibrated
            briers[method] = metrics["brier_score"]
        except Exception as e:  # isotonic can fail on tiny validation sets
            logger.warning("Calibration method failed", method=method, error=str(e))
    if not candidates:
        return model, "none", {}
    best_method = min(briers, key=briers.get)
    return candidates[best_method], best_method, briers


@dataclass
class AblationResult:
    ablation_name: str
    model_name: str
    calibration_method: str
    n_features: int
    train_rows: int
    val_rows: int
    test_rows: int
    classification_metrics: dict
    val_calibration_briers: dict
    trading_result: BacktestResult
    signal_config: MLSignalConfig


def run_single_combo(
    labeled_df: pd.DataFrame,
    feature_cols: list[str],
    model_name: str,
    ablation_name: str,
    barrier_config: TripleBarrierConfig,
    backtest_config: BacktestConfig,
    funding_rates: pd.DataFrame | None = None,
    train_pct: float = 0.6,
    val_pct: float = 0.2,
    signal_config: MLSignalConfig | None = None,
    model_path: str = "./ml/models/_scratch",
) -> AblationResult | None:
    embargo = barrier_config.horizon_bars + 2
    train_df, val_df, test_df = chronological_split_with_embargo(labeled_df, train_pct, val_pct, embargo)

    trainer = ModelTrainer(model_path=model_path)
    X_train, y_train = trainer.prepare_data(train_df, feature_cols, "label")
    X_val, y_val = trainer.prepare_data(val_df, feature_cols, "label")
    X_test, y_test = trainer.prepare_data(test_df, feature_cols, "label")

    if len(X_train) < 50 or len(X_val) < 20 or len(X_test) < 20:
        logger.warning("Skipping combo: insufficient rows after split/dropna", ablation=ablation_name, model=model_name)
        return None

    scaler = None
    X_train_used, X_val_used, X_test_used = X_train, X_val, X_test
    if model_name in NEEDS_SCALING:
        train_feat_df = pd.DataFrame(X_train, columns=feature_cols)
        val_feat_df = pd.DataFrame(X_val, columns=feature_cols)
        test_feat_df = pd.DataFrame(X_test, columns=feature_cols)
        scaled_train, (scaled_val, scaled_test), scaler = fit_transform_train_only(
            train_feat_df, [val_feat_df, test_feat_df], feature_cols,
        )
        X_train_used, X_val_used, X_test_used = scaled_train.values, scaled_val.values, scaled_test.values

    model = train_model(model_name, X_train_used, y_train, X_val_used, y_val, feature_cols, trainer)
    calibrated, cal_method, val_briers = best_calibration(trainer, model, X_val_used, y_val)
    classification_metrics = trainer.evaluate(calibrated, X_test_used, y_test)

    signal_config = signal_config or MLSignalConfig()
    signal_func = make_ml_signal_func(calibrated, feature_cols, barrier_config, signal_config, scaler=scaler)
    bt = Backtester(backtest_config)
    fold_funding = None
    if funding_rates is not None and len(funding_rates) > 0 and len(test_df) > 0:
        fold_funding = funding_rates[
            (funding_rates["timestamp"] >= test_df["timestamp"].iloc[0]) &
            (funding_rates["timestamp"] <= test_df["timestamp"].iloc[-1])
        ]
    trading_result = bt.run(test_df.reset_index(drop=True), signal_func, funding_rates=fold_funding)

    return AblationResult(
        ablation_name=ablation_name,
        model_name=model_name,
        calibration_method=cal_method,
        n_features=len(feature_cols),
        train_rows=len(X_train), val_rows=len(X_val), test_rows=len(X_test),
        classification_metrics=classification_metrics,
        val_calibration_briers=val_briers,
        trading_result=trading_result,
        signal_config=signal_config,
    )


def rolling_walk_forward_windows(
    n_rows: int, train_window: int, val_window: int, test_window: int, step: int, embargo: int,
) -> list[tuple[slice, slice, slice]]:
    """Rolling train/val/test window generator with an embargo gap at BOTH
    internal boundaries (train->val and val->test), sized to the label
    horizon so no fold's labels can see into the next split."""
    windows = []
    start = 0
    while True:
        train_end = start + train_window
        val_start = train_end + embargo
        val_end = val_start + val_window
        test_start = val_end + embargo
        test_end = test_start + test_window
        if test_end > n_rows:
            break
        windows.append((slice(start, train_end), slice(val_start, val_end), slice(test_start, test_end)))
        start += step
    return windows


@dataclass
class MLWalkForwardFold:
    fold: int
    train_period: str
    val_period: str
    test_period: str
    classification_metrics: dict
    trading_result: BacktestResult
    feature_importance: pd.DataFrame | None = None


def run_ml_walk_forward(
    labeled_df: pd.DataFrame,
    feature_cols: list[str],
    model_name: str,
    barrier_config: TripleBarrierConfig,
    backtest_config: BacktestConfig,
    funding_rates: pd.DataFrame | None = None,
    train_window: int = 1500,
    val_window: int = 300,
    test_window: int = 300,
    step: int = 300,
    signal_config: MLSignalConfig | None = None,
) -> list[MLWalkForwardFold]:
    embargo = barrier_config.horizon_bars + 2
    windows = rolling_walk_forward_windows(len(labeled_df), train_window, val_window, test_window, step, embargo)
    results = []

    for i, (train_slice, val_slice, test_slice) in enumerate(windows):
        train_df = labeled_df.iloc[train_slice].reset_index(drop=True)
        val_df = labeled_df.iloc[val_slice].reset_index(drop=True)
        test_df = labeled_df.iloc[test_slice].reset_index(drop=True)

        trainer = ModelTrainer(model_path="./ml/models/_scratch")
        X_train, y_train = trainer.prepare_data(train_df, feature_cols, "label")
        X_val, y_val = trainer.prepare_data(val_df, feature_cols, "label")
        X_test, y_test = trainer.prepare_data(test_df, feature_cols, "label")
        if len(X_train) < 50 or len(X_val) < 20 or len(X_test) < 20:
            logger.warning(f"Skipping ML walk-forward fold {i + 1}: insufficient rows")
            continue

        scaler = None
        X_train_used, X_val_used, X_test_used = X_train, X_val, X_test
        if model_name in NEEDS_SCALING:
            train_feat_df = pd.DataFrame(X_train, columns=feature_cols)
            val_feat_df = pd.DataFrame(X_val, columns=feature_cols)
            test_feat_df = pd.DataFrame(X_test, columns=feature_cols)
            scaled_train, (scaled_val, scaled_test), scaler = fit_transform_train_only(
                train_feat_df, [val_feat_df, test_feat_df], feature_cols,
            )
            X_train_used, X_val_used, X_test_used = scaled_train.values, scaled_val.values, scaled_test.values

        model = train_model(model_name, X_train_used, y_train, X_val_used, y_val, feature_cols, trainer)
        calibrated, cal_method, _ = best_calibration(trainer, model, X_val_used, y_val)
        classification_metrics = trainer.evaluate(calibrated, X_test_used, y_test)

        importance_df = None
        try:
            importance_df = trainer.get_feature_importance(model, feature_cols)
        except Exception:
            pass

        signal_func = make_ml_signal_func(calibrated, feature_cols, barrier_config, signal_config or MLSignalConfig(), scaler=scaler)
        bt = Backtester(backtest_config)
        fold_funding = None
        if funding_rates is not None and len(funding_rates) > 0:
            fold_funding = funding_rates[
                (funding_rates["timestamp"] >= test_df["timestamp"].iloc[0]) &
                (funding_rates["timestamp"] <= test_df["timestamp"].iloc[-1])
            ]
        trading_result = bt.run(test_df, signal_func, funding_rates=fold_funding)

        results.append(MLWalkForwardFold(
            fold=i + 1,
            train_period=f"{train_df['timestamp'].iloc[0]} -> {train_df['timestamp'].iloc[-1]}",
            val_period=f"{val_df['timestamp'].iloc[0]} -> {val_df['timestamp'].iloc[-1]}",
            test_period=f"{test_df['timestamp'].iloc[0]} -> {test_df['timestamp'].iloc[-1]}",
            classification_metrics=classification_metrics,
            trading_result=trading_result,
            feature_importance=importance_df,
        ))

    return results


def format_ml_walk_forward_table(results: list[MLWalkForwardFold]) -> pd.DataFrame:
    rows = []
    for r in results:
        tr = r.trading_result
        rows.append({
            "fold": r.fold, "train_period": r.train_period, "val_period": r.val_period, "test_period": r.test_period,
            "accuracy": r.classification_metrics["accuracy"], "auc_roc": r.classification_metrics["auc_roc"],
            "trades": tr.total_trades, "win_rate": tr.win_rate, "profit_factor": tr.profit_factor,
            "expectancy": tr.expectancy, "net_return_pct": tr.total_pnl_pct, "max_dd_pct": tr.max_drawdown_pct,
            "sharpe": tr.sharpe_ratio,
        })
    return pd.DataFrame(rows)


def build_model_metadata(
    model_name: str,
    ablation_name: str,
    feature_cols: list[str],
    barrier_config: TripleBarrierConfig,
    calibration_method: str,
    dataset_version: str,
    code_version: str,
    training_period: str,
    hyperparameters: dict | None = None,
) -> dict:
    """Everything Phase 3 section 31 requires to reproduce a trained model
    from its stored configuration and dataset alone."""
    return {
        "model_id": f"{model_name}_{ablation_name}",
        "model_version": "v1",
        "training_period": training_period,
        "feature_version": ablation_name,
        "feature_count": len(feature_cols),
        "feature_names": feature_cols,
        "label_version": (
            f"triple_barrier_h{barrier_config.horizon_bars}_"
            f"tp{barrier_config.tp_atr_multiple}_sl{barrier_config.sl_atr_multiple}_"
            f"minrr{barrier_config.min_risk_reward}"
        ),
        "hyperparameters": hyperparameters or {},
        "calibration_method": calibration_method,
        "dataset_hash": dataset_version,
        "code_version": code_version,
    }


def format_ablation_table(results: list[AblationResult]) -> pd.DataFrame:
    rows = []
    for r in results:
        if r is None:
            continue
        cm = r.classification_metrics
        tr = r.trading_result
        rows.append({
            "ablation": r.ablation_name,
            "model": r.model_name,
            "calibration": r.calibration_method,
            "n_features": r.n_features,
            "accuracy": cm["accuracy"],
            "f1_weighted": cm["f1_weighted"],
            "auc_roc": cm["auc_roc"],
            "brier": cm["brier_score"],
            "log_loss": cm["log_loss"],
            "trades": tr.total_trades,
            "win_rate": tr.win_rate,
            "profit_factor": tr.profit_factor,
            "expectancy": tr.expectancy,
            "sharpe": tr.sharpe_ratio,
            "sortino": tr.sortino_ratio,
            "max_dd_pct": tr.max_drawdown_pct,
            "net_return_pct": tr.total_pnl_pct,
        })
    return pd.DataFrame(rows)

import numpy as np
import pandas as pd
import structlog
from dataclasses import dataclass

from ml.training.trainer import ModelTrainer
from services.backtester.engine import Backtester, BacktestConfig, BacktestResult
from services.signal_engine.engine import SignalEngine

logger = structlog.get_logger()


@dataclass
class WalkForwardResult:
    period: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    accuracy: float
    auc_roc: float
    total_return: float
    sharpe: float
    max_drawdown: float
    total_trades: int


def _make_model_signal_func(model, feature_cols: list[str], signal_engine: SignalEngine):
    """Bridges a trained classifier's per-bar prediction into the same
    signal_func contract Backtester.run expects, via SignalEngine (so the
    model is evaluated through the exact same regime-gating and SL/TP
    logic real signals would use, not a shortcut classification metric).
    """

    def _signal(d: pd.DataFrame) -> dict | None:
        if len(d) < 2:
            return None
        row = d.iloc[-1]
        if row[feature_cols].isna().any():
            return None

        X = row[feature_cols].to_numpy(dtype=float).reshape(1, -1)
        proba = model.predict_proba(X)[0]
        ml_prediction = {
            "short_probability": float(proba[0]),
            "no_trade_probability": float(proba[1]),
            "long_probability": float(proba[2]),
        }

        entry_price = float(row["close"])
        output = signal_engine.generate_signal(ml_prediction, d, entry_price)
        if output.signal_type not in ("LONG", "SHORT"):
            return None

        return {
            "signal_type": output.signal_type,
            "stop_loss": output.stop_loss,
            "take_profit_1": output.take_profit_1,
            "leverage": 1,
        }

    return _signal


class WalkForwardValidator:
    def __init__(self, trainer: ModelTrainer, backtest_config: BacktestConfig | None = None):
        self.trainer = trainer
        self.backtest_config = backtest_config or BacktestConfig()

    def validate(
        self,
        splits: list[tuple[pd.DataFrame, pd.DataFrame]],
        feature_cols: list[str],
        label_col: str = "label",
        prediction_threshold: float = 0.55,
        funding_rates: pd.DataFrame | None = None,
    ) -> list[WalkForwardResult]:
        results = []

        for i, (train_df, test_df) in enumerate(splits):
            logger.info(f"Walk-forward period {i + 1}/{len(splits)}")

            X_train, y_train = self.trainer.prepare_data(train_df, feature_cols, label_col)
            X_test, y_test = self.trainer.prepare_data(test_df, feature_cols, label_col)

            if len(X_train) == 0 or len(X_test) == 0:
                logger.warning(f"Skipping period {i + 1}: insufficient data")
                continue

            split_point = int(len(X_train) * 0.85)
            X_tr, X_val = X_train[:split_point], X_train[split_point:]
            y_tr, y_val = y_train[:split_point], y_train[split_point:]

            model = self.trainer.train_xgboost(X_tr, y_tr, X_val, y_val, feature_cols)

            metrics = self.trainer.evaluate(model, X_test, y_test)

            # Real trading metrics for this fold: run the model's predictions
            # through the same SignalEngine + Backtester everything else
            # uses, rather than reporting only classification accuracy/AUC.
            signal_engine = SignalEngine(prediction_threshold=prediction_threshold)
            signal_func = _make_model_signal_func(model, feature_cols, signal_engine)
            bt = Backtester(self.backtest_config)
            fold_funding = None
            if funding_rates is not None and len(funding_rates) > 0:
                fold_funding = funding_rates[
                    (funding_rates["timestamp"] >= test_df["timestamp"].iloc[0]) &
                    (funding_rates["timestamp"] <= test_df["timestamp"].iloc[-1])
                ]
            bt_result = bt.run(test_df.reset_index(drop=True), signal_func, funding_rates=fold_funding)

            result = WalkForwardResult(
                period=i + 1,
                train_start=str(train_df["timestamp"].iloc[0]),
                train_end=str(train_df["timestamp"].iloc[-1]),
                test_start=str(test_df["timestamp"].iloc[0]),
                test_end=str(test_df["timestamp"].iloc[-1]),
                accuracy=metrics["accuracy"],
                auc_roc=metrics["auc_roc"],
                total_return=bt_result.total_pnl_pct,
                sharpe=bt_result.sharpe_ratio,
                max_drawdown=bt_result.max_drawdown_pct,
                total_trades=bt_result.total_trades,
            )

            results.append(result)

        self._report_results(results)
        return results

    def _report_results(self, results: list[WalkForwardResult]):
        if not results:
            logger.warning("No walk-forward results")
            return

        accuracies = [r.accuracy for r in results]
        aucs = [r.auc_roc for r in results]
        returns = [r.total_return for r in results]

        logger.info(
            "Walk-forward validation complete",
            periods=len(results),
            mean_accuracy=np.mean(accuracies),
            std_accuracy=np.std(accuracies),
            mean_auc=np.mean(aucs),
            min_accuracy=min(accuracies),
            max_accuracy=max(accuracies),
            mean_return_pct=np.mean(returns),
            periods_profitable=sum(1 for r in returns if r > 0),
        )

    def compare_with_baselines(
        self, ai_metrics: dict, baselines: dict[str, BacktestResult]
    ) -> pd.DataFrame:
        """`ai_metrics` should carry the same fields as a WalkForwardResult
        aggregate (accuracy/auc_roc/sharpe/max_drawdown/total_return).
        `baselines` is the dict[name, BacktestResult] returned by
        ml.evaluation.baselines.run_all_baselines -- real BacktestResult
        objects, not ad hoc dicts, so every row in this comparison went
        through the identical cost/execution model.
        """
        comparison = [{
            "strategy": "AI Model",
            "accuracy": ai_metrics.get("accuracy", 0),
            "auc_roc": ai_metrics.get("auc_roc", 0),
            "total_return_pct": ai_metrics.get("total_return", 0),
            "sharpe": ai_metrics.get("sharpe", 0),
            "max_drawdown_pct": ai_metrics.get("max_drawdown", 0),
            "total_trades": ai_metrics.get("total_trades", 0),
        }]

        for name, result in baselines.items():
            comparison.append({
                "strategy": name,
                "accuracy": None,
                "auc_roc": None,
                "total_return_pct": result.total_pnl_pct,
                "sharpe": result.sharpe_ratio,
                "max_drawdown_pct": result.max_drawdown_pct,
                "total_trades": result.total_trades,
            })

        return pd.DataFrame(comparison)

"""Walk-forward stability check for the (parameter-free, rule-based)
baseline strategies -- NOT model training or hyperparameter search. Each
fold's TEST window is backtested independently through the same shared
Backtester/cost model; the point is to see whether a baseline's behavior is
stable across time, not to fit anything to the training window (baselines
have no parameters to fit).
"""
from dataclasses import dataclass

import pandas as pd

from services.backtester.engine import BacktestConfig, BacktestResult
from ml.evaluation.baselines import run_baseline
from ml.datasets.loader import DatasetLoader


@dataclass
class FoldResult:
    fold: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    result: BacktestResult


def run_walk_forward_baseline(
    name: str,
    df: pd.DataFrame,
    config: BacktestConfig | None = None,
    funding_rates: pd.DataFrame | None = None,
    train_window: int = 2000,
    test_window: int = 500,
    step: int = 500,
    embargo: int = 50,
) -> list[FoldResult]:
    loader = DatasetLoader(db=None)
    splits = loader.create_walk_forward_splits(
        df, train_window=train_window, test_window=test_window, step=step, embargo=embargo,
    )

    fold_results = []
    for i, (train_df, test_df) in enumerate(splits):
        fold_funding = None
        if funding_rates is not None and len(funding_rates) > 0:
            fold_funding = funding_rates[
                (funding_rates["timestamp"] >= test_df["timestamp"].iloc[0]) &
                (funding_rates["timestamp"] <= test_df["timestamp"].iloc[-1])
            ]
        _, result = run_baseline(name, test_df.reset_index(drop=True), config, funding_rates=fold_funding)
        fold_results.append(FoldResult(
            fold=i + 1,
            train_start=str(train_df["timestamp"].iloc[0]),
            train_end=str(train_df["timestamp"].iloc[-1]),
            test_start=str(test_df["timestamp"].iloc[0]),
            test_end=str(test_df["timestamp"].iloc[-1]),
            result=result,
        ))
    return fold_results


def format_fold_report(strategy_name: str, folds: list[FoldResult]) -> str:
    if not folds:
        return f"{strategy_name}: no walk-forward folds could be generated (dataset too short for the configured window sizes)."

    lines = [f"Walk-forward folds for {strategy_name}:"]
    returns = []
    for f in folds:
        r = f.result
        returns.append(r.total_pnl_pct)
        lines.append(
            f"Fold {f.fold}: train {f.train_start} -> {f.train_end} | test {f.test_start} -> {f.test_end} | "
            f"return={r.total_pnl_pct:.2f}% dd={r.max_drawdown_pct:.2f}% pf={r.profit_factor:.2f} trades={r.total_trades}"
        )

    profitable_folds = sum(1 for r in returns if r > 0)
    lines.append(f"\nAggregate: {len(folds)} folds, {profitable_folds} profitable, "
                 f"mean return={sum(returns)/len(returns):.2f}%, "
                 f"min={min(returns):.2f}%, max={max(returns):.2f}%")
    return "\n".join(lines)

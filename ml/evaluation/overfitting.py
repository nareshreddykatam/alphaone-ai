"""Phase 3, section 30: mechanical checks for the standard overfitting
red flags. These are diagnostics, not gates -- the final report states
what was found, it doesn't hide it.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class OverfittingFlag:
    check: str
    detail: str


def check_train_test_gap(
    train_metrics: dict, test_metrics: dict, max_gap: float = 0.15,
) -> list[OverfittingFlag]:
    flags = []
    for key in ("accuracy", "f1_weighted", "auc_roc"):
        if key in train_metrics and key in test_metrics:
            gap = train_metrics[key] - test_metrics[key]
            if gap > max_gap:
                flags.append(OverfittingFlag(
                    f"train_test_gap_{key}",
                    f"train={train_metrics[key]:.3f} test={test_metrics[key]:.3f} gap={gap:.3f} > {max_gap}",
                ))
    return flags


def check_fold_return_concentration(fold_returns: list[float], top_n: int = 1) -> list[OverfittingFlag]:
    """Flags when a small number of folds account for most/all of the
    total return -- a sign the result is not broadly reproducible."""
    flags = []
    if not fold_returns or sum(abs(r) for r in fold_returns) == 0:
        return flags
    total = sum(fold_returns)
    if total <= 0:
        return flags
    sorted_returns = sorted(fold_returns, reverse=True)
    top_sum = sum(sorted_returns[:top_n])
    share = top_sum / total if total != 0 else 0
    if share > 0.6:
        flags.append(OverfittingFlag(
            "return_concentrated_in_few_folds",
            f"top {top_n} fold(s) of {len(fold_returns)} account for {share:.1%} of total positive return",
        ))
    return flags


def check_profitable_fold_rate(fold_returns: list[float], min_rate: float = 0.4) -> list[OverfittingFlag]:
    flags = []
    if not fold_returns:
        return flags
    rate = sum(1 for r in fold_returns if r > 0) / len(fold_returns)
    if rate < min_rate:
        flags.append(OverfittingFlag(
            "low_profitable_fold_rate",
            f"only {rate:.1%} of folds were profitable (< {min_rate:.0%})",
        ))
    return flags


def check_cost_sensitivity_collapse(base_return: float, stressed_return: float, max_drop_pct: float = 80.0) -> list[OverfittingFlag]:
    """Flags when doubling costs erases most of the base-case return."""
    flags = []
    if base_return <= 0:
        return flags
    drop_pct = (base_return - stressed_return) / base_return * 100
    if drop_pct > max_drop_pct:
        flags.append(OverfittingFlag(
            "return_collapses_under_higher_costs",
            f"base return {base_return:.2f}% drops {drop_pct:.0f}% under the stressed-cost scenario",
        ))
    return flags


def feature_importance_stability(importances_per_fold: list[pd.DataFrame]) -> pd.DataFrame:
    """`importances_per_fold` is a list of DataFrames each with columns
    [feature, importance] (one per walk-forward fold). Returns per-feature
    mean/std/coefficient-of-variation across folds and how many folds
    ranked it in the top 10 -- a feature that matters in every fold looks
    very different from one that spikes in a single fold."""
    if not importances_per_fold:
        return pd.DataFrame(columns=["feature", "mean_importance", "std_importance", "cv", "top10_fold_count"])

    all_features = sorted(set().union(*[set(df["feature"]) for df in importances_per_fold]))
    records = []
    top10_counts = {f: 0 for f in all_features}
    for df in importances_per_fold:
        top10 = set(df.sort_values("importance", ascending=False).head(10)["feature"])
        for f in top10:
            top10_counts[f] += 1

    for feature in all_features:
        values = []
        for df in importances_per_fold:
            match = df[df["feature"] == feature]
            values.append(float(match["importance"].iloc[0]) if len(match) else 0.0)
        mean_v = float(np.mean(values))
        std_v = float(np.std(values))
        cv = std_v / mean_v if mean_v > 0 else float("inf")
        records.append({
            "feature": feature, "mean_importance": round(mean_v, 5), "std_importance": round(std_v, 5),
            "cv": round(cv, 3) if np.isfinite(cv) else None, "top10_fold_count": top10_counts[feature],
        })

    return pd.DataFrame(records).sort_values("mean_importance", ascending=False).reset_index(drop=True)

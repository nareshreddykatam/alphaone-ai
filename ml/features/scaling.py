"""No scaler exists in the training pipeline today (tree-based models don't
need one -- see ml/training/trainer.py), but if one is ever added it MUST be
fit only on the training split and applied unchanged to validation/test.
This helper exists so that requirement has one obvious, tested place to live
rather than being re-implemented (and potentially gotten wrong) ad hoc later.
"""
import pandas as pd
from sklearn.preprocessing import StandardScaler


def fit_transform_train_only(
    train_df: pd.DataFrame,
    other_dfs: list[pd.DataFrame],
    cols: list[str],
) -> tuple[pd.DataFrame, list[pd.DataFrame], StandardScaler]:
    """Fit a StandardScaler on `train_df[cols]` only, then apply the SAME
    fitted transform to `train_df` and every frame in `other_dfs` (val/test).
    Never fits on validation/test data, and never re-fits per split.
    """
    scaler = StandardScaler()
    scaler.fit(train_df[cols].values)

    scaled_train = train_df.copy()
    scaled_train[cols] = scaler.transform(train_df[cols].values)

    scaled_others = []
    for df in other_dfs:
        scaled = df.copy()
        scaled[cols] = scaler.transform(df[cols].values)
        scaled_others.append(scaled)

    return scaled_train, scaled_others, scaler

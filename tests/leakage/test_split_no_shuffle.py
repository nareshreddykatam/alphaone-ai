import pandas as pd

from ml.datasets.loader import DatasetLoader
from tests.leakage.conftest import make_ohlcv


def test_split_chronological_preserves_order_and_no_overlap():
    df = make_ohlcv(1000)
    loader = DatasetLoader(db=None)
    train, val, test = loader.split_chronological(df, train_pct=0.7, val_pct=0.15, test_pct=0.15)

    assert list(train["timestamp"]) == sorted(train["timestamp"])
    assert list(val["timestamp"]) == sorted(val["timestamp"])
    assert list(test["timestamp"]) == sorted(test["timestamp"])

    assert train["timestamp"].max() <= val["timestamp"].min()
    assert val["timestamp"].max() <= test["timestamp"].min()

    assert len(train) + len(val) + len(test) == len(df)


def test_walk_forward_splits_respect_embargo_and_chronology():
    df = make_ohlcv(8000)
    loader = DatasetLoader(db=None)
    embargo = 100
    splits = loader.create_walk_forward_splits(df, train_window=3000, test_window=500, step=1000, embargo=embargo)

    assert len(splits) > 0, "expected at least one walk-forward split for this dataset size"

    for train_df, test_df in splits:
        assert list(train_df["timestamp"]) == sorted(train_df["timestamp"])
        assert list(test_df["timestamp"]) == sorted(test_df["timestamp"])

        # test must start strictly after train ends, with at least `embargo`
        # bars of gap -- this is what prevents a rolling-window feature
        # computed near the train/test boundary from bleeding across it.
        train_end_idx = train_df.index[-1]
        test_start_idx = test_df.index[0]
        assert test_start_idx - train_end_idx - 1 >= embargo, (
            f"embargo violated: only {test_start_idx - train_end_idx - 1} bars between "
            f"train end and test start, expected >= {embargo}"
        )
        assert train_df["timestamp"].max() < test_df["timestamp"].min()


def test_walk_forward_splits_never_reorder_across_folds():
    """Each successive fold should start no earlier than the previous fold
    (this is a forward walk, not a shuffled cross-validation)."""
    df = make_ohlcv(8000)
    loader = DatasetLoader(db=None)
    splits = loader.create_walk_forward_splits(df, train_window=3000, test_window=500, step=1000, embargo=100)

    prev_train_start = None
    for train_df, _ in splits:
        start = train_df["timestamp"].iloc[0]
        if prev_train_start is not None:
            assert start >= prev_train_start
        prev_train_start = start

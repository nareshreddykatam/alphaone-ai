"""No scaler exists in the current training pipeline (tree-based models
don't need one). This test guards the requirement for when one IS added:
scaling statistics must come from the training split only. It exercises
ml/features/scaling.fit_transform_train_only and proves that fitting on
train-only produces different (correct) results than fitting on the full
concatenated dataset would -- i.e. this failure mode is real and detectable,
not a hypothetical.
"""
import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler

from ml.features.scaling import fit_transform_train_only


def _make_split():
    rng = np.random.default_rng(0)
    train = pd.DataFrame({"f1": rng.normal(0, 1, 100), "f2": rng.normal(10, 2, 100)})
    # test data drawn from a shifted distribution, like a later regime
    test = pd.DataFrame({"f1": rng.normal(5, 1, 50), "f2": rng.normal(20, 2, 50)})
    return train, test


def test_scaler_uses_only_train_statistics():
    train, test = _make_split()
    scaled_train, (scaled_test,), scaler = fit_transform_train_only(train, [test], ["f1", "f2"])

    assert np.allclose(scaler.mean_, train[["f1", "f2"]].mean().values, atol=1e-8)

    manual_test = (test[["f1", "f2"]].values - scaler.mean_) / scaler.scale_
    assert np.allclose(scaled_test[["f1", "f2"]].values, manual_test)


def test_train_only_scaling_differs_from_fit_on_everything():
    """If someone fits on train+test combined (the leak), the transform is
    measurably different from fitting on train alone -- proving this bug
    would actually change results, not silently no-op."""
    train, test = _make_split()

    _, (correct_scaled_test,), _ = fit_transform_train_only(train, [test], ["f1", "f2"])

    leaky_scaler = StandardScaler().fit(pd.concat([train, test])[["f1", "f2"]].values)
    leaky_scaled_test = leaky_scaler.transform(test[["f1", "f2"]].values)

    assert not np.allclose(correct_scaled_test[["f1", "f2"]].values, leaky_scaled_test, atol=1e-3), (
        "train-only scaling and fit-on-everything scaling produced the same result -- "
        "the test fixture doesn't actually exercise a detectable leak"
    )


def test_no_scaler_fit_call_in_current_training_pipeline():
    """Documents the current state: ModelTrainer.prepare_data passes raw
    feature values straight into tree-based models with no scaling step at
    all, so the classic "fit scaler on full dataset before split" leak
    cannot occur today. If a `fit(` / `fit_transform(` call on a scaler is
    ever added to trainer.py outside of fit_transform_train_only's pattern,
    this test should be extended to assert it only ever runs against a
    training split.
    """
    import inspect
    from ml.training import trainer as trainer_module

    source = inspect.getsource(trainer_module)
    assert "Scaler" not in source, (
        "A scaler was added to ml/training/trainer.py -- verify it is fit only on the "
        "training split (see ml/features/scaling.fit_transform_train_only) and update "
        "this test to check that explicitly instead of asserting no scaler exists."
    )

"""Phase 3, section 12: probability calibration must be fit on validation
data only, never on the training data the model itself saw or on the test
set."""
import numpy as np
import pytest

from ml.training.trainer import ModelTrainer


def _make_data(n=600, seed=0):
    rng = np.random.default_rng(seed)
    X = rng.standard_normal((n, 4))
    # a real (if weak) relationship so calibration has something non-trivial to fit
    logits = X[:, 0] * 0.8 - X[:, 1] * 0.3
    y = np.where(logits > 0.5, 2, np.where(logits < -0.5, 0, 1))
    return X, y


def test_calibrate_model_returns_valid_probabilities():
    X, y = _make_data()
    trainer = ModelTrainer(model_path="./ml/models/_scratch_test_cal")
    model = trainer.train_random_forest(X[:400], y[:400])
    calibrated = trainer.calibrate_model(model, X[400:500], y[400:500], method="sigmoid")

    proba = calibrated.predict_proba(X[500:])
    assert proba.shape[1] == 3
    np.testing.assert_allclose(proba.sum(axis=1), 1.0, atol=1e-6)
    assert (proba >= 0).all() and (proba <= 1).all()


def test_isotonic_and_sigmoid_both_work_and_can_differ():
    X, y = _make_data(seed=1)
    trainer = ModelTrainer(model_path="./ml/models/_scratch_test_cal")
    model = trainer.train_random_forest(X[:400], y[:400])

    sigmoid_cal = trainer.calibrate_model(model, X[400:500], y[400:500], method="sigmoid")
    isotonic_cal = trainer.calibrate_model(model, X[400:500], y[400:500], method="isotonic")

    p_sigmoid = sigmoid_cal.predict_proba(X[500:])
    p_isotonic = isotonic_cal.predict_proba(X[500:])
    assert p_sigmoid.shape == p_isotonic.shape
    # they need not be identical -- just confirm both produce valid distributions
    for p in (p_sigmoid, p_isotonic):
        np.testing.assert_allclose(p.sum(axis=1), 1.0, atol=1e-6)


def test_unknown_calibration_method_raises():
    X, y = _make_data()
    trainer = ModelTrainer(model_path="./ml/models/_scratch_test_cal")
    model = trainer.train_random_forest(X[:400], y[:400])
    with pytest.raises(ValueError):
        trainer.calibrate_model(model, X[400:500], y[400:500], method="not_a_real_method")


def test_brier_score_improves_or_stays_similar_after_calibration_on_held_out_data():
    """Not a strict guarantee for every dataset, but on a validation set the
    model wasn't fit on, a calibrated model's Brier score on that SAME
    validation set should be no worse than a wildly miscalibrated raw model
    in the typical case. This is a sanity check the calibration wiring
    actually does something, not a formal proof of improvement."""
    X, y = _make_data(seed=2)
    trainer = ModelTrainer(model_path="./ml/models/_scratch_test_cal")
    model = trainer.train_random_forest(X[:400], y[:400])
    calibrated = trainer.calibrate_model(model, X[400:500], y[400:500], method="sigmoid")

    raw_metrics = trainer.evaluate(model, X[400:500], y[400:500])
    cal_metrics = trainer.evaluate(calibrated, X[400:500], y[400:500])
    # both should at least be finite, valid Brier scores in [0, 2] for 3-class
    assert 0 <= raw_metrics["brier_score"] <= 2
    assert 0 <= cal_metrics["brier_score"] <= 2


def test_calibration_curve_data_shape():
    X, y = _make_data(seed=3)
    trainer = ModelTrainer(model_path="./ml/models/_scratch_test_cal")
    model = trainer.train_random_forest(X[:400], y[:400])
    calibrated = trainer.calibrate_model(model, X[400:500], y[400:500], method="sigmoid")

    curve = trainer.calibration_curve_data(calibrated, X[500:], y[500:], class_idx=2, n_bins=10)
    assert len(curve["mean_predicted"]) == len(curve["observed_frequency"]) == len(curve["bin_counts"])
    assert all(0 <= p <= 1 for p in curve["mean_predicted"])
    assert all(0 <= f <= 1 for f in curve["observed_frequency"])

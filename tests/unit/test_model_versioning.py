"""Phase 3, section 31: every trained model must be reproducible from a
stored config + dataset -- verify the metadata contract and a real
save/load round-trip."""
import json
from pathlib import Path

import numpy as np
import pytest

from ml.training.trainer import ModelTrainer
from ml.labeling import TripleBarrierConfig
from ml.evaluation.ml_pipeline import build_model_metadata


REQUIRED_METADATA_KEYS = {
    "model_id", "model_version", "training_period", "feature_version",
    "label_version", "hyperparameters", "calibration_method", "dataset_hash", "code_version",
}


def test_model_metadata_contains_all_required_fields():
    barrier = TripleBarrierConfig(horizon_bars=12, tp_atr_multiple=2.0, sl_atr_multiple=1.0)
    meta = build_model_metadata(
        model_name="xgboost", ablation_name="A_technical_only", feature_cols=["f1", "f2"],
        barrier_config=barrier, calibration_method="sigmoid", dataset_version="abc123",
        code_version="content:deadbeef", training_period="2023-01-01 -> 2024-01-01",
    )
    assert REQUIRED_METADATA_KEYS.issubset(meta.keys())
    assert meta["label_version"] == "triple_barrier_h12_tp2.0_sl1.0_minrr1.5"


def test_model_save_and_load_round_trip(tmp_path):
    rng = np.random.default_rng(0)
    X = rng.standard_normal((100, 3))
    y = rng.integers(0, 3, 100)

    trainer = ModelTrainer(model_path=str(tmp_path))
    model = trainer.train_random_forest(X, y)

    barrier = TripleBarrierConfig()
    meta = build_model_metadata(
        model_name="random_forest", ablation_name="A_technical_only", feature_cols=["f1", "f2", "f3"],
        barrier_config=barrier, calibration_method="none", dataset_version="hash1",
        code_version="content:abc", training_period="2023 -> 2024",
    )
    trainer.feature_names = ["f1", "f2", "f3"]
    trainer.save_model(model, "random_forest", "v1", metadata=meta)

    model_file = tmp_path / "random_forest_v1.joblib"
    meta_file = tmp_path / "random_forest_v1_meta.json"
    assert model_file.exists()
    assert meta_file.exists()

    with open(meta_file) as f:
        loaded_meta = json.load(f)
    assert REQUIRED_METADATA_KEYS.issubset(loaded_meta.keys())
    assert loaded_meta["feature_names"] == ["f1", "f2", "f3"]

    loaded_model = trainer.load_model("random_forest", "v1")
    np.testing.assert_array_equal(loaded_model.predict(X), model.predict(X))


def test_load_missing_model_returns_none_not_a_crash(tmp_path):
    trainer = ModelTrainer(model_path=str(tmp_path))
    assert trainer.load_model("does_not_exist", "v99") is None

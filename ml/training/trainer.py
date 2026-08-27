import numpy as np
import pandas as pd
import xgboost as xgb
import lightgbm as lgb
from sklearn.ensemble import RandomForestClassifier, VotingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.calibration import CalibratedClassifierCV
from sklearn.frozen import FrozenEstimator
from sklearn.model_selection import cross_val_score
from sklearn.metrics import (
    accuracy_score, classification_report, confusion_matrix,
    roc_auc_score, log_loss, brier_score_loss, precision_recall_fscore_support
)
import joblib
import json
from datetime import datetime
from pathlib import Path
import structlog

logger = structlog.get_logger()


class ModelTrainer:
    def __init__(self, model_path: str = "./ml/models"):
        self.model_path = Path(model_path)
        self.model_path.mkdir(parents=True, exist_ok=True)
        self.models = {}
        self.calibrated_model = None
        self.feature_names: list[str] = []

    def prepare_data(
        self, df: pd.DataFrame, feature_cols: list[str], label_col: str = "label"
    ) -> tuple[np.ndarray, np.ndarray]:
        valid_mask = df[feature_cols + [label_col]].notna().all(axis=1)
        df_valid = df[valid_mask].copy()

        X = df_valid[feature_cols].values
        y = df_valid[label_col].values

        label_map = {-1: 0, 0: 1, 1: 2}
        y = np.array([label_map.get(int(v), 1) for v in y])

        return X, y

    def train_xgboost(
        self, X_train: np.ndarray, y_train: np.ndarray,
        X_val: np.ndarray, y_val: np.ndarray,
        feature_names: list[str] | None = None,
    ) -> xgb.XGBClassifier:
        model = xgb.XGBClassifier(
            n_estimators=500,
            max_depth=6,
            learning_rate=0.01,
            subsample=0.8,
            colsample_bytree=0.8,
            min_child_weight=5,
            gamma=0.1,
            reg_alpha=0.1,
            reg_lambda=1.0,
            objective="multi:softprob",
            num_class=3,
            eval_metric="mlogloss",
            early_stopping_rounds=50,
            random_state=42,
            use_label_encoder=False,
        )

        model.fit(
            X_train, y_train,
            eval_set=[(X_val, y_val)],
            verbose=False,
        )

        logger.info("XGBoost trained", best_iteration=model.best_iteration)
        self.models["xgboost"] = model
        return model

    def train_lightgbm(
        self, X_train: np.ndarray, y_train: np.ndarray,
        X_val: np.ndarray, y_val: np.ndarray,
    ) -> lgb.LGBMClassifier:
        model = lgb.LGBMClassifier(
            n_estimators=500,
            max_depth=6,
            learning_rate=0.01,
            subsample=0.8,
            colsample_bytree=0.8,
            min_child_weight=5,
            gamma=0.1,
            reg_alpha=0.1,
            reg_lambda=1.0,
            num_class=3,
            objective="multiclass",
            metric="multi_logloss",
            random_state=42,
            verbose=-1,
        )

        model.fit(
            X_train, y_train,
            eval_set=[(X_val, y_val)],
            callbacks=[lgb.early_stopping(50), lgb.log_evaluation(0)],
        )

        logger.info("LightGBM trained")
        self.models["lightgbm"] = model
        return model

    def train_logistic_regression(
        self, X_train: np.ndarray, y_train: np.ndarray, max_iter: int = 1000,
    ) -> LogisticRegression:
        """Model 1 of the Phase 3 model ladder -- the simplest baseline
        classifier. Unlike the tree models, logistic regression needs
        scaled inputs; the CALLER is responsible for scaling X_train with
        a scaler fit only on the training split (see
        ml/features/scaling.fit_transform_train_only) before calling this
        -- this method does not scale internally so it never accidentally
        fits a scaler on anything but what it's handed.
        """
        # multinomial handling is automatic in modern scikit-learn for
        # solvers that support it (lbfgs, the default) -- no multi_class
        # kwarg needed (removed in newer sklearn versions).
        model = LogisticRegression(max_iter=max_iter, class_weight=None, random_state=42)
        model.fit(X_train, y_train)
        logger.info("Logistic regression trained")
        self.models["logistic_regression"] = model
        return model

    def calibrate_model(
        self, model, X_val: np.ndarray, y_val: np.ndarray, method: str = "sigmoid",
    ):
        """Wraps an ALREADY-fitted model in CalibratedClassifierCV(cv="prefit"),
        fitting the calibration mapping on the VALIDATION split only --
        never on the training data the model itself was fit on, and never
        on the test set. `method` is "sigmoid" (Platt scaling) or
        "isotonic". See docs/ml_methodology.md.
        """
        if method not in ("sigmoid", "isotonic"):
            raise ValueError(f"Unknown calibration method: {method}")
        # FrozenEstimator is the modern replacement for the old cv="prefit":
        # it tells CalibratedClassifierCV the wrapped model is already fit
        # and must not be refit or cross-validated internally -- only the
        # calibration mapping itself is fit, directly on (X_val, y_val).
        calibrated = CalibratedClassifierCV(estimator=FrozenEstimator(model), method=method)
        calibrated.fit(X_val, y_val)
        return calibrated

    def train_random_forest(
        self, X_train: np.ndarray, y_train: np.ndarray,
    ) -> RandomForestClassifier:
        model = RandomForestClassifier(
            n_estimators=300,
            max_depth=10,
            min_samples_split=10,
            min_samples_leaf=5,
            random_state=42,
            n_jobs=-1,
        )
        model.fit(X_train, y_train)
        logger.info("Random Forest trained")
        self.models["random_forest"] = model
        return model

    def train_ensemble(
        self, X_train: np.ndarray, y_train: np.ndarray,
        X_val: np.ndarray, y_val: np.ndarray,
    ) -> VotingClassifier:
        estimators = []
        for name, model in self.models.items():
            estimators.append((name, model))

        ensemble = VotingClassifier(estimators=estimators, voting="soft")
        ensemble.fit(X_train, y_train)
        logger.info("Ensemble trained")
        self.calibrated_model = ensemble
        return ensemble

    def evaluate(self, model, X_test: np.ndarray, y_test: np.ndarray) -> dict:
        y_pred = model.predict(X_test)
        y_proba = model.predict_proba(X_test)

        accuracy = accuracy_score(y_test, y_pred)
        report = classification_report(y_test, y_pred, target_names=["SHORT", "NO_TRADE", "LONG"], output_dict=True)
        conf_matrix = confusion_matrix(y_test, y_pred)

        try:
            auc = roc_auc_score(y_test, y_proba, multi_class="ovr", average="weighted")
        except Exception:
            auc = 0

        try:
            ll = log_loss(y_test, y_proba)
        except Exception:
            ll = float("inf")

        # Multiclass Brier score: mean squared error between predicted
        # probability and the one-hot true label, averaged over classes --
        # the standard multiclass generalization of the binary Brier score.
        # Lower is better; a perfectly calibrated, confident, correct model
        # scores 0.
        try:
            n_classes = y_proba.shape[1]
            y_onehot = np.eye(n_classes)[y_test]
            brier = float(np.mean(np.sum((y_proba - y_onehot) ** 2, axis=1)))
        except Exception:
            brier = float("inf")

        precision, recall, f1, _ = precision_recall_fscore_support(
            y_test, y_pred, average="weighted", zero_division=0,
        )

        metrics = {
            "accuracy": round(accuracy, 4),
            "precision_weighted": round(float(precision), 4),
            "recall_weighted": round(float(recall), 4),
            "f1_weighted": round(float(f1), 4),
            "auc_roc": round(auc, 4),
            "log_loss": round(ll, 4),
            "brier_score": round(brier, 4),
            "classification_report": report,
            "confusion_matrix": conf_matrix.tolist(),
            "timestamp": datetime.utcnow().isoformat(),
        }

        logger.info("Model evaluated", accuracy=accuracy, auc=auc, brier=brier)
        return metrics

    def calibration_curve_data(self, model, X: np.ndarray, y: np.ndarray, class_idx: int, n_bins: int = 10) -> dict:
        """Reliability data for one class: for each predicted-probability
        bin, the mean predicted probability vs the observed frequency of
        that class actually occurring. A well-calibrated model's points
        fall near the y=x diagonal."""
        proba = model.predict_proba(X)[:, class_idx]
        y_binary = (y == class_idx).astype(int)
        bins = np.linspace(0, 1, n_bins + 1)
        bin_idx = np.clip(np.digitize(proba, bins) - 1, 0, n_bins - 1)

        mean_predicted, observed_freq, counts = [], [], []
        for b in range(n_bins):
            mask = bin_idx == b
            if mask.sum() == 0:
                continue
            mean_predicted.append(float(proba[mask].mean()))
            observed_freq.append(float(y_binary[mask].mean()))
            counts.append(int(mask.sum()))

        return {"mean_predicted": mean_predicted, "observed_frequency": observed_freq, "bin_counts": counts}

    def predict(self, model, X: np.ndarray) -> dict:
        proba = model.predict_proba(X[-1:])
        classes = [-1, 0, 1]

        prediction = {
            "short_probability": round(float(proba[0][0]), 4),
            "no_trade_probability": round(float(proba[0][1]), 4),
            "long_probability": round(float(proba[0][2]), 4),
        }

        predicted_class = classes[np.argmax(proba[0])]
        signal_map = {-1: "SHORT", 0: "NO_TRADE", 1: "LONG"}
        prediction["signal_type"] = signal_map[predicted_class]
        prediction["confidence"] = round(float(np.max(proba[0])), 4)

        return prediction

    def save_model(self, model, name: str, version: str, metadata: dict | None = None):
        model_file = self.model_path / f"{name}_{version}.joblib"
        joblib.dump(model, model_file)

        meta_file = self.model_path / f"{name}_{version}_meta.json"
        meta = {
            "name": name,
            "version": version,
            "saved_at": datetime.utcnow().isoformat(),
            "feature_names": self.feature_names,
            **(metadata or {}),
        }
        with open(meta_file, "w") as f:
            json.dump(meta, f, indent=2, default=str)

        logger.info("Model saved", name=name, version=version, path=str(model_file))

    def load_model(self, name: str, version: str):
        model_file = self.model_path / f"{name}_{version}.joblib"
        if model_file.exists():
            return joblib.load(model_file)
        logger.warning("Model file not found", path=str(model_file))
        return None

    def get_feature_importance(self, model, feature_names: list[str]) -> pd.DataFrame:
        if hasattr(model, "feature_importances_"):
            importance = model.feature_importances_
        elif hasattr(model, "coef_"):
            importance = np.abs(model.coef_).mean(axis=0)
        else:
            return pd.DataFrame({"feature": feature_names, "importance": [0] * len(feature_names)})

        fi = pd.DataFrame({
            "feature": feature_names[:len(importance)],
            "importance": importance,
        }).sort_values("importance", ascending=False).reset_index(drop=True)

        return fi

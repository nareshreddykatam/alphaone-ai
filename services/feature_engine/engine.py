import pandas as pd
import structlog

from services.feature_engine.indicators import compute_technical_indicators
from services.feature_engine.structure import detect_market_structure
from services.feature_engine.volume import compute_volume_features
from services.feature_engine.derivatives import compute_derivatives_features
from services.feature_engine.volatility import compute_volatility_features

logger = structlog.get_logger()


class FeatureEngine:
    def __init__(self):
        self.feature_names: list[str] = []

    def compute_features(
        self,
        df: pd.DataFrame,
        funding_rates: pd.DataFrame | None = None,
        open_interest: pd.DataFrame | None = None,
        liquidations: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        if df.empty:
            logger.warning("Empty dataframe passed to feature engine")
            return df

        df = df.copy()
        df = df.sort_values("timestamp").reset_index(drop=True)

        logger.info("Computing features", rows=len(df))

        df = compute_technical_indicators(df)
        df = detect_market_structure(df)
        df = compute_volume_features(df)
        df = compute_derivatives_features(df, funding_rates, open_interest, liquidations)
        df = compute_volatility_features(df)

        df = df.replace([float("inf"), float("-inf")], float("nan"))

        self.feature_names = [c for c in df.columns if c not in ["timestamp", "open", "high", "low", "close", "volume", "symbol", "timeframe"]]

        logger.info("Features computed", count=len(self.feature_names))
        return df

    def get_feature_matrix(self, df: pd.DataFrame, label_col: str | None = None) -> tuple[pd.DataFrame, list[str]]:
        feature_cols = [c for c in self.feature_names if c in df.columns]

        if label_col and label_col in df.columns:
            mask = df[feature_cols + [label_col]].notna().all(axis=1)
            valid_df = df[mask]
            return valid_df[feature_cols], feature_cols

        mask = df[feature_cols].notna().all(axis=1)
        return df.loc[mask, feature_cols], feature_cols

    def get_feature_importance(self, model, feature_names: list[str]) -> pd.DataFrame:
        if hasattr(model, "feature_importances_"):
            importance = model.feature_importances_
        elif hasattr(model, "coef_"):
            importance = abs(model.coef_)
        else:
            return pd.DataFrame({"feature": feature_names, "importance": [0] * len(feature_names)})

        fi = pd.DataFrame({
            "feature": feature_names[:len(importance)],
            "importance": importance,
        }).sort_values("importance", ascending=False)

        return fi

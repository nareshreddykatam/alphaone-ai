import pandas as pd
from services.feature_engine import FeatureEngine
from services.feature_engine.indicators import compute_technical_indicators
from services.feature_engine.structure import detect_market_structure
from services.feature_engine.volume import compute_volume_features
from services.feature_engine.volatility import compute_volatility_features


class FeaturePipeline:
    def __init__(self):
        self.feature_engine = FeatureEngine()

    def compute_all_features(
        self, df: pd.DataFrame,
        funding_rates: pd.DataFrame | None = None,
        open_interest: pd.DataFrame | None = None,
        liquidations: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        return self.feature_engine.compute_features(df, funding_rates, open_interest, liquidations)

    def get_feature_names(self) -> list[str]:
        return self.feature_engine.feature_names

    def compute_baseline_features(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df = compute_technical_indicators(df)
        df = detect_market_structure(df)
        df = compute_volume_features(df)
        df = compute_volatility_features(df)
        return df

    def get_baseline_feature_names(self) -> list[str]:
        return self.feature_engine.feature_names

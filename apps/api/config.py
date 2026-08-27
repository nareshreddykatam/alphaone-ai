from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    app_env: str = "development"
    app_debug: bool = True
    app_secret_key: str = "change-me"

    database_url: str = "postgresql+asyncpg://user:password@localhost:5432/alphaone"
    database_url_sync: str = "postgresql://user:password@localhost:5432/alphaone"

    redis_url: str = "redis://localhost:6379/0"

    exchange_id: str = "binance"
    exchange_api_key: str = ""
    exchange_api_secret: str = ""
    exchange_testnet: bool = True
    exchange_default_market: str = "BTC/USDT:USDT"

    coindcx_api_key: str = ""
    coindcx_api_secret: str = ""
    scheduler_enabled: bool = False
    market_data_ws_enabled: bool = False

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = False

    model_path: str = "./ml/models"
    model_version: str = "v1"
    prediction_threshold: float = 0.55

    risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 2.0
    max_drawdown_pct: float = 10.0
    max_leverage: int = 5
    max_positions: int = 1
    max_daily_trades: int = 10

    trading_mode: str = "paper"

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_cors_origins: str = "http://localhost:3000"

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.api_cors_origins.split(",")]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()

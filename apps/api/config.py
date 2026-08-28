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

    # Multi-Coin AI Futures System: external, read-only Telegram signal
    # ingestion (see services/telegram_signals/). Off by default, and
    # functionally inert even when true unless AlphaOne's bot has been
    # added as an ADMINISTRATOR of this channel by its owner -- a real
    # Telegram-platform precondition this code cannot arrange or fake.
    telegram_external_signals_enabled: bool = False
    telegram_external_signal_channel: str = "@suncrypto_trading_alerts"
    # Stable numeric channel ID, once known (resolved once via MTProto's
    # get_entity() -- see services/telegram_mtproto/setup_session.py's
    # printed output after a successful one-time login). Preferred over
    # the username for authorization when set, since a username can be
    # reassigned to a different channel later but the numeric ID cannot
    # (Phase 4). Empty by default -- username-only matching still applies.
    telegram_external_signal_channel_id: str = ""

    # Multi-Coin AI Futures System: read-only MTProto ingestion via a real
    # Telegram USER ACCOUNT (services/telegram_mtproto/), required because
    # the Bot API can only receive channel_post updates for a channel the
    # bot itself administers, and @suncrypto_trading_alerts is a third-
    # party channel AlphaOne's operator does not own or administer.
    # TELEGRAM_SESSION is a Telethon StringSession (produced ONCE, by the
    # user, running services/telegram_mtproto/setup_session.py themselves
    # in their own terminal -- never generated or seen by this codebase's
    # own automated processes) -- treat it exactly like a password: never
    # logged, never committed, never printed.
    telegram_mtproto_enabled: bool = False
    telegram_api_id: str = ""
    telegram_api_hash: str = ""
    telegram_session: str = ""

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

    # Live Futures Auto-Trading V1 (Phase 12): TWO independent env-var
    # gates, both required, neither sufficient alone -- deliberately not a
    # single flag. Both default false. Cannot be changed by a Telegram
    # message, strategy output, AI output, or any API/frontend request --
    # only by whoever controls this process's real environment variables.
    automatic_trading_enabled: bool = False
    live_execution_armed: bool = False
    # Hard ceiling on simultaneous REAL positions this system manages.
    # Deliberately reuses the existing, already-reviewed RiskConfig
    # default (max_positions=1, services/risk_engine/engine.py) rather
    # than inventing a new number for real money -- see Phase 16's own
    # instruction not to guess an appropriate default.
    max_open_positions_live: int = 1

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

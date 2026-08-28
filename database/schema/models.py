import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Float, Integer, Boolean, DateTime, Text, JSON, Enum as SAEnum, Index,
    ForeignKey, text,
)
from database.schema import Base
from database.schema.types import GUID as UUID
import enum


class SignalType(str, enum.Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    EXIT = "EXIT"
    NO_TRADE = "NO_TRADE"


class MarketRegime(str, enum.Enum):
    TRENDING_BULLISH = "TRENDING_BULLISH"
    TRENDING_BEARISH = "TRENDING_BEARISH"
    RANGING = "RANGING"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    BREAKOUT = "BREAKOUT"
    POST_LIQUIDATION = "POST_LIQUIDATION"
    UNCERTAIN = "UNCERTAIN"


class TradeStatus(str, enum.Enum):
    OPEN = "OPEN"
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class TradeSource(str, enum.Enum):
    MANUAL = "MANUAL"
    SUNCRYPTO_SYNC = "SUNCRYPTO_SYNC"  # Phase 4, kept for historical rows -- no longer the active exchange
    COINDCX_SYNC = "COINDCX_SYNC"
    AI_PAPER = "AI_PAPER"  # AI Trading V1: opened by services/signal_engine/ai_orchestrator.py + services/paper_trader, never a real fill


class SignalQuality(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class AccountConnectionStatus(str, enum.Enum):
    NOT_CONNECTED = "NOT_CONNECTED"
    MANUAL = "MANUAL"
    LIVE = "LIVE"


class DataSourceKind(str, enum.Enum):
    LIVE = "LIVE"
    SYNCED = "SYNCED"
    MANUAL = "MANUAL"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class ExecutionType(str, enum.Enum):
    ENTRY = "ENTRY"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    EXIT = "EXIT"


class SignalOutcomeType(str, enum.Enum):
    WIN = "WIN"
    LOSS = "LOSS"
    NO_TRADE = "NO_TRADE"
    EXPIRED = "EXPIRED"
    PENDING = "PENDING"


class SyncStatus(str, enum.Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"


class SignalMatchStatus(str, enum.Enum):
    MANUAL = "MANUAL"  # user-entered trade, normal Phase 4 matching already applied at open time
    AUTO_MATCHED = "AUTO_MATCHED"  # exchange-detected position, confidently matched to one signal
    AMBIGUOUS = "AMBIGUOUS"  # exchange-detected position, multiple signal candidates -- needs confirmation
    UNMATCHED = "UNMATCHED"  # exchange-detected position, no signal candidate found
    CONFIRMED = "CONFIRMED"  # an AMBIGUOUS match the user manually resolved


class ConnectionState(str, enum.Enum):
    LIVE = "LIVE"
    STALE = "STALE"
    DISCONNECTED = "DISCONNECTED"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class TradingMode(str, enum.Enum):
    PAPER = "paper"
    BACKTEST = "backtest"
    TESTNET = "testnet"
    LIVE = "live"


class DataQualityStatus(str, enum.Enum):
    VALID = "valid"
    INVALID = "invalid"
    GAP = "gap"
    STALE = "stale"


class Candle(Base):
    __tablename__ = "candles"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    timestamp = Column(DateTime, nullable=False)
    timeframe = Column(String(10), nullable=False)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)
    symbol = Column(String(20), nullable=False, default="BTC/USDT")
    source = Column(String(20), nullable=False, default="binance")
    ingested_at = Column(DateTime, default=datetime.utcnow)
    quality_status = Column(String(10), nullable=False, default=DataQualityStatus.VALID.value)
    quality_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_candles_symbol_timeframe_timestamp", "symbol", "timeframe", "timestamp", unique=True),
    )


class FundingRate(Base):
    __tablename__ = "funding_rates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol = Column(String(20), nullable=False, default="BTC/USDT")
    timestamp = Column(DateTime, nullable=False)
    rate = Column(Float, nullable=False)
    source = Column(String(20), nullable=False, default="binance")
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_funding_rates_symbol_timestamp", "symbol", "timestamp", unique=True),
    )


class OpenInterestRecord(Base):
    __tablename__ = "open_interest"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol = Column(String(20), nullable=False, default="BTC/USDT")
    timestamp = Column(DateTime, nullable=False)
    value = Column(Float, nullable=False)
    source = Column(String(20), nullable=False, default="binance")
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_open_interest_symbol_timestamp", "symbol", "timestamp", unique=True),
    )


class LiquidationEvent(Base):
    __tablename__ = "liquidations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol = Column(String(20), nullable=False, default="BTC/USDT")
    timestamp = Column(DateTime, nullable=False)
    side = Column(String(10), nullable=False)
    price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    source = Column(String(20), nullable=False, default="binance")
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_liquidations_symbol_timestamp", "symbol", "timestamp"),
    )


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_name = Column(String(50), nullable=False)
    strategy_version = Column(String(20), nullable=False, default="v1")
    symbol = Column(String(20), nullable=False, default="BTC/USDT")
    timeframe = Column(String(10), nullable=False)
    config_json = Column(JSON, nullable=False)
    dataset_start = Column(DateTime, nullable=False)
    dataset_end = Column(DateTime, nullable=False)
    dataset_version = Column(String(64), nullable=True)
    code_version = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BacktestMetric(Base):
    __tablename__ = "backtest_metrics"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(UUID(as_uuid=True), ForeignKey("backtest_runs.id"), nullable=False)
    total_pnl = Column(Float, default=0)
    total_pnl_pct = Column(Float, default=0)
    total_trades = Column(Integer, default=0)
    winning_trades = Column(Integer, default=0)
    losing_trades = Column(Integer, default=0)
    win_rate = Column(Float, default=0)
    profit_factor = Column(Float, default=0)
    expectancy = Column(Float, default=0)
    average_r = Column(Float, default=0)
    sharpe_ratio = Column(Float, default=0)
    sortino_ratio = Column(Float, default=0)
    max_drawdown = Column(Float, default=0)
    max_drawdown_pct = Column(Float, default=0)
    recovery_factor = Column(Float, default=0)
    average_trade_pnl = Column(Float, default=0)
    average_winning_trade = Column(Float, default=0)
    average_losing_trade = Column(Float, default=0)
    largest_win = Column(Float, default=0)
    largest_loss = Column(Float, default=0)
    consecutive_wins = Column(Integer, default=0)
    consecutive_losses = Column(Integer, default=0)
    total_fees = Column(Float, default=0)
    total_funding = Column(Float, default=0)
    initial_capital = Column(Float, default=0)
    final_capital = Column(Float, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_backtest_metrics_run_id", "run_id"),
    )


class Feature(Base):
    __tablename__ = "features"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    timestamp = Column(DateTime, nullable=False)
    timeframe = Column(String(10), nullable=False)
    symbol = Column(String(20), nullable=False, default="BTC/USDT")
    features = Column(JSON, nullable=False)
    feature_version = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_features_symbol_timeframe_timestamp", "symbol", "timeframe", "timestamp", unique=True),
    )


class Prediction(Base):
    __tablename__ = "predictions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    signal_id = Column(String(30), nullable=False, unique=True)
    timestamp = Column(DateTime, nullable=False)
    symbol = Column(String(20), nullable=False, default="BTC/USDT")
    signal_type = Column(String(10), nullable=False)
    long_probability = Column(Float, nullable=False)
    short_probability = Column(Float, nullable=False)
    no_trade_probability = Column(Float, nullable=False)
    confidence = Column(Float, nullable=False)
    market_regime = Column(String(20), nullable=False)
    features_used = Column(JSON)
    model_version = Column(String(20))
    feature_version = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_predictions_signal_id", "signal_id"),
    )


class Signal(Base):
    __tablename__ = "signals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    signal_id = Column(String(30), nullable=False, unique=True)
    timestamp = Column(DateTime, nullable=False)
    symbol = Column(String(20), nullable=False, default="BTC/USDT")
    signal_type = Column(String(10), nullable=False)
    confidence = Column(Float, nullable=False)
    entry_price = Column(Float)
    stop_loss = Column(Float)
    take_profit_1 = Column(Float)
    take_profit_2 = Column(Float)
    take_profit_3 = Column(Float)
    risk_reward = Column(Float)
    market_regime = Column(String(20))
    reasoning = Column(Text)
    expiry = Column(DateTime)
    is_active = Column(Boolean, default=True)
    quality = Column(String(10), nullable=True)
    strategy_name = Column(String(50), nullable=True)
    # Added for the multi-strategy system (10 independent strategies across
    # 15m/4h) -- lets the dedup guard (services/signal_engine/live_signal.py:
    # signal_already_exists_for_candle) scope "same event" to
    # (symbol, timeframe, strategy_name, timestamp) instead of just
    # (symbol, timestamp), which would otherwise treat two DIFFERENT
    # strategies firing on the same candle as duplicates of each other.
    # Nullable: existing rows predating this column (all S05/4h) are left
    # NULL rather than backfilled with a guess -- see
    # database/schema/migrations.py for how this column is added to an
    # already-existing production table.
    timeframe = Column(String(10), nullable=True)
    model_version = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Trade(Base):
    __tablename__ = "trades"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trade_id = Column(String(30), nullable=False, unique=True)
    signal_id = Column(String(30))
    symbol = Column(String(20), nullable=False, default="BTC/USDT")
    side = Column(String(10), nullable=False)
    status = Column(String(10), nullable=False, default="OPEN")
    mode = Column(String(10), nullable=False, default="paper")
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float)
    stop_loss = Column(Float)
    take_profit_1 = Column(Float)
    take_profit_2 = Column(Float)
    take_profit_3 = Column(Float)
    quantity = Column(Float, nullable=False)
    leverage = Column(Integer, default=1)
    pnl = Column(Float, default=0)
    pnl_pct = Column(Float, default=0)
    fees = Column(Float, default=0)
    funding = Column(Float, default=0)
    r_multiple = Column(Float, default=0)
    entry_time = Column(DateTime, nullable=False)
    exit_time = Column(DateTime)
    exit_reason = Column(String(50))
    market_regime = Column(String(20))
    is_manual_entry = Column(Boolean, nullable=False, default=True)
    source = Column(String(20), nullable=False, default=TradeSource.MANUAL.value)
    matched_signal_confidence = Column(Float, nullable=True)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=True)
    # Phase 5: exchange-synced live position fields. Null for manually-entered
    # trades -- rendered as N/A, never fabricated (see services/exchange/coindcx.py).
    exchange_position_id = Column(String(64), nullable=True)
    exchange_trade_id = Column(String(64), nullable=True)
    mark_price = Column(Float, nullable=True)
    liquidation_price = Column(Float, nullable=True)
    unrealized_pnl = Column(Float, nullable=True)
    margin = Column(Float, nullable=True)
    data_source = Column(String(20), nullable=False, default=DataSourceKind.MANUAL.value)
    match_status = Column(String(20), nullable=False, default=SignalMatchStatus.MANUAL.value)
    last_synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_trades_trade_id", "trade_id"),
        Index("ix_trades_exchange_trade_id", "exchange_trade_id", unique=True,
              sqlite_where=text("exchange_trade_id IS NOT NULL"),
              postgresql_where=text("exchange_trade_id IS NOT NULL")),
    )


class TradeExecution(Base):
    __tablename__ = "trade_executions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trade_id = Column(String(30), ForeignKey("trades.trade_id"), nullable=False)
    execution_type = Column(String(20), nullable=False)
    price = Column(Float, nullable=False)
    quantity = Column(Float, nullable=False)
    timestamp = Column(DateTime, nullable=False)
    note = Column(Text, nullable=True)
    # Phase 5: CoinDCX has no single documented unique trade-fill id, so
    # idempotent sync derives a deterministic key from order_id+timestamp+
    # price+quantity+side (see services/exchange/coindcx_sync.py) and stores
    # it here to prevent duplicate-execution inserts on repeated syncs.
    exchange_transaction_id = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_trade_executions_trade_id", "trade_id"),
        Index("ix_trade_executions_exchange_transaction_id", "exchange_transaction_id", unique=True,
              sqlite_where=text("exchange_transaction_id IS NOT NULL"),
              postgresql_where=text("exchange_transaction_id IS NOT NULL")),
    )


class SignalOutcome(Base):
    __tablename__ = "signal_outcomes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    signal_id = Column(String(30), ForeignKey("signals.signal_id"), nullable=False, unique=True)
    outcome = Column(String(20), nullable=False, default=SignalOutcomeType.PENDING.value)
    hypothetical_entry_price = Column(Float, nullable=True)
    hypothetical_exit_price = Column(Float, nullable=True)
    hypothetical_pnl = Column(Float, nullable=True)
    hypothetical_pnl_pct = Column(Float, nullable=True)
    hypothetical_r_multiple = Column(Float, nullable=True)
    was_taken_by_user = Column(Boolean, nullable=False, default=False)
    evaluated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_signal_outcomes_signal_id", "signal_id"),
    )


class Account(Base):
    __tablename__ = "accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    exchange = Column(String(20), nullable=False, default="coindcx")
    mode = Column(String(20), nullable=False, default=TradingMode.PAPER.value)
    connection_status = Column(String(20), nullable=False, default=AccountConnectionStatus.NOT_CONNECTED.value)
    base_currency = Column(String(10), nullable=False, default="USDT")
    label = Column(String(50), nullable=True)
    last_synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AccountSnapshot(Base):
    __tablename__ = "account_snapshots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    timestamp = Column(DateTime, nullable=False)
    equity = Column(Float, nullable=False)
    available_balance = Column(Float, nullable=True)
    used_margin = Column(Float, nullable=True)
    unrealized_pnl = Column(Float, nullable=True)
    realized_pnl = Column(Float, nullable=True)
    source = Column(String(20), nullable=False, default=DataSourceKind.MANUAL.value)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_account_snapshots_account_id_timestamp", "account_id", "timestamp"),
    )


class Deposit(Base):
    __tablename__ = "deposits"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    amount = Column(Float, nullable=False)
    timestamp = Column(DateTime, nullable=False)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Withdrawal(Base):
    __tablename__ = "withdrawals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False)
    amount = Column(Float, nullable=False)
    timestamp = Column(DateTime, nullable=False)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class SyncEvent(Base):
    __tablename__ = "sync_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source = Column(String(30), nullable=False)
    status = Column(String(20), nullable=False)
    detail = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)


class PerformanceMetrics(Base):
    __tablename__ = "performance_metrics"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    date = Column(DateTime, nullable=False)
    total_pnl = Column(Float, default=0)
    total_trades = Column(Integer, default=0)
    winning_trades = Column(Integer, default=0)
    losing_trades = Column(Integer, default=0)
    win_rate = Column(Float, default=0)
    profit_factor = Column(Float, default=0)
    max_drawdown = Column(Float, default=0)
    sharpe_ratio = Column(Float, default=0)
    sortino_ratio = Column(Float, default=0)
    expectancy = Column(Float, default=0)
    average_r = Column(Float, default=0)
    equity = Column(Float, default=10000)
    mode = Column(String(10), default="paper")
    created_at = Column(DateTime, default=datetime.utcnow)


class BotState(Base):
    __tablename__ = "bot_state"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key = Column(String(50), nullable=False, unique=True)
    value = Column(JSON, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class NotificationLog(Base):
    __tablename__ = "notification_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    signal_id = Column(String(30))
    channel = Column(String(20), nullable=False)
    message_type = Column(String(20), nullable=False)
    status = Column(String(20), nullable=False)
    error = Column(Text)
    sent_at = Column(DateTime, default=datetime.utcnow)

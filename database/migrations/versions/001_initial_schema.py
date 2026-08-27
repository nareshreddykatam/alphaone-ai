"""initial schema

Revision ID: 001
Revises:
Create Date: 2024-01-01
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

from database.schema.types import GUID as UUID

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "candles",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("timeframe", sa.String(10), nullable=False),
        sa.Column("open", sa.Float, nullable=False),
        sa.Column("high", sa.Float, nullable=False),
        sa.Column("low", sa.Float, nullable=False),
        sa.Column("close", sa.Float, nullable=False),
        sa.Column("volume", sa.Float, nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False, server_default="BTC/USDT"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_candles_symbol_timeframe_timestamp", "candles", ["symbol", "timeframe", "timestamp"], unique=True)

    op.create_table(
        "features",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("timeframe", sa.String(10), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False, server_default="BTC/USDT"),
        sa.Column("features", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_features_symbol_timeframe_timestamp", "features", ["symbol", "timeframe", "timestamp"], unique=True)

    op.create_table(
        "predictions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("signal_id", sa.String(30), nullable=False, unique=True),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False, server_default="BTC/USDT"),
        sa.Column("signal_type", sa.String(10), nullable=False),
        sa.Column("long_probability", sa.Float, nullable=False),
        sa.Column("short_probability", sa.Float, nullable=False),
        sa.Column("no_trade_probability", sa.Float, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("market_regime", sa.String(20), nullable=False),
        sa.Column("features_used", sa.JSON),
        sa.Column("model_version", sa.String(20)),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "signals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("signal_id", sa.String(30), nullable=False, unique=True),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False, server_default="BTC/USDT"),
        sa.Column("signal_type", sa.String(10), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("entry_price", sa.Float),
        sa.Column("stop_loss", sa.Float),
        sa.Column("take_profit_1", sa.Float),
        sa.Column("take_profit_2", sa.Float),
        sa.Column("take_profit_3", sa.Float),
        sa.Column("risk_reward", sa.Float),
        sa.Column("market_regime", sa.String(20)),
        sa.Column("reasoning", sa.Text),
        sa.Column("expiry", sa.DateTime),
        sa.Column("is_active", sa.Boolean, default=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "trades",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("trade_id", sa.String(30), nullable=False, unique=True),
        sa.Column("signal_id", sa.String(30)),
        sa.Column("symbol", sa.String(20), nullable=False, server_default="BTC/USDT"),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="OPEN"),
        sa.Column("mode", sa.String(10), nullable=False, server_default="paper"),
        sa.Column("entry_price", sa.Float, nullable=False),
        sa.Column("exit_price", sa.Float),
        sa.Column("stop_loss", sa.Float),
        sa.Column("take_profit_1", sa.Float),
        sa.Column("take_profit_2", sa.Float),
        sa.Column("take_profit_3", sa.Float),
        sa.Column("quantity", sa.Float, nullable=False),
        sa.Column("leverage", sa.Integer, default=1),
        sa.Column("pnl", sa.Float, default=0),
        sa.Column("pnl_pct", sa.Float, default=0),
        sa.Column("fees", sa.Float, default=0),
        sa.Column("funding", sa.Float, default=0),
        sa.Column("r_multiple", sa.Float, default=0),
        sa.Column("entry_time", sa.DateTime, nullable=False),
        sa.Column("exit_time", sa.DateTime),
        sa.Column("exit_reason", sa.String(50)),
        sa.Column("market_regime", sa.String(20)),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "performance_metrics",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("date", sa.DateTime, nullable=False),
        sa.Column("total_pnl", sa.Float, default=0),
        sa.Column("total_trades", sa.Integer, default=0),
        sa.Column("winning_trades", sa.Integer, default=0),
        sa.Column("losing_trades", sa.Integer, default=0),
        sa.Column("win_rate", sa.Float, default=0),
        sa.Column("profit_factor", sa.Float, default=0),
        sa.Column("max_drawdown", sa.Float, default=0),
        sa.Column("sharpe_ratio", sa.Float, default=0),
        sa.Column("sortino_ratio", sa.Float, default=0),
        sa.Column("expectancy", sa.Float, default=0),
        sa.Column("average_r", sa.Float, default=0),
        sa.Column("equity", sa.Float, default=10000),
        sa.Column("mode", sa.String(10), default="paper"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "bot_state",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(50), nullable=False, unique=True),
        sa.Column("value", sa.JSON, nullable=False),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "notification_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("signal_id", sa.String(30)),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("message_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("sent_at", sa.DateTime, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("notification_logs")
    op.drop_table("bot_state")
    op.drop_table("performance_metrics")
    op.drop_table("trades")
    op.drop_table("signals")
    op.drop_table("predictions")
    op.drop_index("ix_features_symbol_timeframe_timestamp", "features")
    op.drop_table("features")
    op.drop_index("ix_candles_symbol_timeframe_timestamp", "candles")
    op.drop_table("candles")

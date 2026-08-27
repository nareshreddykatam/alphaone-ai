"""phase 2: derivatives data tables, candle quality columns, backtest run tracking

Revision ID: 002
Revises: 001
Create Date: 2026-08-26
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

from database.schema.types import GUID as UUID

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("candles", sa.Column("source", sa.String(20), nullable=False, server_default="binance"))
    op.add_column("candles", sa.Column("ingested_at", sa.DateTime, server_default=sa.func.now()))
    op.add_column("candles", sa.Column("quality_status", sa.String(10), nullable=False, server_default="valid"))
    op.add_column("candles", sa.Column("quality_reason", sa.Text, nullable=True))

    op.add_column("features", sa.Column("feature_version", sa.String(20), nullable=True))
    op.add_column("predictions", sa.Column("feature_version", sa.String(20), nullable=True))

    op.create_table(
        "funding_rates",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("symbol", sa.String(20), nullable=False, server_default="BTC/USDT"),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("rate", sa.Float, nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="binance"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_funding_rates_symbol_timestamp", "funding_rates", ["symbol", "timestamp"], unique=True)

    op.create_table(
        "open_interest",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("symbol", sa.String(20), nullable=False, server_default="BTC/USDT"),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="binance"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_open_interest_symbol_timestamp", "open_interest", ["symbol", "timestamp"], unique=True)

    op.create_table(
        "liquidations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("symbol", sa.String(20), nullable=False, server_default="BTC/USDT"),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("price", sa.Float, nullable=False),
        sa.Column("quantity", sa.Float, nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="binance"),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_liquidations_symbol_timestamp", "liquidations", ["symbol", "timestamp"])

    op.create_table(
        "backtest_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("strategy_name", sa.String(50), nullable=False),
        sa.Column("strategy_version", sa.String(20), nullable=False, server_default="v1"),
        sa.Column("symbol", sa.String(20), nullable=False, server_default="BTC/USDT"),
        sa.Column("timeframe", sa.String(10), nullable=False),
        sa.Column("config_json", sa.JSON, nullable=False),
        sa.Column("dataset_start", sa.DateTime, nullable=False),
        sa.Column("dataset_end", sa.DateTime, nullable=False),
        sa.Column("dataset_version", sa.String(64), nullable=True),
        sa.Column("code_version", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "backtest_metrics",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("backtest_runs.id"), nullable=False),
        sa.Column("total_pnl", sa.Float, default=0),
        sa.Column("total_pnl_pct", sa.Float, default=0),
        sa.Column("total_trades", sa.Integer, default=0),
        sa.Column("winning_trades", sa.Integer, default=0),
        sa.Column("losing_trades", sa.Integer, default=0),
        sa.Column("win_rate", sa.Float, default=0),
        sa.Column("profit_factor", sa.Float, default=0),
        sa.Column("expectancy", sa.Float, default=0),
        sa.Column("average_r", sa.Float, default=0),
        sa.Column("sharpe_ratio", sa.Float, default=0),
        sa.Column("sortino_ratio", sa.Float, default=0),
        sa.Column("max_drawdown", sa.Float, default=0),
        sa.Column("max_drawdown_pct", sa.Float, default=0),
        sa.Column("recovery_factor", sa.Float, default=0),
        sa.Column("average_trade_pnl", sa.Float, default=0),
        sa.Column("average_winning_trade", sa.Float, default=0),
        sa.Column("average_losing_trade", sa.Float, default=0),
        sa.Column("largest_win", sa.Float, default=0),
        sa.Column("largest_loss", sa.Float, default=0),
        sa.Column("consecutive_wins", sa.Integer, default=0),
        sa.Column("consecutive_losses", sa.Integer, default=0),
        sa.Column("total_fees", sa.Float, default=0),
        sa.Column("total_funding", sa.Float, default=0),
        sa.Column("initial_capital", sa.Float, default=0),
        sa.Column("final_capital", sa.Float, default=0),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_backtest_metrics_run_id", "backtest_metrics", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_backtest_metrics_run_id", "backtest_metrics")
    op.drop_table("backtest_metrics")
    op.drop_table("backtest_runs")
    op.drop_index("ix_liquidations_symbol_timestamp", "liquidations")
    op.drop_table("liquidations")
    op.drop_index("ix_open_interest_symbol_timestamp", "open_interest")
    op.drop_table("open_interest")
    op.drop_index("ix_funding_rates_symbol_timestamp", "funding_rates")
    op.drop_table("funding_rates")

    op.drop_column("predictions", "feature_version")
    op.drop_column("features", "feature_version")

    op.drop_column("candles", "quality_reason")
    op.drop_column("candles", "quality_status")
    op.drop_column("candles", "ingested_at")
    op.drop_column("candles", "source")

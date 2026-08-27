"""phase 5: CoinDCX live sync fields (positions, wallet snapshots, idempotent
trade/execution matching), replacing SunCrypto as the active exchange.

Revision ID: 004
Revises: 003
Create Date: 2026-08-26
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("exchange_position_id", sa.String(64), nullable=True))
    op.add_column("trades", sa.Column("exchange_trade_id", sa.String(64), nullable=True))
    op.add_column("trades", sa.Column("mark_price", sa.Float, nullable=True))
    op.add_column("trades", sa.Column("liquidation_price", sa.Float, nullable=True))
    op.add_column("trades", sa.Column("unrealized_pnl", sa.Float, nullable=True))
    op.add_column("trades", sa.Column("margin", sa.Float, nullable=True))
    op.add_column("trades", sa.Column("data_source", sa.String(20), nullable=False, server_default="MANUAL"))
    op.add_column("trades", sa.Column("match_status", sa.String(20), nullable=False, server_default="MANUAL"))
    op.add_column("trades", sa.Column("last_synced_at", sa.DateTime, nullable=True))
    op.create_index(
        "ix_trades_exchange_trade_id", "trades", ["exchange_trade_id"], unique=True,
        sqlite_where=sa.text("exchange_trade_id IS NOT NULL"),
        postgresql_where=sa.text("exchange_trade_id IS NOT NULL"),
    )

    op.add_column("trade_executions", sa.Column("exchange_transaction_id", sa.String(128), nullable=True))
    op.create_index(
        "ix_trade_executions_exchange_transaction_id", "trade_executions", ["exchange_transaction_id"],
        unique=True,
        sqlite_where=sa.text("exchange_transaction_id IS NOT NULL"),
        postgresql_where=sa.text("exchange_transaction_id IS NOT NULL"),
    )

    op.add_column("accounts", sa.Column("last_synced_at", sa.DateTime, nullable=True))

    op.add_column("account_snapshots", sa.Column("available_balance", sa.Float, nullable=True))
    op.add_column("account_snapshots", sa.Column("used_margin", sa.Float, nullable=True))
    op.add_column("account_snapshots", sa.Column("unrealized_pnl", sa.Float, nullable=True))
    op.add_column("account_snapshots", sa.Column("realized_pnl", sa.Float, nullable=True))

    # Replace SunCrypto as the active exchange (Phase 5, section 7) -- the
    # Phase 4 default account was never actually connected to a live
    # SunCrypto account (no such API existed), so this is safe: it updates
    # the placeholder exchange label, not real historical trading data.
    op.execute("UPDATE accounts SET exchange = 'coindcx' WHERE exchange = 'suncrypto'")


def downgrade() -> None:
    op.execute("UPDATE accounts SET exchange = 'suncrypto' WHERE exchange = 'coindcx'")

    op.drop_column("account_snapshots", "realized_pnl")
    op.drop_column("account_snapshots", "unrealized_pnl")
    op.drop_column("account_snapshots", "used_margin")
    op.drop_column("account_snapshots", "available_balance")

    op.drop_column("accounts", "last_synced_at")

    op.drop_index("ix_trade_executions_exchange_transaction_id", "trade_executions")
    op.drop_column("trade_executions", "exchange_transaction_id")

    op.drop_index("ix_trades_exchange_trade_id", "trades")
    op.drop_column("trades", "last_synced_at")
    op.drop_column("trades", "match_status")
    op.drop_column("trades", "data_source")
    op.drop_column("trades", "margin")
    op.drop_column("trades", "unrealized_pnl")
    op.drop_column("trades", "liquidation_price")
    op.drop_column("trades", "mark_price")
    op.drop_column("trades", "exchange_trade_id")
    op.drop_column("trades", "exchange_position_id")

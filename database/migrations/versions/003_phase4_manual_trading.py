"""phase 4: accounts, manual trade tracking, signal outcomes, deposits/withdrawals, sync audit

Revision ID: 003
Revises: 002
Create Date: 2026-08-26
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

from database.schema.types import GUID as UUID

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("signals", sa.Column("quality", sa.String(10), nullable=True))
    op.add_column("signals", sa.Column("strategy_name", sa.String(50), nullable=True))
    op.add_column("signals", sa.Column("model_version", sa.String(20), nullable=True))

    op.add_column("trades", sa.Column("is_manual_entry", sa.Boolean, nullable=False, server_default=sa.true()))
    op.add_column("trades", sa.Column("source", sa.String(20), nullable=False, server_default="MANUAL"))
    op.add_column("trades", sa.Column("matched_signal_confidence", sa.Float, nullable=True))
    op.add_column("trades", sa.Column("account_id", UUID(as_uuid=True), nullable=True))

    op.create_table(
        "accounts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("exchange", sa.String(20), nullable=False, server_default="suncrypto"),
        sa.Column("mode", sa.String(20), nullable=False, server_default="paper"),
        sa.Column("connection_status", sa.String(20), nullable=False, server_default="NOT_CONNECTED"),
        sa.Column("base_currency", sa.String(10), nullable=False, server_default="USDT"),
        sa.Column("label", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )

    with op.batch_alter_table("trades") as batch_op:
        batch_op.create_foreign_key(
            "fk_trades_account_id", "accounts", ["account_id"], ["id"]
        )

    op.create_table(
        "account_snapshots",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", UUID(as_uuid=True), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("equity", sa.Float, nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="MANUAL"),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_account_snapshots_account_id_timestamp", "account_snapshots", ["account_id", "timestamp"]
    )

    op.create_table(
        "deposits",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", UUID(as_uuid=True), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("amount", sa.Float, nullable=False),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "withdrawals",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", UUID(as_uuid=True), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("amount", sa.Float, nullable=False),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "trade_executions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("trade_id", sa.String(30), sa.ForeignKey("trades.trade_id"), nullable=False),
        sa.Column("execution_type", sa.String(20), nullable=False),
        sa.Column("price", sa.Float, nullable=False),
        sa.Column("quantity", sa.Float, nullable=False),
        sa.Column("timestamp", sa.DateTime, nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_trade_executions_trade_id", "trade_executions", ["trade_id"])

    op.create_table(
        "signal_outcomes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("signal_id", sa.String(30), sa.ForeignKey("signals.signal_id"), nullable=False, unique=True),
        sa.Column("outcome", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("hypothetical_entry_price", sa.Float, nullable=True),
        sa.Column("hypothetical_exit_price", sa.Float, nullable=True),
        sa.Column("hypothetical_pnl", sa.Float, nullable=True),
        sa.Column("hypothetical_pnl_pct", sa.Float, nullable=True),
        sa.Column("hypothetical_r_multiple", sa.Float, nullable=True),
        sa.Column("was_taken_by_user", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("evaluated_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_signal_outcomes_signal_id", "signal_outcomes", ["signal_id"])

    op.create_table(
        "sync_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("detail", sa.Text, nullable=True),
        sa.Column("timestamp", sa.DateTime, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("sync_events")
    op.drop_index("ix_signal_outcomes_signal_id", "signal_outcomes")
    op.drop_table("signal_outcomes")
    op.drop_index("ix_trade_executions_trade_id", "trade_executions")
    op.drop_table("trade_executions")
    op.drop_table("withdrawals")
    op.drop_table("deposits")
    op.drop_index("ix_account_snapshots_account_id_timestamp", "account_snapshots")
    op.drop_table("account_snapshots")
    with op.batch_alter_table("trades") as batch_op:
        batch_op.drop_constraint("fk_trades_account_id", type_="foreignkey")
    op.drop_table("accounts")

    op.drop_column("trades", "account_id")
    op.drop_column("trades", "matched_signal_confidence")
    op.drop_column("trades", "source")
    op.drop_column("trades", "is_manual_entry")

    op.drop_column("signals", "model_version")
    op.drop_column("signals", "strategy_name")
    op.drop_column("signals", "quality")

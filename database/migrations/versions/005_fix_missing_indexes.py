"""Production deployment prep: fix schema drift found via `alembic check`.

Prediction.signal_id and Trade.trade_id have each carried an explicit
named Index(...) in their __table_args__ for some time, but neither index
was ever captured in a migration -- local dev never noticed because
apps/api/main.py's lifespan calls Base.metadata.create_all() on every
startup against SQLite, which builds the schema straight from the current
ORM models and bypasses Alembic entirely. A real production deployment
must run `alembic upgrade head` against Postgres instead of relying on
create_all, so this drift would otherwise silently ship two missing
indexes. Purely additive and non-destructive -- no column/data changes.

Revision ID: 005
Revises: 004
Create Date: 2026-08-27
"""
from typing import Sequence, Union
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_predictions_signal_id", "predictions", ["signal_id"])
    op.create_index("ix_trades_trade_id", "trades", ["trade_id"])


def downgrade() -> None:
    op.drop_index("ix_trades_trade_id", "trades")
    op.drop_index("ix_predictions_signal_id", "predictions")

"""Idempotent, additive schema patches for columns added AFTER a table
already existed in production. This project has no Alembic migration
history (alembic.ini is present but unused -- every table so far has been
created fresh via Base.metadata.create_all(), which only creates MISSING
TABLES and never adds a missing COLUMN to a table that already exists).
Introducing a full Alembic migration chain for one column is a bigger,
separate change than this needs; this module follows the project's
existing schema-bootstrap pattern (called once at startup, right after
create_all(), from apps/api/main.py's lifespan) rather than a new one.

Every patch here must be:
  - purely additive (ADD COLUMN, nullable, no data loss)
  - idempotent (safe to run on every startup, forever)
  - checked via a real information-schema/pragma query first, never a bare
    ALTER TABLE wrapped in a swallowed exception (which would hide a real
    failure just as easily as a "column already exists" one)
"""
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection


async def ensure_signal_timeframe_column(conn: AsyncConnection) -> None:
    """Signal.timeframe (database/schema/models.py) was added after the
    `signals` table already existed in production Postgres -- add it if
    missing. A no-op on any fresh/dev DB where create_all() already created
    the table with this column from the start."""
    def _get_columns(sync_conn):
        return [c["name"] for c in inspect(sync_conn).get_columns("signals")]

    columns = await conn.run_sync(_get_columns)
    if "timeframe" not in columns:
        await conn.execute(text("ALTER TABLE signals ADD COLUMN timeframe VARCHAR(10)"))


async def run_schema_migrations(conn: AsyncConnection) -> None:
    """Call once at startup, after Base.metadata.create_all(). Add new
    idempotent patches here as the schema evolves; never remove an old one
    (a fresh deploy skipping several versions must still apply all of
    them)."""
    await ensure_signal_timeframe_column(conn)

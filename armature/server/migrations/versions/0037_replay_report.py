"""A replay experiment's comparison report.

Revision ID: 0037
Revises: 0036
Create Date: 2026-11-17 18:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0037'
down_revision: str | Sequence[str] | None = '0036'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# As in 0035: SQLite rebuilds eval_experiments to drop the column, and other tables reference it.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


# A plain ADD COLUMN, which SQLite runs without rebuilding the table.
def upgrade() -> None:
    op.add_column('eval_experiments', sa.Column('report', sa.JSON(), nullable=True))


def downgrade() -> None:
    _sqlite_foreign_keys(False)
    with op.batch_alter_table('eval_experiments') as batch:
        batch.drop_column('report')
    _sqlite_foreign_keys(True)

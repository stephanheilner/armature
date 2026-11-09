"""GCP connections: a project's sealed service account key, for reading a BigQuery export such as Crashlytics.

Revision ID: 0028
Revises: 0027
Create Date: 2026-11-09 09:00:00

"""
from collections.abc import Sequence

from alembic import op

revision: str = '0028'
down_revision: str | Sequence[str] | None = '0027'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The values armature.server.db.schema allowed at this revision and the one before, frozen here.
KINDS = ('github_app', 'slack', 'jira', 'linear', 'gcp')
OLD_KINDS = ('github_app', 'slack', 'jira', 'linear')


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


# As in 0016: SQLite rebuilds connections, and other tables reference it.
def _sqlite_foreign_keys(enabled: bool) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def _kinds(values: tuple[str, ...]) -> None:
    with op.batch_alter_table('connections') as batch:
        batch.drop_constraint(op.f('ck_connections_kind'), type_='check')
        batch.create_check_constraint(op.f('ck_connections_kind'), _in('kind', values))


def upgrade() -> None:
    _sqlite_foreign_keys(False)
    _kinds(KINDS)
    _sqlite_foreign_keys(True)


def downgrade() -> None:
    """Drop GCP connections, since the old constraint refuses them."""
    # SQLite ignores the pragma once a statement opens the transaction, so it goes before the delete.
    _sqlite_foreign_keys(False)
    op.execute("DELETE FROM connections WHERE kind = 'gcp'")
    _kinds(OLD_KINDS)
    _sqlite_foreign_keys(True)

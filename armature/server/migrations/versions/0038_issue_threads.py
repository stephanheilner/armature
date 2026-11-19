"""Issue threads: a Jira or Linear run, and every run it handed off to, joins its issue's thread.

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-19 12:00:00

"""
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0038'
down_revision: str | Sequence[str] | None = '0037'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The rules armature.server.pollers and threads applied at this revision, frozen here.
ISSUE = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")
THREAD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#@-]{0,190}$")
ISSUE_KINDS = ("jira", "linear")
HANDOFF = "handoff"
CHUNK = 500

runs = sa.table('runs', sa.column('id', sa.String), sa.column('thread', sa.String),
                sa.column('trigger_kind', sa.String), sa.column('trigger_ref', sa.JSON))
jobs = sa.table('jobs', sa.column('id', sa.String), sa.column('run_id', sa.String), sa.column('thread', sa.String))


def _ref(value, name: str) -> str | None:
    found = value.get(name) if isinstance(value, dict) else None
    return found if isinstance(found, str) and found else None


def _issue(ref) -> str | None:
    issue = _ref(ref, "issue")
    return issue if issue and ISSUE.fullmatch(issue) and THREAD.fullmatch(issue) else None


def _backfill(conn: sa.Connection) -> None:
    rows = conn.execute(sa.select(runs.c.id, runs.c.trigger_kind, runs.c.trigger_ref)
                        .where(runs.c.thread.is_(None), runs.c.trigger_kind.in_((*ISSUE_KINDS, HANDOFF)))).all()
    threaded = {row.id: _issue(row.trigger_ref) for row in rows if row.trigger_kind in ISSUE_KINDS}
    threaded = {run_id: thread for run_id, thread in threaded.items() if thread}
    parent_of = {row.id: _ref(row.trigger_ref, "run_id") for row in rows if row.trigger_kind == HANDOFF}
    parent_of = {run_id: parent for run_id, parent in parent_of.items() if parent}
    parents = sorted(set(parent_of.values()))
    known: dict[str, str] = {}
    for start in range(0, len(parents), CHUNK):
        known.update(conn.execute(sa.select(runs.c.id, runs.c.thread).where(
            runs.c.id.in_(parents[start:start + CHUNK]), runs.c.thread.is_not(None))).all())
    # A handoff's parent may itself be a handoff this backfill threads, so it passes again until nothing changes.
    changed = True
    while changed:
        changed = False
        for run_id, parent in parent_of.items():
            thread = threaded.get(parent) or known.get(parent)
            if run_id not in threaded and thread:
                threaded[run_id] = thread
                changed = True
    by_thread: dict[str, list[str]] = {}
    for run_id, thread in threaded.items():
        by_thread.setdefault(thread, []).append(run_id)
    for thread, run_ids in by_thread.items():
        for start in range(0, len(run_ids), CHUNK):
            chunk = run_ids[start:start + CHUNK]
            conn.execute(sa.update(runs).where(runs.c.id.in_(chunk), runs.c.thread.is_(None)).values(thread=thread))
            conn.execute(sa.update(jobs).where(jobs.c.run_id.in_(chunk), jobs.c.thread.is_(None)).values(thread=thread))


def upgrade() -> None:
    _backfill(op.get_bind())


# The threads stay: they are data the next revision down reads the same way.
def downgrade() -> None:
    pass

"""Definition columns added by migration 0003, applied to the tables in ``schema``.

Instance-level workflows and settings carry ``team_id`` NULL and scope ``*``,
as documents and secrets already do. Teams gain the slug armature-ops uses,
and projects the repository they review.
"""
from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)

from armature.server.db.schema import (
    SCOPE_MATCHES_TEAM,
    projects,
    scope_of,
    settings,
    teams,
    workflows,
)

SLUG_LENGTH = 40


def _scope_from_team(context) -> str:
    return scope_of(context.get_current_parameters().get("team_id"))


teams.append_column(Column("slug", String(SLUG_LENGTH), unique=True))
projects.append_column(Column("repo", String(191)))

workflows.c.team_id.nullable = True
workflows.append_column(Column("scope", String(36), nullable=False, default=_scope_from_team))
for _constraint in [c for c in workflows.constraints
                    if isinstance(c, UniqueConstraint) and [col.name for col in c.columns] == ["team_id", "name"]]:
    workflows.constraints.remove(_constraint)
workflows.append_constraint(UniqueConstraint("scope", "name"))
workflows.append_constraint(CheckConstraint(SCOPE_MATCHES_TEAM, name="scope"))
# MySQL needs an index for the team foreign key once the unique key that led with team_id is gone.
Index(None, workflows.c.team_id)

settings.append_column(Column("team_id", String(36), ForeignKey("teams.id")))
settings.append_column(Column("scope", String(36), nullable=False, default=_scope_from_team, server_default="*"))
settings.append_constraint(CheckConstraint(SCOPE_MATCHES_TEAM, name="scope"))

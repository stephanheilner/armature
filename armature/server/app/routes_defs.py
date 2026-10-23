"""Read-only workflows, documents, providers, and settings.

A team's definitions show to anyone who can see the team, and instance-level
ones to anyone signed in. Settings are admin-only.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import and_, func, or_, select, true

from armature.server import rbac
from armature.server.app.deps import Granted, by_team, require, server, signed_in
from armature.server.app.http import ApiError
from armature.server.auth import Session
from armature.server.db.schema import (
    DOCUMENT_KINDS,
    INSTANCE_SCOPE,
    documents,
    providers,
    secrets,
    settings,
    users,
    versions,
    workflows,
)
from armature.server.import_ops import lint_ok

router = APIRouter()

INSTANCE_NAMES = ("instance", INSTANCE_SCOPE)


def _access(request: Request, session: Session) -> rbac.Access:
    with server(request).engine.connect() as conn:
        return rbac.load_access(conn, session.user_id, bool(session.user.is_admin))


def _visible(access: rbac.Access, team_col):
    if access.is_admin:
        return true()
    return or_(team_col.is_(None), team_col.in_(rbac.visible_scope(access).visible_team_ids))


def _current_version(table, object_kind: str):
    return and_(versions.c.object_kind == object_kind, versions.c.object_id == table.c.id,
                versions.c.version == table.c.current_version)


def _history(conn, object_kind: str, object_id: str, current: int) -> tuple[dict[str, Any] | None, list]:
    rows = conn.execute(
        select(versions.c.version, versions.c.body, versions.c.note, versions.c.created_at,
               func.coalesce(users.c.username, versions.c.author_id).label("author"))
        .select_from(versions.outerjoin(users, users.c.id == versions.c.author_id))
        .where(versions.c.object_kind == object_kind, versions.c.object_id == object_id)
        .order_by(versions.c.version.desc())).all()
    listed = [{"version": r.version, "note": r.note, "author": r.author, "created_at": r.created_at} for r in rows]
    shown = next(({**item, "body": r.body} for item, r in zip(listed, rows, strict=True) if r.version == current),
                 None)
    return shown, listed


def _workflow(row: Any) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "team_id": row.team_id, "scope": row.scope,
            "current_version": row.current_version, "updated_at": row.updated_at, "lint_ok": lint_ok(row.note)}


def _document(row: Any) -> dict[str, Any]:
    return {"id": row.id, "kind": row.kind, "key": row.key, "team_id": row.team_id, "scope": row.scope,
            "current_version": row.current_version, "updated_at": row.updated_at}


@router.get("/api/workflows")
def list_workflows(request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    access = _access(request, session)
    query = (select(workflows, versions.c.note)
             .select_from(workflows.outerjoin(versions, _current_version(workflows, "workflow")))
             .where(_visible(access, workflows.c.team_id)).order_by(workflows.c.name, workflows.c.scope))
    with server(request).engine.connect() as conn:
        rows = conn.execute(query).all()
    return {"workflows": [_workflow(r) for r in rows]}


@router.get("/api/workflows/{workflow_id}")
def get_workflow(workflow_id: str, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    access = _access(request, session)
    with server(request).engine.connect() as conn:
        row = conn.execute(select(workflows, versions.c.note)
                           .select_from(workflows.outerjoin(versions, _current_version(workflows, "workflow")))
                           .where(workflows.c.id == workflow_id, _visible(access, workflows.c.team_id))).first()
        if row is None:
            raise ApiError(404, "Not found.")
        version, history = _history(conn, "workflow", row.id, row.current_version)
    return {"workflow": _workflow(row), "version": version, "versions": history}


@router.get("/api/documents")
def list_documents(request: Request, kind: str | None = Query(None),
                   session: Session = Depends(signed_in)) -> dict[str, Any]:
    if kind is not None and kind not in DOCUMENT_KINDS:
        raise ApiError(400, f"kind must be one of {', '.join(DOCUMENT_KINDS)}.")
    access = _access(request, session)
    query = select(documents).where(_visible(access, documents.c.team_id))
    if kind:
        query = query.where(documents.c.kind == kind)
    with server(request).engine.connect() as conn:
        rows = conn.execute(query.order_by(documents.c.kind, documents.c.key, documents.c.scope)).all()
    return {"documents": [_document(r) for r in rows]}


@router.get("/api/documents/{document_id}")
def get_document(document_id: str, request: Request, session: Session = Depends(signed_in)) -> dict[str, Any]:
    access = _access(request, session)
    with server(request).engine.connect() as conn:
        row = conn.execute(select(documents).where(documents.c.id == document_id,
                                                   _visible(access, documents.c.team_id))).first()
        if row is None:
            raise ApiError(404, "Not found.")
        version, history = _history(conn, "document", row.id, row.current_version)
    return {"document": _document(row), "version": version, "versions": history}


@router.get("/api/teams/{team_id}/providers")
def list_providers(request: Request, granted: Granted = Depends(require("viewer", by_team()))) -> dict[str, Any]:
    team_id = granted.target.team_id
    with server(request).engine.connect() as conn:
        rows = conn.execute(select(providers).where(providers.c.team_id == team_id)
                            .order_by(providers.c.kind, providers.c.created_at)).all()
        names = {r.config.get("credential_secret") for r in rows if isinstance(r.config, dict)} - {None}
        present = set(conn.execute(select(secrets.c.name).where(
            secrets.c.name.in_(names), secrets.c.scope.in_([team_id, INSTANCE_SCOPE]))).scalars()) if names else set()
    listed = []
    for r in rows:
        config = r.config if isinstance(r.config, dict) else {}
        needed = config.get("credential_secret")
        listed.append({"id": r.id, "kind": r.kind, "config": config, "aliases": r.aliases or {},
                       "configured": needed is None or needed in present})
    return {"providers": listed}


@router.get("/api/settings")
def list_settings(request: Request, scope: str | None = Query(None, max_length=36),
                  granted: Granted = Depends(require("admin"))) -> dict[str, Any]:
    query = select(settings.c.key, settings.c.value, settings.c.scope).order_by(settings.c.key)
    if scope:
        query = query.where(settings.c.scope == (INSTANCE_SCOPE if scope in INSTANCE_NAMES else scope))
    with server(request).engine.connect() as conn:
        rows = conn.execute(query).all()
    return {"settings": [{"key": r.key, "value": r.value, "scope": r.scope} for r in rows]}

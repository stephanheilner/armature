"""Runner keys, run registration, and the per-run event tokens runs stream with.

A runner key is a team's machine credential, ``ark_<random>``, shown once and
stored only as an HMAC. Registering a run with it mints a per-run event token,
also stored only as an HMAC, which expires after 48 hours or when the run ends.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets as _random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Connection, delete, insert, or_, select, update

from armature.server import audit
from armature.server.auth import valid_token
from armature.server.crypto import Keyring
from armature.server.db import new_id
from armature.server.db.schema import (
    INSTANCE_SCOPE,
    projects,
    run_tokens,
    runner_keys,
    runs,
    workflows,
)

KEY_PREFIX = "ark_"
TOKEN_TTL = timedelta(hours=48)
TOUCH = timedelta(seconds=60)
WORKFLOW = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]{0,190}$")
TRIGGER_KIND = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")
TRIGGER_REF_MAX = 4096
ACTOR_MAX = 191


class RegistrationError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class RunToken:
    run_id: str
    team_id: str
    expires_at: datetime


def digest(keyring: Keyring, secret: str) -> str:
    return hmac.new(keyring.subkey("runners"), secret.encode("ascii", "replace"), hashlib.sha256).hexdigest()


def public_key(row: Any) -> dict[str, Any]:
    return {"id": row.id, "name": row.name, "created_at": row.created_at, "created_by": row.created_by,
            "last_used_at": row.last_used_at}


def list_keys(conn: Connection, team_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(select(runner_keys).where(runner_keys.c.team_id == team_id,
                                                  runner_keys.c.revoked_at.is_(None))
                        .order_by(runner_keys.c.name, runner_keys.c.id)).all()
    return [public_key(r) for r in rows]


def create_key(conn: Connection, keyring: Keyring, *, team_id: str, name: str, actor_id: str, now: datetime,
               ip: str | None) -> tuple[dict[str, Any], str]:
    key = KEY_PREFIX + _random.token_urlsafe(32)
    key_id = new_id()
    conn.execute(insert(runner_keys).values(id=key_id, team_id=team_id, name=name, key_hmac=digest(keyring, key),
                                            created_by=actor_id, created_at=now))
    audit.record(conn, action="runner_key.create", actor_id=actor_id, team_id=team_id, target_kind="runner_key",
                 target_id=key_id, detail={"name": name}, ip=ip)
    row = conn.execute(select(runner_keys).where(runner_keys.c.id == key_id)).one()
    return public_key(row), key


def revoke_key(conn: Connection, *, team_id: str, key_id: str, actor_id: str, now: datetime,
               ip: str | None) -> bool:
    result = conn.execute(update(runner_keys).where(runner_keys.c.id == key_id, runner_keys.c.team_id == team_id,
                                                    runner_keys.c.revoked_at.is_(None)).values(revoked_at=now))
    if result.rowcount != 1:
        return False
    conn.execute(delete(run_tokens).where(run_tokens.c.run_id.in_(
        select(runs.c.id).where(runs.c.runner_key_id == key_id).scalar_subquery())))
    audit.record(conn, action="runner_key.delete", actor_id=actor_id, team_id=team_id, target_kind="runner_key",
                 target_id=key_id, ip=ip)
    return True


def authenticate(conn: Connection, keyring: Keyring, key: str | None, now: datetime) -> Any | None:
    if not key or not key.startswith(KEY_PREFIX) or not valid_token(key[len(KEY_PREFIX):]):
        return None
    row = conn.execute(select(runner_keys).where(runner_keys.c.key_hmac == digest(keyring, key))).first()
    if row is None or row.revoked_at is not None:
        return None
    if row.last_used_at is None or now - row.last_used_at > TOUCH:
        conn.execute(update(runner_keys).where(runner_keys.c.id == row.id).values(last_used_at=now))
    return row


def _text(value: Any, what: str, limit: int, pattern: re.Pattern[str] | None = None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RegistrationError(400, f"{what} must be a string.")
    value = "".join(ch for ch in value.strip() if ch.isprintable())[:limit]
    if pattern is not None and not pattern.fullmatch(value):
        raise RegistrationError(400, f"{what} is not valid.")
    return value or None


def register_run(conn: Connection, keyring: Keyring, *, key: Any, body: dict[str, Any], now: datetime,
                 ip: str | None) -> tuple[dict[str, Any], str]:
    """Create a running run for the key's team and mint its event token. Returns the run row and the token."""
    team_id = key.team_id
    workflow = _text(body.get("workflow"), "workflow", 191, WORKFLOW)
    if workflow is None:
        raise RegistrationError(400, "workflow is required.")
    trigger_kind = _text(body.get("trigger_kind"), "trigger_kind", 32, TRIGGER_KIND)
    actor = _text(body.get("actor"), "actor", ACTOR_MAX)
    trigger_ref = body.get("trigger_ref")
    if trigger_ref is not None and (not isinstance(trigger_ref, dict)
                                    or len(json.dumps(trigger_ref, default=str)) > TRIGGER_REF_MAX):
        raise RegistrationError(400, f"trigger_ref must be a JSON object of at most {TRIGGER_REF_MAX} bytes.")
    project_id = None
    project = _text(body.get("project"), "project", 191)
    if project is not None:
        project_id = conn.execute(select(projects.c.id).where(
            projects.c.team_id == team_id, or_(projects.c.name == project, projects.c.repo == project))
            .order_by(projects.c.name != project).limit(1)).scalar()
        if project_id is None:
            raise RegistrationError(404, "No such project in this runner key's team.")
    rerun_of = body.get("rerun_of")
    if rerun_of is not None and (not isinstance(rerun_of, str) or conn.execute(
            select(runs.c.id).where(runs.c.id == rerun_of, runs.c.team_id == team_id)).first() is None):
        raise RegistrationError(404, "No such run to re-run in this runner key's team.")
    # The team's own workflow of that name wins over an instance one.
    workflow_id = conn.execute(select(workflows.c.id).where(
        workflows.c.name == workflow, workflows.c.scope.in_([team_id, INSTANCE_SCOPE]))
        .order_by(workflows.c.scope == INSTANCE_SCOPE).limit(1)).scalar()
    run_id = new_id()
    row = {"id": run_id, "team_id": team_id, "project_id": project_id, "workflow_id": workflow_id,
           "workflow_name": workflow, "status": "running", "actor": actor, "trigger_kind": trigger_kind,
           "trigger_ref": trigger_ref, "rerun_of": rerun_of, "runner_key_id": key.id, "started_at": now,
           "cost_usd": 0, "input_tokens": 0, "output_tokens": 0}
    conn.execute(insert(runs).values(**row))
    token = _random.token_urlsafe(32)
    conn.execute(insert(run_tokens).values(token_hmac=digest(keyring, token), run_id=run_id, team_id=team_id,
                                           expires_at=now + TOKEN_TTL))
    audit.record(conn, action="run.register", actor_id=None, team_id=team_id, target_kind="run", target_id=run_id,
                 detail={"runner_key_id": key.id, "workflow": workflow, "actor": actor}, ip=ip)
    return row, token


def lookup_token(conn: Connection, token_digest: str) -> RunToken | None:
    row = conn.execute(select(run_tokens).where(run_tokens.c.token_hmac == token_digest)).first()
    return RunToken(run_id=row.run_id, team_id=row.team_id, expires_at=row.expires_at) if row else None


def revoke_tokens(conn: Connection, run_ids: list[str]) -> None:
    if run_ids:
        conn.execute(delete(run_tokens).where(run_tokens.c.run_id.in_(run_ids)))

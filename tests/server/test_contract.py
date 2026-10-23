"""The response fields the browser code reads, pinned against what the routes return."""
import re
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import insert

from armature.server import secrets as secret_store
from armature.server.db import new_id
from armature.server.db.schema import runs
from tests.server.app_support import (
    ORIGIN,
    Clock,
    add_user,
    make_client,
    mutation,
    sign_in_as,
)
from tests.server.test_rbac import _side

STATIC = Path(__file__).resolve().parents[2] / "armature" / "server" / "static"

# (file, variable the file reads the response into, route the response comes from, fields read)
READS = [
    ("invite.js", "invite", "inspect", {"username", "otpauth_uri"}),
    ("views/users.js", "created", "create_user", {"invite_url"}),
    ("views/users.js", "issued", "reissue", {"invite_url"}),
    ("views/users.js", "account", "user", {"id", "username", "display_name", "email", "is_admin", "disabled", "sso",
                                           "totp_confirmed", "memberships"}),
    ("views/users.js", "membership", "membership", {"id", "scope_kind", "scope_id", "scope_name", "role"}),
    ("views/users.js", "match", "lookup", {"id", "username"}),
    ("session.js", "current", "session", {"user", "memberships", "sso"}),
    ("login.js", "sessionPayload", "session", {"user", "csrf", "auth"}),
    ("views/runs.js", "run", "run", {"cost_usd", "finished_at", "project_id", "started_at", "status", "team_id",
                                     "triggered_by", "workflow"}),
    ("views/runs.js", "page", "runs", {"next_before"}),
    ("views/secrets.js", "secret", "secret", {"name", "set", "updated_at", "updated_by"}),
    ("views/teams.js", "projectRecord", "project", {"name", "platform", "team_id", "id"}),
    ("views/teams.js", "project", "project", {"name", "platform", "id"}),
    ("views/teams.js", "team", "team", {"id", "name"}),
]


def _read_fields(source: str, variable: str) -> set[str]:
    return set(re.findall(rf"\b{variable}\??\.([a-z_]+)\b", source))


@pytest.fixture
def responses(sqlite_engine, keyring):
    clock = Clock()
    with sqlite_engine.begin() as conn:
        side = _side(conn, "own", clock)
        secret_store.set_secret(conn, keyring, team_id=side.team, name="K", value="v", actor_id=None)
        conn.execute(insert(runs).values(id=new_id(), team_id=side.team, status="running", started_at=clock(),
                                         cost_usd=Decimal(1)))
    admin_id, _ = add_user(sqlite_engine, keyring, clock, "root", is_admin=True)
    client = make_client(sqlite_engine, keyring, clock)
    csrf = sign_in_as(client, sqlite_engine, keyring, clock, admin_id)
    created = client.post("/api/users", json={"username": "bea", "team_id": side.team, "role": "viewer"},
                          headers=mutation(csrf)).json()
    token = created["invite_url"].split("#", 1)[1]
    guest = make_client(sqlite_engine, keyring, clock)
    guest_csrf = guest.get("/api/session").json()["csrf"]
    users = client.get("/api/users").json()["users"]
    return {
        "inspect": guest.post("/api/invite/inspect", json={"token": token},
                              headers={"Origin": ORIGIN, "X-CSRF-Token": guest_csrf}).json(),
        "create_user": created,
        "reissue": client.post(f"/api/users/{created['user']['id']}/invite", json={}, headers=mutation(csrf)).json(),
        "user": users[0],
        "membership": next(u for u in users if u["memberships"])["memberships"][0],
        "lookup": client.get("/api/users/lookup", params={"q": "bea"}).json()["users"][0],
        "session": client.get("/api/session").json(),
        "run": client.get("/api/runs").json()["runs"][0],
        "runs": client.get("/api/runs").json(),
        "secret": client.get(f"/api/teams/{side.team}/secrets").json()["secrets"][0],
        "project": client.get(f"/api/projects/{side.project}").json()["project"],
        "team": client.get("/api/teams").json()["teams"][0],
    }


@pytest.mark.parametrize("path,variable,response,fields", READS, ids=[f"{r[0]}:{r[1]}" for r in READS])
def test_browser_reads_fields_the_route_returns(responses, path, variable, response, fields):
    read = _read_fields((STATIC / path).read_text(), variable)
    assert fields <= read, f"{path} no longer reads {sorted(fields - read)} from {variable}; update READS"
    returned = set(responses[response])
    assert read <= returned | {"length"}, f"{path} reads {sorted(read - returned)} from {variable}, not returned"

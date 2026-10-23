import base64
import hashlib
import hmac
import os
import shutil
from pathlib import Path

import pyotp
import pytest
import yaml
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select, text
from typer.testing import CliRunner

from armature.cli import app
from armature.server import secrets as secret_store
from armature.server.app import routes_defs
from armature.server.db import make_engine, migrate, new_id, utcnow
from armature.server.db.schema import (
    audit,
    documents,
    memberships,
    projects,
    providers,
    settings,
    teams,
    users,
    versions,
    workflows,
)
from armature.server.import_ops import (
    ACTOR,
    DashboardUser,
    dashboard_totp_key,
    decrypt_dashboard_secret,
    import_ops,
    is_secret_name,
    lint_ok,
    parse_env,
)
from armature.server.passwords import hash_password
from armature.server.users import totp_aad
from tests.server.app_support import (
    COST,
    PASSWORD,
    Clock,
    add_membership,
    add_user,
    login,
    make_client,
    sign_in_as,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ops"
SESSION_KEY = os.urandom(32)
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
FIXTURE_SECRETS = ("fixture-gh-token-value", "fixture webhook secret", "fixture-jira-token", "fixture-bedrock-bearer",
                   "fixture-mrp-slack-token", "fixture-linear-key", "fixture-pem-body-not-a-real-key")
EVERY_TABLE = (teams, projects, workflows, documents, versions, settings, providers, users, memberships)


def _hkdf_sha256(ikm: bytes, info: bytes, length: int = 32) -> bytes:
    """Node's ``crypto.hkdfSync('sha256', ikm, Buffer.alloc(0), info, 32)``, written out from RFC 5869."""
    prk = hmac.new(b"", ikm, hashlib.sha256).digest()
    block, out, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def dashboard_encrypt(session_key: bytes, plaintext: str) -> str:
    """``encryptSecret(userKeys(sessionKey).encryption, plaintext)`` from the dashboard's ``lib/users.js``."""
    key = _hkdf_sha256(session_key, b"armature-dashboard totp encryption v1")
    iv = os.urandom(12)
    sealed = AESGCM(key).encrypt(iv, plaintext.encode(), None)
    return ".".join(["v1", _b64url(iv), _b64url(sealed[:-16]), _b64url(sealed[-16:])])


def _dashboard_users(password_hash: str) -> list[DashboardUser]:
    return [
        DashboardUser(username="stephan", display_name="Stephan", role="admin", disabled=False,
                      password_hash=password_hash, totp_enc=dashboard_encrypt(SESSION_KEY, TOTP_SECRET),
                      totp_pending=False, teams=["mobile"], last_totp_step=5),
        DashboardUser(username="bryan", display_name="Bryan", role="user", disabled=False,
                      password_hash=password_hash, totp_enc=dashboard_encrypt(os.urandom(32), TOTP_SECRET),
                      totp_pending=False, teams=["mobile", "talentstorehouse", "gone"]),
        DashboardUser(username="newbie", display_name="", role="user", disabled=True, password_hash=password_hash,
                      totp_enc=dashboard_encrypt(SESSION_KEY, TOTP_SECRET), totp_pending=True, teams=[]),
    ]


def _run(conn, keyring, ops=FIXTURE, dashboard=None):
    source = (lambda: dashboard) if dashboard is not None else None
    return import_ops(conn, keyring, ops, now=utcnow(), users_source=source, session_key=SESSION_KEY)


def _counts(conn):
    return {table.name: conn.execute(select(func.count()).select_from(table)).scalar() for table in EVERY_TABLE}


def test_parse_env_reads_sh_quoting_and_drops_comments():
    env = parse_env((FIXTURE / "config" / "ops.env").read_text())
    assert env["GITHUB_WEBHOOK_SECRET"] == "fixture webhook secret"
    assert env["JIRA_API_TOKEN"] == "fixture-jira-token"
    assert env["JIRA_TRANSITION_QA"] == "In Test"
    assert env["SLACK_RELEASE_CHANNEL"] == ""
    assert "SLACK_SIGNING_SECRET" not in env


def test_secret_names():
    assert all(is_secret_name(n) for n in ("GH_TOKEN", "SLACK_SIGNING_SECRET", "LINEAR_API_KEY", "X_PASSWORD", "A_PEM"))
    assert not any(is_secret_name(n) for n in ("CLAUDE_REVIEW_MODEL", "AWS_REGION_NAME", "MRP_GITHUB_APP_ID"))


def test_import_maps_every_part_of_the_tree(engine, keyring):
    with engine.begin() as conn:
        report = _run(conn, keyring, dashboard=[])
    with engine.connect() as conn:
        team_ids = {r.slug: r.id for r in conn.execute(select(teams))}
        assert set(team_ids) == {"mobile", "talentstorehouse", "product-prototype"}
        mobile, tsh = team_ids["mobile"], team_ids["talentstorehouse"]
        ios = conn.execute(select(projects).where(projects.c.repo == "fs-eng/mobile-ios")).one()
        assert (ios.team_id, ios.name, ios.platform) == (mobile, "mobile-ios", "ios")
        assert ios.enabled_workflows == ["daily_review", "pr_review"]
        assert ios.branch_filters == ["master"]
        assert conn.execute(select(projects.c.team_id).where(
            projects.c.repo == "MountainRiverPartners/talentstorehouse")).scalar() == tsh
        assert conn.execute(select(func.count()).select_from(projects)).scalar() == 3

        flows = {r.name: r for r in conn.execute(select(workflows))}
        assert set(flows) == {"pr-role-review", "unsafe"}
        assert all(r.team_id is None and r.scope == "*" for r in flows.values())
        notes = dict(conn.execute(select(workflows.c.name, versions.c.note).join(
            versions, versions.c.object_id == workflows.c.id)).all())
        assert lint_ok(notes["pr-role-review"]) is True
        assert lint_ok(notes["unsafe"]) is False
        body = conn.execute(select(versions.c.body).where(versions.c.object_id == flows["pr-role-review"].id)).scalar()
        assert body == (FIXTURE / "specs" / "pr-role-review.yml").read_text()

        docs = {(r.kind, r.key): r for r in conn.execute(select(documents))}
        assert set(docs) == {("role", "security"), ("role", "correctness"), ("role", "test_adequacy"),
                             ("prompt", "_shared"), ("prompt", "team_lead_pr")}
        role_body = conn.execute(select(versions.c.body).where(
            versions.c.object_id == docs[("role", "correctness")].id)).scalar()
        _, matter, prompt = role_body.split("---\n", 2)
        front = yaml.safe_load(matter)
        assert front["name"] == "correctness"
        assert front["applies_to"] == ["ios", "android"]
        assert front["tools"] == ["WebFetch(domain:developer.android.com)", "mcp__jira__jira_get_issue"]
        assert front["mcp_inherit"] == ["jira"]
        assert front["mcp_servers"]["sosumi"]["type"] == "http"
        assert prompt.lstrip("\n") == (FIXTURE / "prompts" / "correctness.md").read_text()
        adequacy = conn.execute(select(versions.c.body).where(
            versions.c.object_id == docs[("role", "test_adequacy")].id)).scalar()
        assert yaml.safe_load(adequacy.split("---\n", 2)[1])["models"] == {"dotnet": "us.anthropic.claude-sonnet-5-5"}

        stored = {r.key: r for r in conn.execute(select(settings))}
        assert "dashboard" not in " ".join(stored)
        assert stored["ops.validate"].value == {"fs-eng/android-tree": {"command": "./gradlew assembleDebug",
                                                                         "timeout": 2400}}
        assert stored["ops.roles.base_tools"].value == ["Read", "Grep", "Bash(git log:*)"]
        assert stored["teams.mobile.auto_fix"].value is True
        assert stored["teams.mobile.auto_fix"].scope == mobile
        assert stored["teams.talentstorehouse.auto_fix"].value is False

        bedrock = conn.execute(select(providers).where(providers.c.team_id == mobile)).one()
        assert bedrock.kind == "bedrock"
        assert bedrock.config == {"region": "us-east-1", "credential_secret": "AWS_BEARER_TOKEN_BEDROCK"}
        assert bedrock.aliases == {"review": "us.anthropic.claude-opus-5-5", "fix": "us.anthropic.claude-opus-5-5",
                                   "jira_work": "us.anthropic.claude-opus-5-5", "lead": "us.anthropic.claude-opus-5-5",
                                   "fast": "us.anthropic.claude-haiku-5-5"}
        kinds = sorted(conn.execute(select(providers.c.kind).where(providers.c.team_id == tsh)).scalars())
        assert kinds == ["bedrock", "claude-subscription"]
    assert any("CLAUDE_CODE_OAUTH_TOKEN" in w for w in report.warnings)
    assert any("missing prompt" in w for w in report.warnings)


def test_env_scope_rules(engine, keyring):
    with engine.begin() as conn:
        _run(conn, keyring)
    with engine.connect() as conn:
        tsh = conn.execute(select(teams.c.id).where(teams.c.slug == "talentstorehouse")).scalar()
        instance = {s["name"] for s in secret_store.list_secrets(conn, None)}
        team = {s["name"] for s in secret_store.list_secrets(conn, tsh)}
        assert instance == {"GH_TOKEN", "GITHUB_WEBHOOK_SECRET", "JIRA_API_TOKEN", "AWS_BEARER_TOKEN_BEDROCK"}
        assert team == {"MRP_SLACK_BOT_TOKEN", "MRP_GITHUB_APP_KEY_PEM", "LINEAR_API_KEY"}
        resolved = secret_store.resolve_for_run(conn, keyring, tsh, ["MRP_GITHUB_APP_KEY_PEM", "GH_TOKEN",
                                                                     "GITHUB_WEBHOOK_SECRET"])
        assert resolved["MRP_GITHUB_APP_KEY_PEM"] == (FIXTURE / "config" / "secrets" / "github-app-mrp.pem").read_text()
        assert resolved["GH_TOKEN"] == "fixture-gh-token-value"
        assert resolved["GITHUB_WEBHOOK_SECRET"] == "fixture webhook secret"
        scopes = dict(conn.execute(select(settings.c.key, settings.c.scope).where(settings.c.key.like("env.%"))).all())
        assert scopes["env.MRP_GITHUB_APP_ID"] == tsh
        assert scopes["env.CLAUDE_REVIEW_MODEL"] == "*"
        assert "env.MRP_GITHUB_APP_KEY" not in scopes
        assert not any(is_secret_name(key.removeprefix("env.")) for key in scopes)


def test_import_twice_changes_nothing_and_a_new_body_adds_one_version(engine, keyring, tmp_path):
    dashboard = _dashboard_users(hash_password(PASSWORD, cost=COST))
    with engine.begin() as conn:
        _run(conn, keyring, dashboard=dashboard)
    with engine.connect() as conn:
        before = _counts(conn)
        audited = conn.execute(select(func.count()).select_from(audit)).scalar()
    with engine.begin() as conn:
        report = _run(conn, keyring, dashboard=dashboard)
    assert {outcome for items in report.entries.values() for _, outcome in items} <= {"unchanged", "skipped (empty)"}
    with engine.connect() as conn:
        assert _counts(conn) == before
        assert conn.execute(select(func.count()).select_from(audit)).scalar() == audited

    ops = tmp_path / "ops"
    shutil.copytree(FIXTURE, ops)
    (ops / "prompts" / "security.md").write_text("You are the security reviewer, revised.\n")
    with engine.begin() as conn:
        report = _run(conn, keyring, ops=ops, dashboard=dashboard)
    assert ("security", "updated") in report.entries["roles"]
    with engine.connect() as conn:
        doc = conn.execute(select(documents).where(documents.c.key == "security")).one()
        assert doc.current_version == 2
        assert conn.execute(select(func.count()).select_from(versions)).scalar() == before["versions"] + 1


def test_every_write_is_audited_as_the_importer(engine, keyring):
    with engine.begin() as conn:
        _run(conn, keyring, dashboard=_dashboard_users(hash_password(PASSWORD, cost=COST)))
    with engine.connect() as conn:
        actors = set(conn.execute(select(audit.c.actor_id)).scalars())
        actions = set(conn.execute(select(audit.c.action)).scalars())
    assert actors == {ACTOR}
    assert {"team.create", "project.create", "workflow.create", "document.create", "setting.set", "secret.set",
            "provider.create", "user.create", "membership.create"} <= actions


def test_report_and_audit_never_carry_a_value(engine, keyring):
    password_hash = hash_password(PASSWORD, cost=COST)
    dashboard = _dashboard_users(password_hash)
    with engine.begin() as conn:
        report = _run(conn, keyring, dashboard=dashboard)
    with engine.connect() as conn:
        audit_text = repr(conn.execute(select(audit.c.detail)).scalars().all())
    text = "\n".join(report.lines())
    for value in (*FIXTURE_SECRETS, password_hash, TOTP_SECRET, dashboard[0].totp_enc,
                  base64.b64encode(SESSION_KEY).decode()):
        assert value not in text
        assert value not in audit_text


def test_dry_run_cli_writes_nothing_and_prints_names_only(tmp_path, monkeypatch):
    config = tmp_path / "server.yaml"
    runner = CliRunner()
    init = runner.invoke(app, ["server", "init", "--config", str(config), "--database-url",
                               f"sqlite:///{tmp_path / 'db.sqlite'}", "--key-file", str(tmp_path / "master.key"),
                               "--no-admin"])
    assert init.exit_code == 0, init.output
    result = runner.invoke(app, ["server", "import-ops", str(FIXTURE), "--config", str(config), "--dry-run",
                                 "--skip-users"])
    assert result.exit_code == 0, result.output
    assert "GH_TOKEN (instance): created" in result.output
    assert "Dry run: nothing was written." in result.output
    for value in FIXTURE_SECRETS:
        assert value not in result.output
    engine = make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    try:
        with engine.connect() as conn:
            assert all(count == 0 for count in _counts(conn).values())
    finally:
        engine.dispose()


def test_dashboard_derivation_matches_node():
    sealed = dashboard_encrypt(SESSION_KEY, TOTP_SECRET)
    assert decrypt_dashboard_secret(dashboard_totp_key(SESSION_KEY), sealed) == TOTP_SECRET
    with pytest.raises(ValueError):
        decrypt_dashboard_secret(dashboard_totp_key(os.urandom(32)), sealed)


def test_totp_is_resealed_so_the_authenticator_keeps_working(sqlite_engine, keyring):
    clock = Clock(utcnow())
    with sqlite_engine.begin() as conn:
        report = _run(conn, keyring, dashboard=_dashboard_users(hash_password(PASSWORD, cost=COST)))
    with sqlite_engine.connect() as conn:
        rows = {r.username: r for r in conn.execute(select(users))}
        tsh = conn.execute(select(teams.c.id).where(teams.c.slug == "talentstorehouse")).scalar()
        bryan_roles = dict(conn.execute(select(memberships.c.scope_id, memberships.c.role)
                                        .where(memberships.c.user_id == rows["bryan"].id)).all())
    stephan = rows["stephan"]
    assert stephan.is_admin and stephan.totp_confirmed_at is not None and stephan.last_totp_step == 5
    assert keyring.open_(stephan.totp_secret_enc, totp_aad(stephan.id), label="totp") == TOTP_SECRET
    assert rows["bryan"].totp_secret_enc is None and not rows["bryan"].is_admin
    assert bryan_roles[tsh] == "contributor" and len(bryan_roles) == 2
    assert rows["newbie"].totp_secret_enc is None and rows["newbie"].disabled
    assert any("bryan TOTP could not be decrypted" in w for w in report.warnings)
    assert any("newbie never confirmed" in w for w in report.warnings)
    assert any("gone" in w for w in report.warnings)

    client = make_client(sqlite_engine, keyring, clock)
    response = login(client, "stephan", pyotp.TOTP(TOTP_SECRET).at(clock()))
    assert response.status_code == 200, response.text


def test_a_user_who_signed_in_here_keeps_this_servers_sign_in(engine, keyring):
    clock = Clock(utcnow())
    user_id, own_secret = add_user(engine, keyring, clock, "stephan")
    with engine.begin() as conn:
        conn.execute(users.update().where(users.c.id == user_id).values(last_login_at=clock()))
        own_hash = conn.execute(select(users.c.password_hash).where(users.c.id == user_id)).scalar()
        report = _run(conn, keyring, dashboard=_dashboard_users(hash_password("another password!", cost=COST)))
    with engine.connect() as conn:
        row = conn.execute(select(users).where(users.c.id == user_id)).one()
    assert row.password_hash == own_hash
    assert keyring.open_(row.totp_secret_enc, totp_aad(user_id), label="totp") == own_secret
    assert any("stephan has signed in here" in w for w in report.warnings)


@pytest.fixture
def defs_world(sqlite_engine, keyring):
    clock = Clock(utcnow())
    with sqlite_engine.begin() as conn:
        _run(conn, keyring)
        team_ids = {r.slug: r.id for r in conn.execute(select(teams))}
        hidden = new_id()
        conn.execute(workflows.insert().values(id=hidden, team_id=team_ids["talentstorehouse"], name="private",
                                               current_version=0, created_at=clock(), updated_at=clock()))
    client = make_client(sqlite_engine, keyring, clock)
    if not any(getattr(route, "path", None) == "/api/workflows" for route in client.app.routes):
        client.app.include_router(routes_defs.router)
    ids = {name: add_user(sqlite_engine, keyring, clock, name, is_admin=name == "admin")[0]
           for name in ("admin", "viewer", "outsider")}
    add_membership(sqlite_engine, clock, ids["viewer"], "team", team_ids["mobile"], "viewer")
    return client, sqlite_engine, keyring, clock, ids, team_ids, hidden


def _as(world, name):
    client, engine, keyring, clock, ids, *_ = world
    sign_in_as(client, engine, keyring, clock, ids[name])
    return client


def test_routes_list_instance_workflows_to_anyone_and_team_ones_to_members(defs_world):
    hidden = defs_world[-1]
    names = {w["name"]: w for w in _as(defs_world, "outsider").get("/api/workflows").json()["workflows"]}
    assert set(names) == {"pr-role-review", "unsafe"}
    assert names["pr-role-review"]["lint_ok"] is True and names["unsafe"]["lint_ok"] is False
    assert names["unsafe"]["scope"] == "*" and names["unsafe"]["team_id"] is None
    assert _as(defs_world, "outsider").get(f"/api/workflows/{hidden}").status_code == 404
    assert "private" not in {w["name"] for w in _as(defs_world, "viewer").get("/api/workflows").json()["workflows"]}
    admin = _as(defs_world, "admin")
    assert "private" in {w["name"] for w in admin.get("/api/workflows").json()["workflows"]}
    detail = admin.get(f"/api/workflows/{names['pr-role-review']['id']}").json()
    assert detail["version"]["body"] == (FIXTURE / "specs" / "pr-role-review.yml").read_text()
    assert detail["version"]["author"] == ACTOR
    assert [v["version"] for v in detail["versions"]] == [1]


def test_routes_documents_by_kind(defs_world):
    client = _as(defs_world, "viewer")
    roles = client.get("/api/documents?kind=role").json()["documents"]
    assert {d["key"] for d in roles} == {"security", "correctness", "test_adequacy"}
    assert client.get("/api/documents?kind=bogus").status_code == 400
    detail = client.get(f"/api/documents/{roles[0]['id']}").json()
    assert detail["version"]["body"].startswith("---\n")
    assert client.get(f"/api/documents/{new_id()}").status_code == 404


def test_routes_providers_need_the_team_and_hide_values(defs_world):
    *_, team_ids, _hidden = defs_world
    tsh = team_ids["talentstorehouse"]
    listed = _as(defs_world, "admin").get(f"/api/teams/{tsh}/providers").json()["providers"]
    configured = {p["kind"]: p["configured"] for p in listed}
    assert configured == {"bedrock": True, "claude-subscription": False}
    assert "fixture" not in repr(listed)
    assert _as(defs_world, "viewer").get(f"/api/teams/{team_ids['mobile']}/providers").status_code == 200
    assert _as(defs_world, "viewer").get(f"/api/teams/{tsh}/providers").status_code == 404


def test_routes_settings_are_admin_only(defs_world):
    *_, team_ids, _hidden = defs_world
    assert _as(defs_world, "viewer").get("/api/settings").status_code == 403
    admin = _as(defs_world, "admin")
    every = admin.get("/api/settings").json()["settings"]
    assert {"key": "env.CLAUDE_REVIEW_MODEL", "value": "us.anthropic.claude-opus-5-5", "scope": "*"} in every
    scoped = admin.get(f"/api/settings?scope={team_ids['talentstorehouse']}").json()["settings"]
    assert {s["key"] for s in scoped} == {"env.MRP_GITHUB_APP_ID", "teams.talentstorehouse.auto_fix"}
    assert {s["scope"] for s in admin.get("/api/settings?scope=instance").json()["settings"]} == {"*"}


def test_0003_upgrades_a_live_database_and_backfills_scope(db_url):
    migrate(db_url, "0002")
    team_id, workflow_id = new_id(), new_id()
    engine = make_engine(db_url)
    try:
        with engine.begin() as conn:
            stamp = {"at": "2026-10-23 12:00:00"}
            conn.execute(text("INSERT INTO teams (id, name, created_at) VALUES (:t, 'live', :at)"),
                         {"t": team_id, **stamp})
            conn.execute(text("INSERT INTO workflows (id, team_id, name, current_version, created_at, updated_at) "
                              "VALUES (:w, :t, 'wf', 1, :at, :at)"), {"w": workflow_id, "t": team_id, **stamp})
            conn.execute(text("INSERT INTO runs (id, team_id, workflow_id, status, started_at) "
                              "VALUES (:r, :t, :w, 'succeeded', :at)"),
                         {"r": new_id(), "t": team_id, "w": workflow_id, **stamp})
    finally:
        engine.dispose()
    migrate(db_url)
    engine = make_engine(db_url)
    try:
        with engine.connect() as conn:
            assert conn.execute(select(workflows.c.scope).where(workflows.c.id == workflow_id)).scalar() == team_id
    finally:
        engine.dispose()

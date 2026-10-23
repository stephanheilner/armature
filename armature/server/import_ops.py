"""Copy an armature-ops tree's configuration into the server database (`armature server import-ops`).

The report names what changed and never carries a value: no secret, hash,
TOTP seed, or session key leaves this module except sealed into the database.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import Connection, insert, select, update

from armature.server import audit
from armature.server import secrets as secret_store
from armature.server.crypto import Keyring, SealError
from armature.server.db import new_id
from armature.server.db.schema import (
    INSTANCE_SCOPE,
    documents,
    memberships,
    projects,
    providers,
    scope_of,
    secrets,
    settings,
    teams,
    users,
    versions,
    workflows,
)
from armature.server.users import USERNAME, totp_aad
from armature.spec.template_lint import lint_tree

ACTOR = "import-ops"

TEAM_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ROLE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
REPO_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
BRANCH = re.compile(r"^(?!-)(?!/)(?!.*\.\.)(?!.*//)[A-Za-z0-9._/-]{1,100}(?<!/)(?<!\.)(?<!\.lock)$")
PLATFORMS = ("ios", "android", "backend", "dotnet")
ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
BASE32 = re.compile(r"^[A-Z2-7]+=*$")

BEHAVIOR_FILES = ("validate", "workers", "jira-work", "pr-authors", "ios-port", "linear-intake", "github-apps",
                  "pr-review", "team-lead-schema", "incremental-schema", "intake-chat-schema")
SECRET_MARKERS = ("TOKEN", "SECRET", "KEY", "PASSWORD")
MRP_TEAM = "talentstorehouse"
APP_KEY_PATH = "MRP_GITHUB_APP_KEY"
APP_KEY_SECRET = "MRP_GITHUB_APP_KEY_PEM"

BEDROCK_SECRET = "AWS_BEARER_TOKEN_BEDROCK"
SUBSCRIPTION_SECRET = "CLAUDE_CODE_OAUTH_TOKEN"
SUBSCRIPTION_TEAMS = (MRP_TEAM,)
LEAD_DEFAULT = "us.anthropic.claude-opus-5-5"
FAST_MODEL = "us.anthropic.claude-haiku-5-5"
ALIAS_ENV = (("review", "CLAUDE_REVIEW_MODEL"), ("lead", "CLAUDE_TEAM_LEAD_MODEL"), ("fix", "CLAUDE_PR_FIX_MODEL"),
             ("pr_review", "CLAUDE_PR_REVIEW_MODEL"), ("jira_work", "JIRA_WORK_MODEL"),
             ("intake", "INTAKE_CHAT_MODEL"))

DASHBOARD_TOTP_INFO = b"armature-dashboard totp encryption v1"
LINT_OK = "lint: ok"


@dataclass
class DashboardUser:
    username: str
    display_name: str | None
    role: str
    disabled: bool
    password_hash: str | None
    totp_enc: str | None
    totp_pending: bool
    teams: list[str]
    last_totp_step: int = 0
    must_change_password: bool = False


@dataclass
class Report:
    entries: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add(self, section: str, name: str, outcome: str) -> None:
        self.entries.setdefault(section, []).append((name, outcome))

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def counts(self, section: str) -> dict[str, int]:
        tally: dict[str, int] = {}
        for _, outcome in self.entries.get(section, []):
            tally[outcome] = tally.get(outcome, 0) + 1
        return tally

    def lines(self) -> list[str]:
        out = []
        for section, items in self.entries.items():
            summary = ", ".join(f"{count} {outcome}" for outcome, count in sorted(self.counts(section).items()))
            out.append(f"{section}: {summary}")
            out.extend(f"  {name}: {outcome}" for name, outcome in items)
        if self.warnings:
            out.append(f"warnings: {len(self.warnings)}")
            out.extend(f"  {message}" for message in self.warnings)
        return out


def lint_note(data: Any) -> str:
    """The template lint of a parsed workflow, as the first line of a version note and any issues after it."""
    if data is None:
        return "lint: not parsed"
    issues = lint_tree(data)
    if not issues:
        return LINT_OK
    return "\n".join([f"lint: {len(issues)} issue(s)", *(f"- {issue.message}" for issue in issues)])


def lint_ok(note: str | None) -> bool | None:
    """Whether a version note records a clean lint, or None when it records no lint."""
    for line in (note or "").splitlines():
        if line.startswith("lint:"):
            return line == LINT_OK
    return None


# As in sh, '#' starts a comment only at the start of a word and outside quotes, so foo#bar is one value.
def _without_comment(value: str) -> str:
    quote, escaped, word_start = None, False, True
    for i, char in enumerate(value):
        if escaped:
            escaped, word_start = False, False
            continue
        if char == "#" and word_start and not quote:
            return value[:i]
        if char == "\\" and quote != "'":
            escaped = True
        elif quote:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        word_start = not quote and not escaped and char.isspace()
    return value


def parse_env(text: str) -> dict[str, str]:
    """``KEY=value`` lines as ``sh`` reads them: quotes removed, comments dropped, nothing expanded."""
    values = {}
    for raw in text.splitlines():
        match = ENV_LINE.match(raw.strip())
        if not match:
            continue
        try:
            words = shlex.split(_without_comment(match.group(2)), comments=False, posix=True)
        except ValueError:
            continue
        values[match.group(1)] = " ".join(words)
    return values


def is_secret_name(name: str) -> bool:
    return any(marker in name for marker in SECRET_MARKERS) or name.endswith("_PEM")


def env_team(name: str) -> str | None:
    return MRP_TEAM if name.startswith("MRP_") or name == "LINEAR_API_KEY" else None


def dashboard_totp_key(session_key: bytes) -> bytes:
    """``userKeys(sessionKey).encryption`` from the dashboard's ``lib/users.js``."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=DASHBOARD_TOTP_INFO).derive(session_key)


def _b64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def decrypt_dashboard_secret(key: bytes, sealed: str) -> str:
    """``decryptSecret`` from ``lib/users.js``: ``v1.<iv>.<ciphertext>.<tag>``, base64url, AES-256-GCM."""
    parts = str(sealed).split(".")
    if len(parts) != 4 or parts[0] != "v1" or not all(parts[1:]):
        raise ValueError("unreadable secret")
    try:
        iv, ciphertext, tag = (_b64url(part) for part in parts[1:])
        return AESGCM(key).decrypt(iv, ciphertext + tag, None).decode("utf-8")
    except (InvalidTag, binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("unreadable secret") from exc


def load_dashboard_users(database_url: str) -> list[DashboardUser]:
    """Read the dashboard's users in a read-only transaction."""
    import psycopg

    with psycopg.connect(database_url) as conn:
        conn.read_only = True
        rows = conn.execute(
            "SELECT username, display_name, role, disabled, must_change_password, password_hash, totp_enc, "
            "totp_pending, teams, last_totp_step FROM users ORDER BY username").fetchall()
    return [DashboardUser(username=r[0], display_name=r[1], role=r[2], disabled=bool(r[3]),
                          must_change_password=bool(r[4]), password_hash=r[5], totp_enc=r[6],
                          totp_pending=bool(r[7]), teams=list(r[8] or []), last_totp_step=int(r[9] or 0))
            for r in rows]


@dataclass
class _Context:
    conn: Connection
    keyring: Keyring
    now: datetime
    report: Report
    team_ids: dict[str, str] = field(default_factory=dict)


def _audit(ctx: _Context, action: str, target_kind: str, target_id: str, team_id: str | None = None,
           **detail: Any) -> None:
    audit.record(ctx.conn, action=action, actor_id=ACTOR, team_id=team_id, target_kind=target_kind,
                 target_id=target_id, detail={"source": ACTOR, **detail})


def _read_json(path: Path, report: Report) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        report.warn(f"{path.name}: not readable JSON, skipped")
        return None


# Teams and projects follow the readers in armature-ops: a bad entry is skipped on its own and the first team
# to list a repository keeps it.
def _import_teams(ctx: _Context, ops: Path) -> None:
    path = ops / "config" / "teams.json"
    if not path.is_file():
        path = ops / "config" / "teams.example.json"
    data = _read_json(path, ctx.report)
    entries = data.get("teams") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        ctx.report.warn(f"{path.name}: no teams list")
        return
    claimed: set[str] = set()
    for entry in entries:
        slug = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(slug, str) or not TEAM_ID.fullmatch(slug) or slug in ctx.team_ids:
            ctx.report.warn(f"{path.name}: skipped a team entry with a bad or repeated id")
            continue
        name = entry.get("name") if isinstance(entry.get("name"), str) and entry["name"].strip() else slug
        team_id = _upsert_team(ctx, slug, name.strip())
        ctx.team_ids[slug] = team_id
        _put_setting(ctx, f"teams.{slug}.auto_fix", entry.get("auto_fix") is True, team_id)
        for repo in entry.get("repos") or []:
            _import_project(ctx, team_id, repo, claimed, path.name)


def _upsert_team(ctx: _Context, slug: str, name: str) -> str:
    row = ctx.conn.execute(select(teams).where(teams.c.slug == slug)).first()
    if row is None:
        row = ctx.conn.execute(select(teams).where(teams.c.slug.is_(None), teams.c.name.in_([name, slug]))).first()
    if row is None:
        team_id = new_id()
        ctx.conn.execute(insert(teams).values(id=team_id, name=name, slug=slug, created_at=ctx.now))
        _audit(ctx, "team.create", "team", team_id, team_id, name=name, slug=slug)
        ctx.report.add("teams", slug, "created")
        return team_id
    if row.name == name and row.slug == slug:
        ctx.report.add("teams", slug, "unchanged")
        return row.id
    ctx.conn.execute(update(teams).where(teams.c.id == row.id).values(name=name, slug=slug))
    _audit(ctx, "team.update", "team", row.id, row.id, name=name, slug=slug)
    ctx.report.add("teams", slug, "updated")
    return row.id


def _import_project(ctx: _Context, team_id: str, repo: Any, claimed: set[str], source: str) -> None:
    slug = repo.get("slug") if isinstance(repo, dict) else None
    platform = repo.get("platform") if isinstance(repo, dict) else None
    if not isinstance(slug, str) or not REPO_SLUG.fullmatch(slug) or platform not in PLATFORMS:
        ctx.report.warn(f"{source}: skipped a repository entry with a bad slug or platform")
        return
    if slug.lower() in claimed:
        ctx.report.warn(f"{source}: {slug} is listed by an earlier team, kept there")
        return
    claimed.add(slug.lower())
    pipelines = repo.get("pipelines") if isinstance(repo.get("pipelines"), dict) else {}
    enabled = sorted(name for name, on in pipelines.items() if on is True)
    raw_branches = repo.get("review_branches")
    branches = [b for b in raw_branches if isinstance(b, str) and BRANCH.fullmatch(b)] \
        if isinstance(raw_branches, list) else []
    values = {"repo": slug, "platform": platform, "enabled_workflows": enabled, "branch_filters": branches}
    name = slug.split("/", 1)[1]
    row = ctx.conn.execute(select(projects).where(projects.c.repo == slug)).first() or \
        ctx.conn.execute(select(projects).where(projects.c.team_id == team_id, projects.c.name == name)).first()
    if row is not None and row.team_id != team_id:
        ctx.conn.execute(update(projects).where(projects.c.id == row.id).values(team_id=team_id, **values))
        _audit(ctx, "project.move", "project", row.id, team_id, repo=slug, from_team=row.team_id)
        ctx.report.add("projects", slug, "moved to this team")
        ctx.report.warn(f"projects: {slug} moved from another team to the team teams.json names")
    elif row is None:
        project_id = new_id()
        ctx.conn.execute(insert(projects).values(id=project_id, team_id=team_id, name=name, created_at=ctx.now,
                                                 **values))
        _audit(ctx, "project.create", "project", project_id, team_id, repo=slug)
        ctx.report.add("projects", slug, "created")
    elif all(getattr(row, key) == value for key, value in values.items()):
        ctx.report.add("projects", slug, "unchanged")
    else:
        ctx.conn.execute(update(projects).where(projects.c.id == row.id).values(**values))
        _audit(ctx, "project.update", "project", row.id, team_id, repo=slug)
        ctx.report.add("projects", slug, "updated")


def _current_body(ctx: _Context, object_kind: str, object_id: str, version: int) -> str | None:
    return ctx.conn.execute(select(versions.c.body).where(
        versions.c.object_kind == object_kind, versions.c.object_id == object_id,
        versions.c.version == version)).scalar()


def _add_version(ctx: _Context, object_kind: str, object_id: str, version: int, body: str, note: str) -> None:
    ctx.conn.execute(insert(versions).values(id=new_id(), object_kind=object_kind, object_id=object_id,
                                             version=version, body=body, author_id=ACTOR, created_at=ctx.now,
                                             note=note))


def _upsert_versioned(ctx: _Context, table, object_kind: str, where: list, values: dict[str, Any], body: str,
                      note: str, section: str, label: str) -> None:
    row = ctx.conn.execute(select(table).where(*where)).first()
    if row is None:
        object_id = new_id()
        ctx.conn.execute(insert(table).values(id=object_id, current_version=1, created_at=ctx.now,
                                              updated_at=ctx.now, **values))
        _add_version(ctx, object_kind, object_id, 1, body, note)
        _audit(ctx, f"{object_kind}.create", object_kind, object_id, name=label)
        ctx.report.add(section, label, "created")
        return
    if _current_body(ctx, object_kind, row.id, row.current_version) == body:
        ctx.report.add(section, label, "unchanged")
        return
    version = row.current_version + 1
    _add_version(ctx, object_kind, row.id, version, body, note)
    ctx.conn.execute(update(table).where(table.c.id == row.id).values(current_version=version, updated_at=ctx.now))
    _audit(ctx, f"{object_kind}.version", object_kind, row.id, name=label, version=version)
    ctx.report.add(section, label, "updated")


def _import_workflows(ctx: _Context, ops: Path) -> None:
    for path in sorted((ops / "specs").glob("*.yml")):
        body = path.read_text()
        try:
            data = yaml.safe_load(body)
        except yaml.YAMLError:
            data = None
            ctx.report.warn(f"specs/{path.name}: YAML did not parse, imported as text")
        named = data.get("name") if isinstance(data, dict) else None
        name = named if isinstance(named, str) and TEAM_ID.fullmatch(named) else path.stem
        lint = lint_note(data)
        if lint_ok(lint) is not True:
            ctx.report.warn(f"workflows: {name} {lint.splitlines()[0]}")
        note = f"Imported from armature-ops specs/{path.name}.\n{lint}"
        _upsert_versioned(ctx, workflows, "workflow", [workflows.c.scope == INSTANCE_SCOPE, workflows.c.name == name],
                          {"team_id": None, "scope": INSTANCE_SCOPE, "name": name}, body, note, "workflows", name)


def role_document(role: dict[str, Any], prompt: str) -> str:
    """A ``roles.json`` entry and its prompt as one Markdown document with YAML front matter."""
    front: dict[str, Any] = {"name": role["name"]}
    if isinstance(role.get("description"), str):
        front["description"] = role["description"]
    front["applies_to"] = list(role.get("platforms") or [])
    for source, target in (("extra_tools", "tools"), ("mcp_servers", "mcp_servers"),
                           ("mcp_inherit", "mcp_inherit"), ("models", "models")):
        if role.get(source):
            front[target] = role[source]
    matter = yaml.safe_dump(front, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return f"---\n{matter}---\n\n{prompt}"


def _upsert_document(ctx: _Context, kind: str, key: str, body: str, source: str) -> None:
    _upsert_versioned(ctx, documents, "document",
                      [documents.c.scope == INSTANCE_SCOPE, documents.c.kind == kind, documents.c.key == key],
                      {"team_id": None, "scope": INSTANCE_SCOPE, "kind": kind, "key": key}, body,
                      f"Imported from armature-ops {source}.", "roles" if kind == "role" else "prompts", key)


def _import_documents(ctx: _Context, ops: Path) -> None:
    prompts_dir = ops / "prompts"
    used: set[str] = set()
    data = _read_json(ops / "config" / "roles.json", ctx.report)
    if isinstance(data, dict):
        if isinstance(data.get("base_tools"), list):
            _put_setting(ctx, "ops.roles.base_tools", data["base_tools"], None)
        for role in data.get("roles") or []:
            name = role.get("name") if isinstance(role, dict) else None
            prompt_rel = role.get("prompt") if isinstance(role, dict) else None
            prompt_path = (ops / prompt_rel).resolve() if isinstance(prompt_rel, str) else None
            if not isinstance(name, str) or not ROLE_NAME.fullmatch(name) or prompt_path is None \
                    or prompt_path.parent != prompts_dir.resolve() or not prompt_path.is_file():
                ctx.report.warn("roles.json: skipped a role with a bad name or a missing prompt file")
                continue
            used.add(prompt_path.name)
            _upsert_document(ctx, "role", name, role_document(role, prompt_path.read_text()),
                             f"config/roles.json and prompts/{prompt_path.name}")
    for path in sorted(prompts_dir.glob("*.md")):
        if path.name not in used:
            _upsert_document(ctx, "prompt", path.stem, path.read_text(), f"prompts/{path.name}")


def _put_setting(ctx: _Context, key: str, value: Any, team_id: str | None) -> None:
    scope = scope_of(team_id)
    where = (settings.c.scope == scope, settings.c.key == key)
    row = ctx.conn.execute(select(settings).where(*where)).first()
    if row is not None and row.value == value:
        ctx.report.add("settings", key, "unchanged")
        return
    values = {"value": value, "team_id": team_id, "scope": scope, "updated_at": ctx.now, "updated_by": ACTOR}
    if row is None:
        ctx.conn.execute(insert(settings).values(key=key, **values))
    else:
        ctx.conn.execute(update(settings).where(*where).values(**values))
    _audit(ctx, "setting.set", "setting", key, team_id, key=key)
    ctx.report.add("settings", key, "created" if row is None else "updated")


def _import_behavior(ctx: _Context, ops: Path) -> None:
    for stem in BEHAVIOR_FILES:
        path = ops / "config" / f"{stem}.json"
        if not path.is_file():
            continue
        value = _read_json(path, ctx.report)
        if value is not None:
            _put_setting(ctx, f"ops.{stem}", value, None)


def _put_secret(ctx: _Context, name: str, value: str, team_id: str | None) -> None:
    label = f"{name} ({'instance' if team_id is None else 'team ' + MRP_TEAM})"
    row = ctx.conn.execute(select(secrets.c.id, secrets.c.ciphertext)
                           .where(secrets.c.scope == scope_of(team_id), secrets.c.name == name)).first()
    if row is not None:
        try:
            if ctx.keyring.open_(row.ciphertext, secret_store.aad(team_id, row.id, name)) == value:
                ctx.report.add("secrets", label, "unchanged")
                return
        except SealError:
            pass
    secret_store.set_secret(ctx.conn, ctx.keyring, team_id=team_id, name=name, value=value, actor_id=ACTOR)
    ctx.report.add("secrets", label, "created" if row is None else "updated")


def _import_env(ctx: _Context, ops: Path) -> dict[str, str]:
    path = ops / "config" / "ops.env"
    if not path.is_file():
        ctx.report.warn("config/ops.env: missing, no settings or secrets imported from it")
        return {}
    env = parse_env(path.read_text())
    for name, value in env.items():
        team_slug = env_team(name)
        team_id = ctx.team_ids.get(team_slug) if team_slug else None
        if team_slug and team_id is None:
            ctx.report.warn(f"ops.env: {name} belongs to team {team_slug}, which was not imported")
            continue
        if name == APP_KEY_PATH:
            _import_app_key(ctx, ops, value, team_id)
        elif is_secret_name(name):
            if value:
                _put_secret(ctx, name, value, team_id)
            else:
                ctx.report.add("secrets", name, "skipped (empty)")
        else:
            _put_setting(ctx, f"env.{name}", value, team_id)
    return env


def _import_app_key(ctx: _Context, ops: Path, value: str, team_id: str | None) -> None:
    key_path = Path(value).expanduser()
    key_path = key_path if key_path.is_absolute() else ops / key_path
    try:
        pem = key_path.read_text()
    except (OSError, UnicodeDecodeError):
        ctx.report.warn(f"ops.env: {APP_KEY_PATH} names a file that could not be read")
        return
    _put_secret(ctx, APP_KEY_SECRET, pem, team_id)


def _secret_set(conn: Connection, name: str, team_id: str) -> bool:
    return conn.execute(select(secrets.c.id).where(secrets.c.name == name,
                                                   secrets.c.scope.in_([team_id, INSTANCE_SCOPE]))).first() is not None


def _upsert_provider(ctx: _Context, team_slug: str, team_id: str, kind: str, config: dict, aliases: dict) -> None:
    label = f"{team_slug}/{kind}"
    row = ctx.conn.execute(select(providers).where(providers.c.team_id == team_id, providers.c.kind == kind)).first()
    if row is None:
        provider_id = new_id()
        ctx.conn.execute(insert(providers).values(id=provider_id, team_id=team_id, kind=kind, config=config,
                                                  aliases=aliases, created_at=ctx.now, updated_at=ctx.now))
        _audit(ctx, "provider.create", "provider", provider_id, team_id, kind=kind)
        ctx.report.add("providers", label, "created")
    elif row.config == config and row.aliases == aliases:
        ctx.report.add("providers", label, "unchanged")
    else:
        ctx.conn.execute(update(providers).where(providers.c.id == row.id)
                         .values(config=config, aliases=aliases, updated_at=ctx.now))
        _audit(ctx, "provider.update", "provider", row.id, team_id, kind=kind)
        ctx.report.add("providers", label, "updated")


def _import_providers(ctx: _Context, env: dict[str, str]) -> None:
    aliases = {alias: env[var] for alias, var in ALIAS_ENV if env.get(var)}
    aliases.setdefault("lead", LEAD_DEFAULT)
    aliases["fast"] = FAST_MODEL
    bedrock = {"region": env.get("AWS_REGION_NAME") or env.get("AWS_REGION") or None,
               "credential_secret": BEDROCK_SECRET}
    for slug, team_id in ctx.team_ids.items():
        _upsert_provider(ctx, slug, team_id, "bedrock", bedrock, aliases)
        if slug in SUBSCRIPTION_TEAMS:
            _upsert_provider(ctx, slug, team_id, "claude-subscription", {"credential_secret": SUBSCRIPTION_SECRET}, {})
            if not _secret_set(ctx.conn, SUBSCRIPTION_SECRET, team_id):
                ctx.report.warn(f"providers: {slug}/claude-subscription is not configured until "
                                f"{SUBSCRIPTION_SECRET} is set")


def _dashboard_totp(ctx: _Context, user: DashboardUser, key: bytes | None) -> str | None:
    if not user.totp_enc:
        return None
    if user.totp_pending:
        ctx.report.warn(f"users: {user.username} never confirmed an authenticator, imported without TOTP")
        return None
    if key is None:
        ctx.report.warn(f"users: {user.username} kept no TOTP because the dashboard session key is unavailable")
        return None
    try:
        secret = decrypt_dashboard_secret(key, user.totp_enc)
    except ValueError:
        secret = None
    if secret is None or not BASE32.fullmatch(secret):
        ctx.report.warn(f"users: {user.username} TOTP could not be decrypted, imported without TOTP")
        return None
    return secret


def _stored_totp(ctx: _Context, row: Any) -> str | None:
    if not row.totp_secret_enc:
        return None
    try:
        return ctx.keyring.open_(row.totp_secret_enc, totp_aad(row.id), label="totp")
    except SealError:
        return None


def _import_user(ctx: _Context, user: DashboardUser, key: bytes | None) -> None:
    username = user.username.strip().lower() if isinstance(user.username, str) else ""
    if not USERNAME.fullmatch(username):
        ctx.report.warn("users: skipped a dashboard user with an invalid username")
        return
    password_hash = user.password_hash if isinstance(user.password_hash, str) and \
        user.password_hash.startswith("scrypt$") and len(user.password_hash.split("$")) == 6 else None
    if user.must_change_password:
        password_hash = None
        ctx.report.warn(f"users: {username} still had a generated password in the dashboard, "
                        "imported without one, needs an invite")
    elif password_hash is None:
        ctx.report.warn(f"users: {username} has no usable password hash, needs an invite")
    totp = _dashboard_totp(ctx, user, key)
    is_admin = user.role == "admin"
    display = (user.display_name or "").strip() or username
    row = ctx.conn.execute(select(users).where(users.c.username == username)).first()
    if row is None:
        user_id = new_id()
        ctx.conn.execute(insert(users).values(
            id=user_id, username=username, display_name=display, password_hash=password_hash,
            totp_secret_enc=ctx.keyring.seal(totp, totp_aad(user_id), label="totp") if totp else None,
            totp_confirmed_at=ctx.now if totp else None, last_totp_step=user.last_totp_step if totp else 0,
            is_admin=is_admin, disabled=user.disabled, created_at=ctx.now))
        _audit(ctx, "user.create", "user", user_id, username=username, totp=bool(totp))
        ctx.report.add("users", username, "created")
    else:
        user_id = row.id
        changes: dict[str, Any] = {}
        if row.display_name != display:
            changes["display_name"] = display
        if is_admin and not row.is_admin:
            changes["is_admin"] = True
        demotion = row.is_admin and not is_admin
        if bool(row.disabled) != user.disabled:
            changes["disabled"] = user.disabled
        # Once someone signs in here, this server owns their password and authenticator.
        if row.last_login_at is None:
            if row.password_hash != password_hash and (password_hash or user.must_change_password):
                changes["password_hash"] = password_hash
            if totp and _stored_totp(ctx, row) != totp:
                changes.update(totp_secret_enc=ctx.keyring.seal(totp, totp_aad(user_id), label="totp"),
                               totp_confirmed_at=ctx.now)
            # Never lower the replay floor, so a code already used on either side stays used.
            if totp and user.last_totp_step > row.last_totp_step:
                changes["last_totp_step"] = user.last_totp_step
        elif password_hash != row.password_hash or (totp and _stored_totp(ctx, row) != totp):
            ctx.report.warn(f"users: {username} has signed in here, kept this server's password and TOTP")
        if changes:
            ctx.conn.execute(update(users).where(users.c.id == user_id).values(**changes))
            _audit(ctx, "user.update", "user", user_id, username=username, fields=sorted(changes))
        outcome = "updated" if changes else "unchanged"
        ctx.report.add("users", username, "admin kept; demote in the UI" if demotion else outcome)
    if not ctx.conn.execute(select(users.c.totp_confirmed_at).where(users.c.id == user_id)).scalar():
        ctx.report.warn(f"users: {username} has no confirmed authenticator here, so needs an invite to sign in")
    for slug in user.teams:
        _import_membership(ctx, user_id, username, slug)


def _import_membership(ctx: _Context, user_id: str, username: str, slug: str) -> None:
    team_id = ctx.team_ids.get(slug)
    label = f"{username} in {slug}"
    if team_id is None:
        ctx.report.warn(f"memberships: {label} skipped, no such team")
        return
    exists = ctx.conn.execute(select(memberships.c.id).where(
        memberships.c.user_id == user_id, memberships.c.scope_kind == "team",
        memberships.c.scope_id == team_id)).first()
    if exists:
        ctx.report.add("memberships", label, "unchanged")
        return
    membership_id = new_id()
    ctx.conn.execute(insert(memberships).values(id=membership_id, user_id=user_id, scope_kind="team",
                                                scope_id=team_id, role="contributor", source="manual",
                                                created_at=ctx.now))
    _audit(ctx, "membership.create", "membership", membership_id, team_id, username=username, role="contributor")
    ctx.report.add("memberships", label, "created")


def import_ops(conn: Connection, keyring: Keyring, ops_dir: Path, *, now: datetime,
               users_source: Callable[[], list[DashboardUser]] | None = None,
               session_key: bytes | None = None) -> Report:
    """Import everything in one transaction the caller commits or rolls back."""
    ops = ops_dir.expanduser().resolve()
    if not (ops / "config").is_dir():
        raise ValueError(f"{ops} has no config directory, so it is not an armature-ops tree")
    ctx = _Context(conn=conn, keyring=keyring, now=now, report=Report())
    _import_teams(ctx, ops)
    _import_workflows(ctx, ops)
    _import_documents(ctx, ops)
    _import_behavior(ctx, ops)
    env = _import_env(ctx, ops)
    _import_providers(ctx, env)
    if users_source is not None:
        key = dashboard_totp_key(session_key) if session_key else None
        for user in users_source():
            _import_user(ctx, user, key)
    return ctx.report


def dashboard_source(ops_dir: Path) -> tuple[Callable[[], list[DashboardUser]] | None, bytes | None, str | None]:
    """The dashboard's user reader and session key from ``config/dashboard.json``, or why there is none."""
    path = ops_dir.expanduser() / "config" / "dashboard.json"
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError):
        return None, None, "config/dashboard.json is missing or not JSON"
    url = config.get("database_url") if isinstance(config, dict) else None
    if not isinstance(url, str) or not url:
        return None, None, "config/dashboard.json has no database_url"
    session_key = None
    if isinstance(config.get("session_key"), str):
        try:
            session_key = base64.b64decode(config["session_key"], validate=True)
        except binascii.Error:
            session_key = None
    return (lambda: load_dashboard_users(url)), session_key, None

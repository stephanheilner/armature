"""`armature server` commands: init, migrate, create-admin, run, import-ops, import-history, and pack."""
from __future__ import annotations

import getpass
from pathlib import Path

import typer

server_app = typer.Typer(name="server", help="Run and administer the Armature server (needs armature\\[server])",
                         no_args_is_help=True)

pack_app = typer.Typer(name="pack", help="Install and remove workflow packs on this host", no_args_is_help=True)
server_app.add_typer(pack_app, name="pack")

ConfigOption = typer.Option(None, "--config", help="Server config file (default $ARMATURE_SERVER_CONFIG "
                                                   "or ~/.armature/server.yaml)")


def _fail(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(1)


def _load(config: Path | None):
    from pydantic import ValidationError

    from armature.server.config import load_config
    try:
        return load_config(config)
    except (ValidationError, ValueError) as exc:
        _fail(f"Invalid server config: {exc}")


def _keyring(cfg):
    from armature.server.crypto import MasterKeyError, keyring_from_source
    try:
        return keyring_from_source(cfg.master_key)
    except (MasterKeyError, NotImplementedError) as exc:
        _fail(f"Master key unavailable: {exc}")


def _prompt_password() -> str:
    from armature.server.passwords import check_password_policy
    while True:
        password = getpass.getpass("Password: ")
        try:
            check_password_policy(password)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            continue
        if getpass.getpass("Repeat password: ") == password:
            return password
        typer.echo("Passwords differ.", err=True)


CONFIRM_TRIES = 3


def _create_admin(cfg, username: str | None) -> None:
    from armature.server.db import make_engine
    from armature.server.users import create_admin
    keyring = _keyring(cfg)
    username = username or typer.prompt("Admin username")
    password = _prompt_password()
    engine = make_engine(cfg.database_url)
    try:
        try:
            with engine.begin() as conn:
                admin = create_admin(conn, keyring, username=username, password=password)
        except ValueError as exc:
            _fail(str(exc))
        typer.echo(f"Created admin {username}. Add this to an authenticator app now. It is not shown again:")
        typer.echo(admin.otpauth_uri)
        confirmed = False
        try:
            confirmed = _confirm_admin(engine, keyring, admin.user_id)
        finally:
            # Any way out without a confirmed code, Ctrl-C included, removes the admin it made.
            if not confirmed:
                _remove_unconfirmed(engine, admin.user_id)
        if not confirmed:
            _fail("No code matched, so the admin was removed. Run the command again.")
    finally:
        engine.dispose()
    typer.echo("Authenticator confirmed.")


def _confirm_admin(engine, keyring, user_id: str) -> bool:
    """Ask for codes until one confirms the authenticator. Returns False after three misses."""
    from sqlalchemy import select

    from armature.server import audit
    from armature.server.auth import confirm_enrollment, match_totp, totp_secret
    from armature.server.db import utcnow
    from armature.server.db.schema import users
    for _ in range(CONFIRM_TRIES):
        code = typer.prompt("Code from the authenticator app")
        now = utcnow()
        with engine.begin() as conn:
            user = conn.execute(select(users).where(users.c.id == user_id)).one()
            step = match_totp(totp_secret(keyring, user), code, now)
            if step is not None and confirm_enrollment(conn, user_id, step, now):
                audit.record(conn, action="user.totp_confirm", actor_id=user_id, target_kind="user", target_id=user_id)
                return True
        typer.echo("That code did not match.", err=True)
    return False


def _remove_unconfirmed(engine, user_id: str) -> None:
    from sqlalchemy import delete

    from armature.server import audit
    from armature.server.db.schema import users
    with engine.begin() as conn:
        conn.execute(delete(users).where(users.c.id == user_id, users.c.totp_confirmed_at.is_(None)))
        audit.record(conn, action="user.delete_unconfirmed", actor_id=None, target_kind="user", target_id=user_id)


@server_app.command("init")
def init(
    config: Path | None = ConfigOption,
    database_url: str | None = typer.Option(None, "--database-url",
                                            help="SQLAlchemy URL, used only when writing a new config. "
                                                 "Prefer $ARMATURE_SERVER_DATABASE_URL for a URL with a password"),
    key_file: Path | None = typer.Option(None, "--key-file",
                                         help="Master key file to create, used only when writing a new config"),
    username: str | None = typer.Option(None, "--username", help="First admin's username (prompted if omitted)"),
    no_admin: bool = typer.Option(False, "--no-admin", help="Skip creating the first admin"),
):
    """Write a config if missing, create the master key, migrate, and create the first admin."""
    import os

    from sqlalchemy.engine import make_url

    from armature.server.config import (
        ENV_PREFIX,
        ServerConfig,
        config_path,
        write_config,
    )
    from armature.server.crypto import write_key_file
    from armature.server.db import make_engine, migrate

    path = (config or config_path()).expanduser()
    if not path.exists():
        values = {}
        if database_url and make_url(database_url).password:
            typer.echo(f"Warning: --database-url holds a password, which shell history and ps can show. "
                       f"Set {ENV_PREFIX}DATABASE_URL instead.", err=True)
        database_url = database_url or os.environ.get(ENV_PREFIX + "DATABASE_URL")
        if database_url:
            values["database_url"] = database_url
        if key_file:
            values["master_key"] = f"file:{key_file.expanduser()}"
        write_config(ServerConfig(**values), path)
        typer.echo(f"Wrote {path}")
    cfg = _load(path)
    migrate(cfg.database_url)
    typer.echo("Database is at the latest schema.")
    from armature.server.users import has_admin, has_sealed_values
    engine = make_engine(cfg.database_url)
    try:
        with engine.connect() as conn:
            sealed, exists = has_sealed_values(conn), has_admin(conn)
    finally:
        engine.dispose()
    scheme, _, rest = cfg.master_key.partition(":")
    key_path = Path(rest).expanduser()
    if scheme == "file" and not os.path.lexists(key_path):
        if sealed:
            _fail(f"Master key file {key_path} is missing, but the database holds values sealed with a master key. "
                  "Restore the key file from backup. A new key could never open them.")
        write_key_file(key_path)
        typer.echo(f"Generated master key {key_path} (mode 0600). Back it up: without it no secret can be read.")
    _keyring(cfg)
    if no_admin:
        return
    if exists:
        typer.echo("An admin already exists. Use `armature server create-admin` to add another.")
        return
    _create_admin(cfg, username)


@server_app.command("migrate")
def migrate_cmd(config: Path | None = ConfigOption):
    """Upgrade the database to the latest schema."""
    from armature.server.db import migrate
    cfg = _load(config)
    migrate(cfg.database_url)
    typer.echo("Database is at the latest schema.")


@server_app.command("create-admin")
def create_admin_cmd(
    config: Path | None = ConfigOption,
    username: str | None = typer.Option(None, "--username", help="Admin username (prompted if omitted)"),
):
    """Create an admin. The password is always prompted, never taken from arguments."""
    _create_admin(_load(config), username)


@server_app.command("run")
def run(config: Path | None = ConfigOption):
    """Serve the API and the browser UI. Refuses to start without an admin or a working master key."""
    from sqlalchemy.exc import SQLAlchemyError

    from armature.server.db import make_engine
    from armature.server.users import has_admin
    cfg = _load(config)
    keyring = _keyring(cfg)
    engine = make_engine(cfg.database_url)
    try:
        with engine.connect() as conn:
            exists = has_admin(conn)
    except SQLAlchemyError as exc:
        engine.dispose()
        _fail(f"Database unavailable or not migrated ({exc.__class__.__name__}). Run `armature server migrate`.")
    if not exists:
        engine.dispose()
        _fail("No admin exists. Run `armature server create-admin` first.")
    import uvicorn

    from armature.server.app import create_app
    app = create_app(cfg, engine, keyring)
    typer.echo(f"Armature server on http://{cfg.host}:{cfg.port}{cfg.base_path}/ (public origin {cfg.public_origin})")
    # proxy_headers off: the app trusts X-Real-IP only from a loopback peer and never X-Forwarded-For.
    uvicorn.run(app, host=cfg.host, port=cfg.port, workers=1, proxy_headers=False, server_header=False,
                log_level="info", timeout_graceful_shutdown=5)


@server_app.command("import-ops")
def import_ops_cmd(
    ops_dir: Path = typer.Argument(..., help="An armature-ops checkout to read. Nothing in it is written"),
    config: Path | None = ConfigOption,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the report and roll every change back"),
    skip_users: bool = typer.Option(False, "--skip-users", help="Leave the dashboard's users out"),
):
    """Copy teams, workflows, roles, prompts, settings, secrets, providers, and dashboard users into the database."""
    from armature.server.db import make_engine, utcnow
    from armature.server.import_ops import dashboard_source, import_ops

    cfg = _load(config)
    keyring = _keyring(cfg)
    users_source, session_key = None, None
    if not skip_users:
        users_source, session_key, reason = dashboard_source(ops_dir)
        if reason:
            typer.echo(f"Users skipped: {reason}.", err=True)
    engine = make_engine(cfg.database_url)
    try:
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                report = import_ops(conn, keyring, ops_dir, now=utcnow(), users_source=users_source,
                                    session_key=session_key)
            except ValueError as exc:
                transaction.rollback()
                _fail(f"Import failed, nothing was written: {exc}")
            except Exception as exc:  # noqa: BLE001
                transaction.rollback()
                # The class alone, because SQL errors echo parameters and driver errors connection strings.
                _fail(f"Import failed, nothing was written: {exc.__class__.__name__}")
            if dry_run:
                transaction.rollback()
            else:
                transaction.commit()
    finally:
        engine.dispose()
    for line in report.lines():
        typer.echo(line)
    typer.echo("Dry run: nothing was written." if dry_run else "Import committed.")


def _since(value: str | None):
    from datetime import UTC, datetime
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _fail("--since takes a date such as 2026-10-01 or an ISO time.")
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


@server_app.command("import-history")
def import_history_cmd(
    ops_dir: Path = typer.Argument(..., help="An armature-ops checkout whose config/dashboard.json names the "
                                             "dashboard database. Nothing in it or the database is written"),
    config: Path | None = ConfigOption,
    traces: Path = typer.Option(Path("~/.armature/traces.db"), "--traces",
                                help="traces.db to read runs the dashboard lacks from, opened read-only"),
    no_traces: bool = typer.Option(False, "--no-traces", help="Import only the dashboard's runs"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the counts and roll every change back"),
    since: str | None = typer.Option(None, "--since", help="Only runs started on or after this date (UTC)"),
    limit: int | None = typer.Option(None, "--limit", min=1, help="At most this many runs, oldest first"),
):
    """Copy past runs, their steps, findings, and a timeline from the dashboard and traces.db into the database."""
    import time

    from armature.server.db import make_engine, utcnow
    from armature.server.import_history import import_history, ops_source

    cfg = _load(config)
    since_at = _since(since)
    keyring = _keyring(cfg)
    started = time.monotonic()
    try:
        source = ops_source(ops_dir, None if no_traces else traces.expanduser())
    except Exception as exc:  # noqa: BLE001
        # The class alone for a driver error, which can echo the connection string.
        _fail(f"Dashboard unavailable: {exc if isinstance(exc, ValueError) else exc.__class__.__name__}")
    kept = "" if dry_run else " Batches committed before it stay."
    engine = make_engine(cfg.database_url)
    try:
        report = import_history(engine, keyring, source, now=utcnow(), since=since_at, limit=limit,
                                traces=not no_traces, dry_run=dry_run,
                                progress=lambda line: typer.echo(line, err=True))
    except ValueError as exc:
        _fail(f"Import stopped: {exc}.{kept}")
    except Exception as exc:  # noqa: BLE001
        _fail(f"Import stopped: {exc.__class__.__name__}.{kept}")
    finally:
        source.close()
        engine.dispose()
    for line in report.lines():
        typer.echo(line)
    typer.echo(f"{'Dry run: nothing was written' if dry_run else 'Import committed'} "
               f"in {time.monotonic() - started:.1f}s.")


@pack_app.command("install")
def pack_install(
    source: str = typer.Argument(..., help="A pack directory, or <git-url-or-repo-path>@<40-character commit sha>"),
    config: Path | None = ConfigOption,
    force: bool = typer.Option(False, "--force", help="Install over a newer installed version"),
):
    """Copy a pack into packs_dir, record it in the database, and audit it."""
    from armature.server.db import make_engine, utcnow
    from armature.server.packs import InstallError, install

    cfg = _load(config)
    engine = make_engine(cfg.database_url)
    try:
        installed = install(engine, Path(cfg.packs_dir), source, now=utcnow(), force=force)
    except InstallError as exc:
        _fail(f"Install refused, nothing changed: {exc}")
    finally:
        engine.dispose()
    was = f" (was {installed.previous_version})" if installed.previous_version else ""
    typer.echo(f"Installed {installed.name} {installed.version}{was} at {installed.path}, "
               f"pinned to {installed.pinned_ref}.")


@pack_app.command("remove")
def pack_remove(
    name: str = typer.Argument(..., help="The installed pack's name"),
    config: Path | None = ConfigOption,
):
    """Delete an installed pack's files and its database row, and audit it."""
    from armature.server.db import make_engine
    from armature.server.packs import InstallError, remove

    cfg = _load(config)
    engine = make_engine(cfg.database_url)
    try:
        version = remove(engine, Path(cfg.packs_dir), name)
    except InstallError as exc:
        _fail(f"Remove refused: {exc}")
    finally:
        engine.dispose()
    typer.echo(f"Removed {name}{f' {version}' if version else ''}.")

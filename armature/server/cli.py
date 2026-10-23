"""`armature server` commands: init, migrate, create-admin, and run."""
from __future__ import annotations

import getpass
from pathlib import Path

import typer

server_app = typer.Typer(name="server", help="Run and administer the Armature server (needs armature\\[server])",
                         no_args_is_help=True)

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


def _create_admin(cfg, username: str | None) -> None:
    from armature.server.db import make_engine
    from armature.server.users import create_admin
    keyring = _keyring(cfg)
    username = username or typer.prompt("Admin username")
    password = _prompt_password()
    engine = make_engine(cfg.database_url)
    try:
        with engine.begin() as conn:
            admin = create_admin(conn, keyring, username=username, password=password)
    except ValueError as exc:
        _fail(str(exc))
    finally:
        engine.dispose()
    typer.echo(f"Created admin {username}. Add this to an authenticator app now. It is not shown again:")
    typer.echo(admin.otpauth_uri)


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
    """Start the server (not yet implemented)."""
    _fail("armature server run is not implemented yet.")

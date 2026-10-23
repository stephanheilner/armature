import os

import pyotp
from sqlalchemy import select
from typer.testing import CliRunner

from armature.cli import app
from armature.server.config import load_config
from armature.server.crypto import keyring_from_source
from armature.server.db import make_engine
from armature.server.db.schema import users
from armature.server.passwords import verify_password
from armature.server.users import totp_aad

PASSWORD = "correct horse battery"


def _init(tmp_path, monkeypatch, *extra):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PASSWORD)
    config = tmp_path / "server.yaml"
    args = ["server", "init", "--config", str(config), "--database-url", f"sqlite:///{tmp_path / 'db.sqlite'}",
            "--key-file", str(tmp_path / "master.key"), "--username", "admin", *extra]
    return config, CliRunner().invoke(app, args)


def test_init_creates_config_key_schema_and_admin(tmp_path, monkeypatch):
    config, result = _init(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    assert os.stat(tmp_path / "master.key").st_mode & 0o777 == 0o600
    assert os.stat(config).st_mode & 0o777 == 0o600
    assert PASSWORD not in result.output
    uri = next(line for line in result.output.splitlines() if line.startswith("otpauth://"))

    cfg = load_config(config)
    engine = make_engine(cfg.database_url)
    with engine.connect() as conn:
        row = conn.execute(select(users)).one()
    engine.dispose()
    assert row.username == "admin" and row.is_admin
    assert verify_password(PASSWORD, row.password_hash)
    secret = keyring_from_source(cfg.master_key).open_(row.totp_secret_enc, totp_aad(row.id), label="totp")
    assert pyotp.parse_uri(uri).secret == secret


def test_init_again_keeps_the_admin(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    _, result = _init(tmp_path, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "already exists" in result.output
    assert "otpauth://" not in result.output


def test_init_refuses_a_world_readable_key(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch, "--no-admin")
    os.chmod(tmp_path / "master.key", 0o644)
    _, result = _init(tmp_path, monkeypatch)
    assert result.exit_code == 1
    assert "chmod 600" in result.output


def test_create_admin_has_no_password_option():
    result = CliRunner().invoke(app, ["server", "create-admin", "--password", "x"])
    assert result.exit_code == 2
    assert "No such option" in result.output


def test_init_refuses_a_new_key_over_sealed_values(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch)
    (tmp_path / "master.key").unlink()
    _, result = _init(tmp_path, monkeypatch)
    assert result.exit_code == 1
    assert "Restore the key file" in result.output
    assert not (tmp_path / "master.key").exists()


def test_init_makes_a_new_key_when_nothing_is_sealed(tmp_path, monkeypatch):
    _init(tmp_path, monkeypatch, "--no-admin")
    (tmp_path / "master.key").unlink()
    _, result = _init(tmp_path, monkeypatch, "--no-admin")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "master.key").exists()


def test_init_takes_database_url_from_env(tmp_path, monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": PASSWORD)
    url = f"sqlite:///{tmp_path / 'env.sqlite'}"
    monkeypatch.setenv("ARMATURE_SERVER_DATABASE_URL", url)
    config = tmp_path / "server.yaml"
    result = CliRunner().invoke(app, ["server", "init", "--config", str(config), "--key-file",
                                      str(tmp_path / "master.key"), "--no-admin"])
    assert result.exit_code == 0, result.output
    monkeypatch.delenv("ARMATURE_SERVER_DATABASE_URL")
    assert load_config(config).database_url == url
    assert (tmp_path / "env.sqlite").exists()


def test_init_warns_about_a_password_in_the_url(tmp_path, monkeypatch):
    config = tmp_path / "server.yaml"
    result = CliRunner().invoke(app, ["server", "init", "--config", str(config), "--database-url",
                                      "postgresql+psycopg://u:hunter2@127.0.0.1:1/x", "--key-file",
                                      str(tmp_path / "master.key"), "--no-admin"])
    assert "holds a password" in result.output


def test_run_is_a_stub():
    assert CliRunner().invoke(app, ["server", "run"]).exit_code == 1

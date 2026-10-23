"""User records. Sign-in and sessions come in a later slice."""
from __future__ import annotations

import re
from dataclasses import dataclass

import pyotp
from sqlalchemy import Connection, insert, select

from armature.server import audit
from armature.server.crypto import Keyring
from armature.server.db import new_id, utcnow
from armature.server.db.schema import secrets, users
from armature.server.passwords import check_password_policy, hash_password

USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{1,39}$")
TOTP_ISSUER = "Armature"


@dataclass(frozen=True)
class NewAdmin:
    user_id: str
    otpauth_uri: str


def totp_aad(user_id: str) -> dict[str, str]:
    return {"user_id": user_id}


def has_admin(conn: Connection) -> bool:
    return conn.execute(select(users.c.id).where(users.c.is_admin.is_(True)).limit(1)).first() is not None


def has_sealed_values(conn: Connection) -> bool:
    if conn.execute(select(secrets.c.id).limit(1)).first():
        return True
    return conn.execute(select(users.c.id).where(users.c.totp_secret_enc.is_not(None)).limit(1)).first() is not None


def create_admin(conn: Connection, keyring: Keyring, *, username: str, password: str,
                 actor_id: str | None = None) -> NewAdmin:
    if not USERNAME.fullmatch(username):
        raise ValueError("username must be 2-40 characters of a-z, 0-9, '.', '_', or '-', starting with a letter or digit")
    check_password_policy(password)
    if conn.execute(select(users.c.id).where(users.c.username == username)).first():
        raise ValueError(f"user {username!r} already exists")
    user_id = new_id()
    totp_secret = pyotp.random_base32()
    conn.execute(insert(users).values(
        id=user_id, username=username, display_name=username, password_hash=hash_password(password),
        totp_secret_enc=keyring.seal(totp_secret, totp_aad(user_id), label="totp"),
        last_totp_step=0, is_admin=True, disabled=False, must_change_password=False, created_at=utcnow(),
    ))
    audit.record(conn, action="user.create_admin", actor_id=actor_id or user_id,
                 target_kind="user", target_id=user_id, detail={"username": username})
    uri = pyotp.TOTP(totp_secret).provisioning_uri(name=username, issuer_name=TOTP_ISSUER)
    return NewAdmin(user_id=user_id, otpauth_uri=uri)

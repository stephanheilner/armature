"""Server settings, loaded from a YAML file with environment overrides.

The file is ``$ARMATURE_SERVER_CONFIG``, else ``~/.armature/server.yaml``.
Each top-level scalar can be overridden by ``ARMATURE_SERVER_<FIELD>``,
such as ``ARMATURE_SERVER_DATABASE_URL``.
"""
from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONFIG_ENV = "ARMATURE_SERVER_CONFIG"
DEFAULT_CONFIG_PATH = Path("~/.armature/server.yaml")
ENV_PREFIX = "ARMATURE_SERVER_"

ScopedRole = Literal["team_lead", "project_lead", "contributor", "viewer"]


class GroupGrant(BaseModel):
    """A role a group grants in one team, or one project named ``team/project``."""

    model_config = ConfigDict(extra="forbid")

    scope_kind: Literal["team", "project"]
    scope: str
    role: ScopedRole


class OIDCConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issuer: str
    client_id: str
    client_secret_ref: str
    scopes: list[str] = Field(default_factory=lambda: ["openid", "profile", "email"])
    groups_claim: str = "groups"
    admin_groups: list[str] = Field(default_factory=list)
    group_roles: dict[str, list[GroupGrant]] = Field(default_factory=dict)


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database_url: str = Field(default_factory=lambda: f"sqlite:///{Path('~/.armature/server.db').expanduser()}")
    public_origin: str = "http://127.0.0.1:8098"
    base_path: str = ""
    host: str = "127.0.0.1"
    port: int = Field(default=8098, ge=1, le=65535)
    allow_public_bind: bool = False
    # Plain-http sign-in on a private IPv4 origin, for a home or office network without TLS.
    allow_private_http: bool = False
    trust_real_ip: bool = False
    master_key: str = Field(default_factory=lambda: f"file:{Path('~/.armature/master.key').expanduser()}")
    oidc: OIDCConfig | None = None
    password_login: bool = True
    events_socket: str = "~/.armature/server-events.sock"
    lost_after_minutes: int = Field(default=30, ge=1)
    sse_per_user: int = Field(default=8, ge=1)
    sse_total: int = Field(default=500, ge=1)

    @field_validator("base_path")
    @classmethod
    def _base_path(cls, value: str) -> str:
        if value and (not value.startswith("/") or value.endswith("/")):
            raise ValueError("base_path must be empty or start with '/' and not end with '/'")
        return value

    @field_validator("master_key")
    @classmethod
    def _master_key(cls, value: str) -> str:
        scheme, _, rest = value.partition(":")
        if scheme not in ("env", "file", "kms") or not rest:
            raise ValueError("master_key must be env:<NAME>, file:<path>, or kms:<key-arn>")
        return value

    @model_validator(mode="after")
    def _loopback_unless_allowed(self) -> ServerConfig:
        if not self.allow_public_bind and not _is_loopback(self.host):
            raise ValueError(f"host {self.host!r} is not loopback; set allow_public_bind: true to bind it")
        return self


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def config_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get(CONFIG_ENV) or DEFAULT_CONFIG_PATH).expanduser()


def load_config(path: Path | str | None = None, env: Mapping[str, str] | None = None) -> ServerConfig:
    env = os.environ if env is None else env
    p = Path(path).expanduser() if path else config_path(env)
    data: dict[str, Any] = {}
    if p.exists():
        from ruamel.yaml import YAML
        loaded = YAML(typ="safe").load(p.read_text(encoding="utf-8"))
        if loaded is not None and not isinstance(loaded, dict):
            raise ValueError(f"{p} must hold a mapping")
        data = dict(loaded or {})
    for name in ServerConfig.model_fields:
        if name == "oidc":
            continue
        value = env.get(ENV_PREFIX + name.upper())
        if value is not None:
            data[name] = value
    return ServerConfig.model_validate(data)


def write_config(config: ServerConfig, path: Path) -> None:
    from ruamel.yaml import YAML
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        YAML(typ="safe", pure=True).dump(config.model_dump(exclude_none=True), out)

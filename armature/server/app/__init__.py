"""The Armature server's FastAPI app: a signed-in JSON API and the browser UI."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, FastAPI
from fastapi.responses import RedirectResponse
from sqlalchemy import Engine

from armature.server.app import routes_api, routes_auth, routes_oidc, static
from armature.server.app.deps import Server
from armature.server.app.http import BodyLimit, SecurityHeaders, install_error_handlers
from armature.server.auth import dummy_hash
from armature.server.config import ServerConfig
from armature.server.crypto import Keyring
from armature.server.db import utcnow
from armature.server.oidc import Provider

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app(cfg: ServerConfig, engine: Engine, keyring: Keyring, *, clock: Callable[[], datetime] = utcnow,
               static_dir: Path = STATIC_DIR, oidc_transport: httpx.BaseTransport | None = None,
               password_cost: int | None = None) -> FastAPI:
    app = FastAPI(title="Armature", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.server = Server(
        cfg=cfg, engine=engine, keyring=keyring, clock=clock, dummy_hash=dummy_hash(password_cost),
        static_dir=static_dir, provider=Provider(cfg.oidc, oidc_transport) if cfg.oidc else None,
        password_cost=password_cost,
    )
    install_error_handlers(app)
    base = cfg.base_path

    health = APIRouter()

    @health.get("/health")
    def get_health() -> dict[str, bool]:
        return {"ok": True}

    for router in (health, routes_auth.router, routes_api.router, routes_oidc.router, static.router):
        app.include_router(router, prefix=base)

    if base:
        @app.get(base, include_in_schema=False)
        def base_redirect() -> RedirectResponse:
            return RedirectResponse(f"{base}/", status_code=302)

    app.add_middleware(BodyLimit)
    app.add_middleware(SecurityHeaders, hsts=not app.state.server.insecure_localhost)
    return app

"""Response headers, body limits, client address, and JSON errors."""
from __future__ import annotations

import ipaddress
import re

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    # no-referrer makes a browser send "Origin: null" on our own POSTs, which the origin check refuses.
    (b"referrer-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
    (b"cache-control", b"no-store"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
]

BODY_LIMIT = 64 * 1024
LOOPBACK_PEERS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
REAL_IP = re.compile(r"^[0-9a-fA-F:.]{2,45}$")


class ApiError(Exception):
    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or {}


def error_response(status: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


HSTS = (b"strict-transport-security", b"max-age=31536000")


class SecurityHeaders:
    def __init__(self, app, hsts: bool = True):
        self.app = app
        self.headers = SECURITY_HEADERS + ([HSTS] if hsts else [])

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                names = {name for name, _ in self.headers}
                message["headers"] = [h for h in message.get("headers", []) if h[0].lower() not in names] \
                    + self.headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class BodyLimit:
    """Refuses a request body over the limit, declared or streamed."""

    def __init__(self, app, limit: int = BODY_LIMIT):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for name, value in scope.get("headers", []):
            if name == b"content-length" and value.isdigit() and int(value) > self.limit:
                return await error_response(413, "Request body too large.")(scope, receive, send)
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise StarletteHTTPException(413, "Request body too large.")
            return message

        await self.app(scope, limited_receive, send)


# X-Real-IP is read only when the config says a proxy on this host sets it, and only from a loopback peer.
# X-Forwarded-For is never read.
def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else ""
    real = request.headers.get("x-real-ip")
    trusted = request.app.state.server.cfg.trust_real_ip
    if trusted and peer in LOOPBACK_PEERS and real and REAL_IP.fullmatch(real):
        try:
            return str(ipaddress.ip_address(real))
        except ValueError:
            pass
    return peer


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError):
        return error_response(exc.status, exc.message, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException):
        message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return error_response(exc.status_code, message, getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _invalid(_request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ())[1:])
        return error_response(400, f"Invalid request: {where or 'body'} {first.get('msg', '')}".strip())

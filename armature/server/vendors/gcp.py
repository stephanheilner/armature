"""GCP connections: a service account's OAuth token, and BigQuery jobs.query, as the account's key signs for.

The key's own token_uri is never used, so a pasted key can't send its signed assertion elsewhere.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx

from armature.server.vendors import VendorError, Verified, call, client, json_of
from armature.server.vendors.github import load_key, rs256

TOKEN_URL = "https://oauth2.googleapis.com/token"
BIGQUERY = "https://bigquery.googleapis.com/bigquery/v2"
SCOPE = "https://www.googleapis.com/auth/bigquery.readonly"
GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
TIMEOUT = 60.0
QUERY_TIMEOUT_MS = 50_000
BYTES_BILLED_MAX = 10 * 1000 ** 3
ROWS_MAX = 1000
CLIENT_EMAIL = re.compile(r"^[a-z0-9-]{1,63}@[a-z0-9-]{1,63}\.iam\.gserviceaccount\.com$")
KEPT = ("type", "project_id", "private_key_id", "private_key", "client_email")


def service_account(text: str) -> str:
    """A service account key file, checked and cut down to the fields signing needs."""
    try:
        found = json.loads(text)
    except ValueError as exc:
        raise VendorError("service_account_json is not JSON.") from exc
    if not isinstance(found, dict) or found.get("type") != "service_account":
        raise VendorError("service_account_json is not a service account key.")
    if not isinstance(found.get("client_email"), str) or not CLIENT_EMAIL.fullmatch(found["client_email"]):
        raise VendorError("service_account_json has no service account client_email.")
    if not isinstance(found.get("private_key"), str):
        raise VendorError("service_account_json has no private_key.")
    load_key(found["private_key"])
    return json.dumps({k: found[k] for k in KEPT if isinstance(found.get(k), str)}, sort_keys=True)


def _account(creds: dict[str, str]) -> dict[str, Any]:
    try:
        found = json.loads(creds["service_account_json"])
    except (KeyError, ValueError) as exc:
        raise VendorError("The service account key can't be read.") from exc
    return found


def token(creds: dict[str, str], now: datetime, http: httpx.Client) -> str:
    account = _account(creds)
    stamp = int(now.timestamp())
    assertion = rs256(account["private_key"], {"iss": account["client_email"], "scope": SCOPE, "aud": TOKEN_URL,
                                               "iat": stamp - 60, "exp": stamp + 3000})
    what = "Google POST /token"
    made = json_of(call(http, "POST", TOKEN_URL, what, data={"grant_type": GRANT, "assertion": assertion}), what)
    found = made.get("access_token") if isinstance(made, dict) else None
    if not isinstance(found, str) or not found:
        raise VendorError(f"{what} gave no token.")
    return found


def verify(creds: dict[str, str], config: dict[str, Any], *, now: datetime,
           transport: httpx.BaseTransport | None = None) -> Verified:
    with client(transport, TIMEOUT) as http:
        token(creds, now, http)
    return Verified(identity={"client_email": _account(creds).get("client_email")})


def _value(value: Any, kind: str) -> Any:
    if value is None:
        return None
    if kind in ("INTEGER", "INT64"):
        return int(value)
    if kind == "TIMESTAMP":
        return datetime.fromtimestamp(int(value) / 1_000_000, UTC).isoformat()
    return str(value)


def query(creds: dict[str, str], config: dict[str, Any], sql: str, params: list[dict[str, Any]], *, now: datetime,
          transport: httpx.BaseTransport | None = None) -> list[dict[str, Any]]:
    """The rows of one standard SQL query with named parameters, as dicts of flat columns.

    A query that doesn't finish in time, or answers with more than one page, fails rather than answering in part.
    """
    what = "BigQuery POST /queries"
    body = {"query": sql, "useLegacySql": False, "parameterMode": "NAMED", "queryParameters": params,
            "maximumBytesBilled": str(BYTES_BILLED_MAX), "timeoutMs": QUERY_TIMEOUT_MS, "maxResults": ROWS_MAX,
            "formatOptions": {"useInt64Timestamp": True}}
    with client(transport, TIMEOUT) as http:
        bearer = token(creds, now, http)
        found = json_of(call(http, "POST", f"{BIGQUERY}/projects/{config['project_id']}/queries", what,
                             headers={"Authorization": f"Bearer {bearer}"}, json=body), what)
    if not isinstance(found, dict) or found.get("jobComplete") is not True:
        raise VendorError(f"{what} did not finish in time.")
    if found.get("pageToken"):
        raise VendorError(f"{what} answered with more than {ROWS_MAX} rows.")
    fields = ((found.get("schema") or {}).get("fields")) or []
    rows = found.get("rows") or []
    if not isinstance(fields, list) or not isinstance(rows, list):
        raise VendorError(f"{what} answered without a schema or rows.")
    names = [(f.get("name"), f.get("type")) for f in fields if isinstance(f, dict)]
    out = []
    try:
        for row in rows:
            cells = row.get("f") if isinstance(row, dict) else None
            if not isinstance(cells, list) or len(cells) != len(names):
                raise VendorError(f"{what} answered with a malformed row.")
            out.append({name: _value((cell or {}).get("v"), kind) for (name, kind), cell in zip(names, cells)})
    except (TypeError, ValueError, AttributeError) as exc:
        raise VendorError(f"{what} answered with a malformed value.") from exc
    return out

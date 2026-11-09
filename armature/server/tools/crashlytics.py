"""Crashlytics tools: the top fatal crash clusters from the project's BigQuery export, through its GCP connection.

The query reads only an allowlist of columns, so no crash message, log, breadcrumb, or custom key leaves BigQuery.
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from armature.server import sanitize
from armature.server.connections import BQ_NAME, GCP_PROJECT
from armature.server.tools import Call, ToolError
from armature.server.vendors import gcp

ISSUE_ID = re.compile(r"^[A-Za-z0-9]{1,40}$")
LABEL_PREFIX = "crashlytics-"
VERSIONS_MAX = 20
TEXT_MAX = 300

SQL = """\
WITH events AS (
  SELECT issue_id, application.display_version AS version, installation_uuid, event_timestamp,
         STRUCT(blame_frame.symbol AS symbol, blame_frame.file AS file, blame_frame.line AS line,
                blame_frame.library AS library) AS frame
  FROM `{table}`
  WHERE event_timestamp >= TIMESTAMP_SUB(@as_of, INTERVAL @days DAY) AND event_timestamp < @as_of
    AND error_type = 'FATAL'
),
clusters AS (
  SELECT issue_id, COUNT(DISTINCT installation_uuid) AS users, COUNT(*) AS events,
         MIN(event_timestamp) AS first_seen, MAX(event_timestamp) AS last_seen, ANY_VALUE(frame) AS frame
  FROM events
  GROUP BY issue_id
  HAVING users >= @min_users
  ORDER BY users DESC, issue_id
  LIMIT @top
),
versions AS (
  SELECT issue_id, version, COUNT(DISTINCT installation_uuid) AS version_users,
         MAX(event_timestamp) AS version_last_seen
  FROM events
  WHERE issue_id IN (SELECT issue_id FROM clusters)
  GROUP BY issue_id, version
  QUALIFY ROW_NUMBER() OVER (PARTITION BY issue_id ORDER BY version_users DESC, version) <= @versions
)
SELECT c.issue_id, c.users, c.events, c.first_seen, c.last_seen, c.frame.symbol AS symbol, c.frame.file AS file,
       c.frame.line AS line, c.frame.library AS library, v.version, v.version_users, v.version_last_seen
FROM clusters c JOIN versions v USING (issue_id)
ORDER BY c.users DESC, c.issue_id, v.version_users DESC, v.version
"""


def _table(config: dict[str, Any]) -> str:
    project, dataset, table = config.get("project_id"), config.get("dataset"), config.get("table")
    if not (isinstance(project, str) and GCP_PROJECT.fullmatch(project) and isinstance(dataset, str)
            and BQ_NAME.fullmatch(dataset) and isinstance(table, str) and BQ_NAME.fullmatch(table)):
        raise ToolError(409, "The GCP connection needs a project_id, dataset, and table.")
    return f"{project}.{dataset}.{table}"


def _param(name: str, kind: str, value: Any) -> dict[str, Any]:
    return {"name": name, "parameterType": {"type": kind}, "parameterValue": {"value": str(value)}}


def _text(value: Any) -> str:
    return " ".join(sanitize.plain(str(value), TEXT_MAX).split()) if value is not None else ""


def clusters(ctx: Call, params: dict[str, Any]) -> dict[str, Any]:
    """The top fatal clusters by distinct installations in the last ``days`` days, each with its crashing versions."""
    days, min_users, top = int(params.get("days") or 7), int(params.get("min_users") or 10), int(params.get("top") or 20)
    as_of = ctx.now.replace(microsecond=0)
    sql = SQL.format(table=_table(ctx.config))
    rows = gcp.query(ctx.creds, ctx.config, sql,
                     [_param("as_of", "TIMESTAMP", as_of.isoformat()), _param("days", "INT64", days),
                      _param("min_users", "INT64", min_users), _param("top", "INT64", top),
                      _param("versions", "INT64", VERSIONS_MAX)],
                     now=ctx.now, transport=ctx.transport)
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        issue_id = row.get("issue_id")
        if not isinstance(issue_id, str) or not ISSUE_ID.fullmatch(issue_id):
            continue
        cluster = found.get(issue_id)
        if cluster is None:
            frame = {"symbol": _text(row.get("symbol")), "file": _text(row.get("file")), "line": row.get("line"),
                     "library": _text(row.get("library"))}
            cluster = found[issue_id] = {
                "issue_id": issue_id, "label": f"{LABEL_PREFIX}{issue_id}",
                "title": frame["symbol"] or frame["file"] or issue_id, "frame": frame,
                "users": row.get("users"), "events": row.get("events"), "first_seen": row.get("first_seen"),
                "last_seen": row.get("last_seen"), "versions": []}
        cluster["versions"].append({"version": _text(row.get("version")) or "unknown",
                                    "users": row.get("version_users"), "last_seen": row.get("version_last_seen")})
    return {"as_of": as_of.isoformat(), "since": (as_of - timedelta(days=days)).isoformat(), "days": days,
            "min_users": min_users, "clusters": list(found.values()),
            "labels": [c["label"] for c in found.values()]}

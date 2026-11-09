"""crash-watch's decisions, as pure functions a tool_call stage runs: which fixes to check, and what to file.

They read only their arguments, so the same inputs always give the same issues to file. The runner imports this
module for the workflow's tools, so it imports nothing of the server.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from armature.registry.registry import PermissionLevel, ToolDescriptor

GRACE = timedelta(days=21)
CAP = 2
SUMMARY_MAX = 200
VERSIONS_SHOWN = 10
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _when(value: Any) -> datetime | None:
    try:
        found = datetime.fromisoformat(value) if isinstance(value, str) and value else None
    except ValueError:
        return None
    return found if found is not None and found.tzinfo is not None else None


def _list(value: Any, what: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{what} must be a list of objects.")
    return value


def _tracking(issues: list[dict[str, Any]], label: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The open issues carrying ``label``, and the one resolved last."""
    mine = [i for i in issues if label in (i.get("labels") or [])]
    open_ = [i for i in mine if i.get("status_category") != "done"]
    resolved = [i for i in mine if i.get("status_category") == "done"]
    latest = max(resolved, key=lambda i: (_when(i.get("resolved_at")) or EPOCH, i.get("issue") or "")) \
        if resolved else None
    return open_, latest


def _fix_pr(issue: dict[str, Any], repo: str) -> int | None:
    found = [p.get("number") for p in issue.get("merged_prs") or []
             if isinstance(p, dict) and str(p.get("repo") or "").lower() == repo.lower()]
    return next((n for n in reversed(found) if isinstance(n, int)), None)


def _versions(cluster: dict[str, Any]) -> list[str]:
    return [v["version"] for v in cluster.get("versions") or [] if isinstance(v, dict)
            and isinstance(v.get("version"), str) and v["version"] != "unknown"]


def checks(clusters: Any, issues: Any, repo: str) -> list[dict[str, Any]]:
    """For each cluster whose label is only on resolved issues, and whose last one is Fixed: the fix to look for."""
    clusters, issues = _list(clusters, "clusters"), _list(issues, "issues")
    out = []
    for cluster in clusters:
        open_, latest = _tracking(issues, cluster.get("label") or "")
        if open_ or latest is None or latest.get("resolution") != "Fixed":
            continue
        out.append({"issue": latest.get("issue"), "pr": _fix_pr(latest, repo), "versions": _versions(cluster)})
    return out


def _regressed(cluster: dict[str, Any], shipped: dict[str, Any]) -> dict[str, Any] | None:
    """The first crashing version whose tag holds the fix and that crashed after the tag was made."""
    tags = {v.get("version"): v for v in shipped.get("versions") or [] if isinstance(v, dict)}
    for version in cluster.get("versions") or []:
        tag = tags.get(version.get("version")) if isinstance(version, dict) else None
        made, seen = _when((tag or {}).get("tag_date")), _when(version.get("last_seen") if tag else None)
        if tag and tag.get("contained") is True and made and seen and seen > made:
            return {**tag, "last_seen": version.get("last_seen")}
    return None


def _decide(cluster: dict[str, Any], issues: list[dict[str, Any]], shipped: dict[str, dict[str, Any]],
            as_of: datetime) -> dict[str, Any]:
    base = {"issue_id": cluster.get("issue_id"), "label": cluster.get("label"), "users": cluster.get("users")}
    open_, latest = _tracking(issues, cluster.get("label") or "")
    if open_:
        return {**base, "decision": "skip", "reason": f"{open_[0].get('issue')} is open", "issue": open_[0].get("issue")}
    if latest is None:
        return {**base, "decision": "new", "reason": "no issue tracks it"}
    key = latest.get("issue")
    if latest.get("resolution") != "Fixed":
        return {**base, "decision": "ignore", "reason": f"{key} was resolved {latest.get('resolution')}", "issue": key}
    fix = shipped.get(key) or {}
    if fix.get("merged") is not True:
        return {**base, "decision": "ignore", "reason": f"{key} has no merged pull request", "issue": key}
    tag = _regressed(cluster, fix)
    if tag is not None:
        return {**base, "decision": "regression", "issue": key, "fix": fix, "tag": tag,
                "reason": f"{tag['tag']} holds the fix for {key} and crashed after it"}
    resolved = _when(latest.get("resolved_at"))
    if resolved is not None and as_of - resolved < GRACE:
        return {**base, "decision": "wait", "reason": f"{key}'s fix has not shipped yet", "issue": key}
    return {**base, "decision": "ignore", "reason": f"{key}'s fix never shipped to a crashing version", "issue": key}


def _description(cluster: dict[str, Any], decision: dict[str, Any], since: str, as_of: str) -> str:
    frame = cluster.get("frame") or {}
    where = f"{frame.get('file') or 'unknown file'}:{frame.get('line') or '?'}"
    lines = [(f"Crashlytics issue {cluster.get('issue_id')} crashed the app for {cluster.get('users')} users "
              f"({cluster.get('events')} crashes) from {since} to {as_of}."),
             "",
             "Blamed frame",
             f"{frame.get('symbol') or 'unknown symbol'}",
             f"{where}" + (f" in {frame['library']}" if frame.get("library") else ""),
             "",
             "Crashing versions"]
    lines += [f"{v.get('version')}: {v.get('users')} users, last seen {v.get('last_seen')}"
              for v in (cluster.get("versions") or [])[:VERSIONS_SHOWN] if isinstance(v, dict)]
    lines += ["", f"First seen {cluster.get('first_seen')}, last seen {cluster.get('last_seen')} in this window."]
    if decision["decision"] == "regression":
        fix, tag = decision["fix"], decision["tag"]
        lines += ["", (f"Regression of {decision['issue']}: it was resolved Fixed by {fix.get('repo')} pull request "
                       f"{fix.get('pr')}, which {tag['tag']} holds, and this crash happened on {tag['version']} at "
                       f"{tag['last_seen']}, after that tag.")]
    lines += ["", f"Filed by crash-watch from the Crashlytics BigQuery export. Label {cluster.get('label')}."]
    return "\n".join(lines)


def triage(clusters: Any, issues: Any, shipped: Any, as_of: str, since: str = "") -> dict[str, Any]:
    """Every cluster's decision, and at most ``CAP`` new or regressed ones to file, most users first."""
    clusters, issues = _list(clusters, "clusters"), _list(issues, "issues")
    now = _when(as_of)
    if now is None:
        raise ValueError("as_of must be an ISO time with a zone.")
    fixes = {s["issue"]: s for s in (shipped or []) if isinstance(s, dict) and isinstance(s.get("issue"), str)}
    ranked = sorted(clusters, key=lambda c: (-(c.get("users") if isinstance(c.get("users"), int) else 0),
                                              str(c.get("issue_id"))))
    decisions = [_decide(c, issues, fixes, now) for c in ranked]
    actions = []
    for cluster, decision in zip(ranked, decisions):
        if decision["decision"] not in ("new", "regression") or len(actions) >= CAP:
            continue
        regression = decision["decision"] == "regression"
        title = " ".join(str(cluster.get("title") or cluster.get("issue_id")).split())
        actions.append({"kind": decision["decision"], "issue_id": cluster.get("issue_id"),
                        "labels": [cluster.get("label")], "link": decision["issue"] if regression else "",
                        "summary": f"{'Crash regression' if regression else 'Crash'}: {title}"[:SUMMARY_MAX],
                        "description": _description(cluster, decision, since, as_of)})
    shown = [{k: v for k, v in d.items() if k not in ("fix", "tag")} for d in decisions]
    return {"as_of": as_of, "actions": actions, "count": len(actions), "decisions": shown}


def register(registry: Any) -> None:
    async def run_checks(args: dict[str, Any]) -> dict[str, Any]:
        return {"checks": checks(args.get("clusters"), args.get("issues"), str(args.get("repo") or ""))}

    async def run_triage(args: dict[str, Any]) -> dict[str, Any]:
        return triage(args.get("clusters"), args.get("issues"), args.get("shipped"), str(args.get("as_of") or ""),
                      str(args.get("since") or ""))

    registry.register(ToolDescriptor(name="crash_watch_checks", permission=PermissionLevel.READ_ONLY,
                                     handler=run_checks, description="The fixes crash-watch checks for release."))
    registry.register(ToolDescriptor(name="crash_watch_triage", permission=PermissionLevel.READ_ONLY,
                                     handler=run_triage, description="The crashes crash-watch files."))

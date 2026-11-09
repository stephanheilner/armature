"""Jira tools: read an issue, search the project's Jira project by label, create an issue there, comment, and move
an issue to a status with ops jira_transition.sh's guards, on REST v3."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from armature.server import sanitize
from armature.server.tools import Call, ToolError
from armature.server.vendors import VendorError, call, client, json_of
from armature.server.vendors import jira as vendor

KEY = re.compile(r"[A-Z][A-Z0-9]+-[0-9]+")
PROJECT_KEY = "jira.project"
COMPONENTS_KEY = "jira.components"
BOARD_KEY = "jira.board"
CRASH_LABEL = re.compile(r"^crashlytics-[A-Za-z0-9]{1,40}$")
SEARCH_FIELDS = "summary,status,resolution,resolutiondate,labels,created,comment"
SEARCH_MAX = 100
MERGED = re.compile(r"\bMerged ([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#([0-9]+)\b")
LINK_TYPE = "Relates"
SUMMARY_MAX = 255
ISSUE_FIELDS = "summary,description,status,issuetype,components,labels,comment"
COMMENTS_MAX = 50
# The description and each comment, so one long issue can't crowd the code out of a session's context.
BODY_MAX = 4000


def _lines(block: str) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for index, line in enumerate(block.split("\n")):
        if index:
            content.append({"type": "hardBreak"})
        if line:
            content.append({"type": "text", "text": line})
    return content


def adf(text: str) -> dict[str, Any]:
    """Plain text as Atlassian Document Format, so nothing in it is markup.

    Each blank-line block is a paragraph, and each line break in it a hard break, so lines stay apart in Jira.
    """
    blocks = [b.strip("\n") for b in re.split(r"\n\s*\n", sanitize.plain(text).replace("\r\n", "\n"))
              if b.strip()] or ["(empty)"]
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": _lines(b)} for b in blocks]}


class Jira:
    def __init__(self, ctx: Call):
        self.site = ctx.config["site_url"]
        self._auth = (ctx.config["email"], ctx.creds["api_token"])
        self._http = client(ctx.transport)

    def __enter__(self) -> Jira:
        return self

    def __exit__(self, *exc: Any) -> None:
        self._http.close()

    def request(self, method: str, path: str, agile: bool = False, **kwargs: Any) -> httpx.Response:
        what = f"Jira {method} {re.sub(KEY, '{key}', path.split('?')[0])}"
        base = "/rest/agile/1.0" if agile else "/rest/api/3"
        return call(self._http, method, f"{self.site}{base}{path}", what, auth=self._auth,
                    headers={"Accept": "application/json"}, **kwargs)

    def json(self, method: str, path: str, agile: bool = False, **kwargs: Any) -> Any:
        response = self.request(method, path, agile, **kwargs)
        return json_of(response, f"Jira {method} {re.sub(KEY, '{key}', path.split('?')[0])}") \
            if response.status_code != 204 else {}


def text_of(doc: Any) -> str:
    """Atlassian Document Format as plain text: paragraphs apart by a blank line, a hard break as a newline."""
    if not isinstance(doc, dict):
        return doc if isinstance(doc, str) else ""
    if doc.get("type") == "text":
        return str(doc.get("text") or "")
    if doc.get("type") == "hardBreak":
        return "\n"
    parts = [text_of(child) for child in doc.get("content") or []]
    return ("\n\n" if doc.get("type") == "doc" else "").join(parts)


def _capped(text: str) -> str:
    return text[:BODY_MAX] + "\n[truncated]" if len(text) > BODY_MAX else text


def get_issue(ctx: Call, params: dict[str, Any]) -> dict[str, Any]:
    issue = params["issue"]
    with Jira(ctx) as jira:
        response = jira.request("GET", f"/issue/{quote(issue)}", params={"fields": ISSUE_FIELDS})
        if response.status_code in (403, 404):
            return {"issue": issue, "found": False}
        fields = json_of(response, "Jira GET /issue/{key}").get("fields") or {}
    comments = ((fields.get("comment") or {}).get("comments") or [])[-COMMENTS_MAX:]
    return {"issue": issue, "found": True, "summary": fields.get("summary") or "",
            "description": _capped(text_of(fields.get("description"))),
            "status": (fields.get("status") or {}).get("name") or "",
            "type": (fields.get("issuetype") or {}).get("name") or "",
            "components": [c.get("name") for c in fields.get("components") or [] if isinstance(c, dict)],
            "labels": [x for x in fields.get("labels") or [] if isinstance(x, str)],
            "comments": [{"author": (c.get("author") or {}).get("displayName") or "", "body": _capped(text_of(c.get("body")))}
                         for c in comments if isinstance(c, dict)],
            "url": f"{ctx.config['site_url']}/browse/{issue}"}


def _project(ctx: Call) -> str:
    project = ctx.policy.get("project")
    if not isinstance(project, str) or not re.fullmatch(r"[A-Z][A-Z0-9]+", project):
        raise ToolError(409, f"Set the {PROJECT_KEY} setting to this project's Jira project key first.")
    return project


def _issue(site_url: str, issue: dict[str, Any], account: Any) -> dict[str, Any]:
    """One search hit. Only the connection's own comments, which pr-merged writes, can name a merged fix."""
    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    key = issue.get("key")
    if not isinstance(key, str) or not KEY.fullmatch(key):
        raise VendorError("Jira GET /rest/api/3/search/jql answered with an issue without a key.")
    status = fields.get("status") if isinstance(fields.get("status"), dict) else {}
    resolution = fields.get("resolution") if isinstance(fields.get("resolution"), dict) else {}
    comments = (fields.get("comment") or {}).get("comments") if isinstance(fields.get("comment"), dict) else []
    merged = []
    own = [c for c in comments or [] if isinstance(c, dict) and isinstance(c.get("author"), dict)
           and account and c["author"].get("accountId") == account]
    for found in (MERGED.finditer(text_of(c.get("body"))) for c in own):
        merged += [{"repo": m.group(1), "number": int(m.group(2))} for m in found]
    return {"issue": key, "summary": fields.get("summary") or "", "status": status.get("name") or "",
            "status_category": (status.get("statusCategory") or {}).get("key") or "",
            "resolution": resolution.get("name"),
            "resolved_at": fields.get("resolutiondate"), "created_at": fields.get("created"),
            "labels": [x for x in fields.get("labels") or [] if isinstance(x, str)], "merged_prs": merged,
            "url": f"{site_url}/browse/{key}"}


def crash_labels(params: dict[str, Any]) -> list[str]:
    """The crashlytics labels in a step's params, which make a jira.create a crash issue."""
    return [x for x in params.get("labels") or [] if isinstance(x, str) and CRASH_LABEL.fullmatch(x)]


def _by_labels(ctx: Call, project: str, labels: list[str]) -> list[dict[str, Any]]:
    if not labels:
        return []
    quoted = ", ".join(f'"{x}"' for x in labels)
    jql = f"project = {project} AND labels in ({quoted}) ORDER BY created DESC"
    found = vendor.issues(ctx.creds, ctx.config, jql, limit=SEARCH_MAX, fields=SEARCH_FIELDS,
                          transport=ctx.transport)
    return [_issue(ctx.config["site_url"], issue, ctx.identity.get("account_id")) for issue in found]


def search(ctx: Call, params: dict[str, Any]) -> dict[str, Any]:
    """The issues in the project's own Jira project that carry any of ``labels``. The project is never a param."""
    labels = list(params.get("labels") or [])
    if crash_labels(params) != labels:
        raise ToolError(400, "Each label must be crashlytics- and an issue id.")
    issues = _by_labels(ctx, _project(ctx), labels)
    return {"issues": issues, "count": len(issues)}


def _active_sprint(jira: Jira, board: int) -> int:
    found = jira.json("GET", f"/board/{board}/sprint", agile=True, params={"state": "active"})
    sprints = [s for s in (found.get("values") or []) if isinstance(s, dict)] if isinstance(found, dict) else None
    if sprints is None:
        raise VendorError("Jira GET /board/{id}/sprint answered without sprints.")
    if len(sprints) != 1 or not isinstance(sprints[0].get("id"), int):
        raise ToolError(409, f"Board {board} has {len(sprints)} active sprints, not one.")
    return sprints[0]["id"]


def create(ctx: Call, params: dict[str, Any]) -> dict[str, Any]:
    """An issue in the project's own Jira project, from an allowlist of fields. The project key is never a param.

    A crash issue, one with a crashlytics label, files nothing when an open issue already has the label, so a rerun
    never files the same crash twice. Only a crash issue gets the components and the board's active sprint that
    project settings name.
    """
    project = _project(ctx)
    crash = crash_labels(params)
    open_ = [i["issue"] for i in _by_labels(ctx, project, crash) if i["status_category"] != "done"]
    if open_:
        return {"issue": None, "created": False, "reason": f"{', '.join(open_)} already tracks this crash"}
    fields: dict[str, Any] = {"project": {"key": project}, "issuetype": {"name": params["type"]},
                              "summary": " ".join(sanitize.plain(params["summary"]).split())[:SUMMARY_MAX]}
    if params.get("description"):
        fields["description"] = adf(params["description"])
    if params.get("labels"):
        fields["labels"] = params["labels"]
    components = ctx.policy.get("components") if crash else None
    if components:
        fields["components"] = [{"name": name} for name in components]
    if params.get("assign_to_me"):
        account = ctx.identity.get("account_id")
        if not isinstance(account, str) or not account:
            raise ToolError(409, "The Jira connection has no account. Verify it first.")
        fields["assignee"] = {"accountId": account}
    body: dict[str, Any] = {"fields": fields}
    link = params.get("link")
    if link:
        if link.rsplit("-", 1)[0] != project:
            raise ToolError(403, f"A link goes only to an issue in {project}.")
        body["update"] = {"issuelinks": [{"add": {"type": {"name": LINK_TYPE}, "outwardIssue": {"key": link}}}]}
    board = ctx.policy.get("board") if crash else None
    with Jira(ctx) as jira:
        sprint = _active_sprint(jira, board) if board else None
        made = jira.json("POST", "/issue", json=body)
        key = made.get("key") if isinstance(made, dict) else None
        if not isinstance(key, str) or not KEY.fullmatch(key):
            raise VendorError("Jira POST /issue answered without an issue key.")
        if sprint is not None:
            try:
                jira.json("POST", f"/sprint/{sprint}/issue", agile=True, json={"issues": [key]})
            except VendorError as exc:
                raise VendorError(f"Created {key}, but moving it to sprint {sprint} failed: {exc}") from exc
    return {"issue": key, "created": True, "url": f"{ctx.config['site_url']}/browse/{key}",
            **({"sprint": sprint} if sprint is not None else {})}


def comment(ctx: Call, params: dict[str, Any]) -> dict[str, Any]:
    issue = params["issue"]
    with Jira(ctx) as jira:
        made = jira.json("POST", f"/issue/{quote(issue)}/comment", json={"body": adf(params["body"])})
    return {"issue": issue, "commented": True, "comment_id": made.get("id")}


def _key(params: dict[str, Any]) -> str | None:
    if params.get("issue"):
        return params["issue"]
    for text in params.get("key_from") or []:
        found = KEY.search(text or "")
        if found:
            return found.group(0)
    return None


def transition(ctx: Call, params: dict[str, Any]) -> dict[str, Any]:
    issue = _key(params)
    if issue is None:
        # Plenty of pull requests name no ticket, so this is data for the workflow, not a failure.
        return {"issue": None, "transitioned": False, "reason": "no issue key found"}
    projects = params.get("projects") or []
    if projects and issue.rsplit("-", 1)[0] not in projects:
        return {"issue": issue, "transitioned": False, "reason": "project not in projects"}
    with Jira(ctx) as jira:
        response = jira.request("GET", f"/issue/{quote(issue)}", params={"fields": "status,issuetype"})
        if response.status_code in (403, 404):
            return {"issue": issue, "transitioned": False, "reason": "issue not readable"}
        fields = (json_of(response, "Jira GET /issue/{key}").get("fields") or {})
        issue_type = str((fields.get("issuetype") or {}).get("name") or "")
        current = str((fields.get("status") or {}).get("name") or "")
        defect = issue_type == "Defect" and bool(params.get("defect_to"))
        target = params["defect_to"] if defect else params["to"]
        resolution = params.get("defect_resolution") if defect else None
        result = {"issue": issue, "from": current, "to": target, "issue_type": issue_type}
        if current.lower() == target.lower():
            return {**result, "transitioned": False, "reason": "already in target status"}
        # A status past the from guard means a person or an earlier delivery already moved it.
        if params.get("from") and current.lower() != params["from"].lower():
            return {**result, "transitioned": False, "reason": f"source status is {current}, not {params['from']}"}
        found = jira.json("GET", f"/issue/{quote(issue)}/transitions").get("transitions") or []
        move = next((t for t in found if isinstance(t, dict) and target.lower() in
                     (str(t.get("name") or "").lower(), str((t.get("to") or {}).get("name") or "").lower())), None)
        if move is None:
            return {**result, "transitioned": False, "reason": "transition unavailable"}
        body: dict[str, Any] = {"transition": {"id": str(move.get("id"))}}
        if resolution:
            body["fields"] = {"resolution": {"name": resolution}}
        moved = jira.request("POST", f"/issue/{quote(issue)}/transitions", json=body)
        if moved.status_code in (400, 409):
            return {**result, "transitioned": False, "reason": "transition unavailable"}
        if moved.status_code >= 300:
            raise VendorError(f"Jira POST /issue/{{key}}/transitions answered HTTP {moved.status_code}.")
        comment_error = None
        if params.get("comment"):
            try:
                jira.json("POST", f"/issue/{quote(issue)}/comment", json={"body": adf(params["comment"])})
            except VendorError as exc:
                comment_error = str(exc)
    return {**result, "transitioned": True, "url": f"{ctx.config['site_url']}/browse/{issue}",
            **({"comment_error": comment_error} if comment_error else {})}

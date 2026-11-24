"""Subjects: every run about one pull request or issue, grouped into a list row and a history page."""
from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, String, and_, cast, func, or_, select, tuple_

from armature.server import actions, display, rbac
from armature.server.config import ActionConfig
from armature.server.db.schema import (
    connections,
    findings,
    gh_pull_requests,
    gh_repos,
    jobs,
    run_artifacts,
    runner_keys,
    runs,
    trigger_seen,
    trigger_state,
    triggers,
    webhook_deliveries,
    workflows,
)
from armature.server.ingest import HOST_RUN, RUN_KIND
from armature.server.pollers import CONNECTION_KIND, DEFAULT_INTERVAL
from armature.server.threads import ISSUE_KEY, PULL_SUBJECT, PULL_URL, links

RUNS_MAX = 500
FINDINGS_MAX = 2000
SEARCH_BATCHES = 10
HEAD_LENGTH = 12
POSTED_LABEL = "Posted result"
LOGIN_MAX = 100
DONE_STATES = ("merged", "closed")

WORKFLOW = func.coalesce(workflows.c.name, runs.c.workflow_name)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _scalars(value: Any) -> dict[str, Any] | None:
    """Only the plain values of a reported object, so the page never renders nested data."""
    if not isinstance(value, dict):
        return None
    return {k: v for k, v in value.items() if isinstance(k, str) and (v is None or isinstance(v, str | int | float | bool))}


def _cost(value: Any) -> float | None:
    return float(value) if value is not None else None


def parse_cursor(before: str | None) -> tuple[datetime, str] | None:
    if not before:
        return None
    stamp, _, subject = before.partition("|")
    at = datetime.fromisoformat(stamp)
    return (at if at.tzinfo else at.replace(tzinfo=UTC)), subject


def make_cursor(at: datetime, subject: str) -> str:
    # Z rather than +00:00, because a bare + in a query string reads as a space.
    return f"{at.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')}|{subject}"


def _run_query(scope: rbac.Scope):
    return (select(runs.c.id, runs.c.subject, runs.c.team_id, runs.c.project_id, runs.c.workflow_id,
                   WORKFLOW.label("workflow"), RUN_KIND.label("kind"),
                   runs.c.status, runs.c.started_at, runs.c.finished_at, runs.c.cost_usd, runs.c.summary,
                   runs.c.trigger_ref, runs.c.outcome, runs.c.risk, runs.c.risk_reasons, runs.c.location,
                   HOST_RUN.label("host_run"))
            .select_from(runs.outerjoin(workflows, workflows.c.id == runs.c.workflow_id)
                         .outerjoin(runner_keys, runner_keys.c.id == runs.c.runner_key_id))
            .where(rbac.scope_clause(scope, runs.c.team_id, runs.c.project_id)))


def _repo_number(subject: str, ordered: list[Any]) -> tuple[str | None, int | None]:
    for run in reversed(ordered):
        ref = _dict(run.trigger_ref)
        if _text(ref.get("repo")):
            number = ref.get("number")
            return ref["repo"], number if isinstance(number, int) and not isinstance(number, bool) else None
    match = PULL_SUBJECT.fullmatch(subject)
    return (match["repo"], int(match["number"])) if match else (None, None)


def status_of(ordered: list[Any]) -> str:
    """Merged once a merge run exists, else the latest review verdict, else the latest run's status."""
    if any(run.kind == "merge" for run in ordered):
        return "merged"
    for run in reversed(ordered):
        verdict = _text(_dict(run.summary).get("verdict"))
        if run.kind == "review" and verdict:
            return verdict
    return ordered[-1].status if ordered else "unknown"


def _latest(ordered: list[Any], field: str) -> str | None:
    for run in reversed(ordered):
        value = _text(_dict(run.summary).get(field))
        if value:
            return value
    return None


def _stored_states(conn: Connection, keys: set[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """Each team and subject's merged or closed state as that team's GitHub sync stored it."""
    wanted: dict[tuple[str, str, int], set[str]] = {}
    for team_id, name in keys:
        repo, _, number = name.rpartition("#")
        wanted.setdefault((team_id, repo.lower(), int(number)), set()).add(name)
    if not wanted:
        return {}
    rows = conn.execute(select(gh_pull_requests.c.team_id, gh_repos.c.full_name, gh_pull_requests.c.number,
                               gh_pull_requests.c.state)
                        .join(gh_repos, gh_repos.c.id == gh_pull_requests.c.repo_row_id)
                        .where(gh_repos.c.team_id == gh_pull_requests.c.team_id,
                               gh_pull_requests.c.team_id.in_({team for team, _, _ in wanted}),
                               gh_pull_requests.c.number.in_({number for _, _, number in wanted}),
                               gh_pull_requests.c.state.in_(DONE_STATES)))
    states: dict[tuple[str, str], str] = {}
    for row in rows:
        for name in wanted.get((row.team_id, row.full_name.lower(), row.number), ()):
            if states.get((row.team_id, name)) != "merged":
                states[(row.team_id, name)] = row.state
    return states


def pull_states(conn: Connection, scope: rbac.Scope,
                listed: set[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Each listed subject and team's pull request title, latest review verdict, and merged or closed state.

    Title, verdict, and merged come from the subject's visible runs. state is merged once one of them is a merge run,
    else what the listed team's GitHub sync stored, merged or closed, else open, as pull_done filters.
    """
    keys = {(subject, team_id) for subject, team_id in listed if PULL_SUBJECT.fullmatch(subject)}
    if not keys:
        return {}
    pulls = sorted({subject for subject, _ in keys})
    by_subject: dict[str, list[Any]] = {name: [] for name in pulls}
    query = _run_query(scope).where(runs.c.subject.in_(pulls)).order_by(runs.c.started_at, runs.c.id)
    for run in conn.execute(query).all():
        by_subject[run.subject].append(run)
    stored = _stored_states(conn, {(team_id, subject) for subject, team_id in keys})
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for subject, team_id in keys:
        ordered = by_subject[subject]
        if not ordered:
            continue
        merged = any(run.kind == "merge" for run in ordered)
        found[(subject, team_id)] = {
            "title": _latest(ordered, "title"),
            "verdict": _latest([run for run in ordered if run.kind == "review"], "verdict"),
            "merged": merged, "state": "merged" if merged else stored.get((team_id, subject), "open")}
    return found


def pull_done(scope: rbac.Scope, subject: Any, team_id: Any) -> Any:
    """A filter for rows whose subject pull_states calls merged or closed, keyed on the row's own team."""
    merges = runs.alias("merges")
    merge_kind = workflows.alias("merge_kind")
    merged = (select(merges.c.subject).join(merge_kind, merge_kind.c.id == merges.c.workflow_id)
              .where(merges.c.subject.is_not(None), merge_kind.c.kind == "merge",
                     rbac.scope_clause(scope, merges.c.team_id, merges.c.project_id)))
    stored = (select(gh_pull_requests.c.team_id,
                     func.lower(gh_repos.c.full_name, type_=String) + "#" + cast(gh_pull_requests.c.number, String))
              .join(gh_repos, gh_repos.c.id == gh_pull_requests.c.repo_row_id)
              .where(gh_repos.c.team_id == gh_pull_requests.c.team_id, gh_pull_requests.c.state.in_(DONE_STATES)))
    keyed = tuple_(team_id, func.lower(subject, type_=String))
    return and_(subject.is_not(None), or_(subject.in_(merged), keyed.in_(stored)))


OPEN_RUNS = ("queued", "running", "waiting")


def open_jobs(conn: Connection, listed: list[Any]) -> dict[str, dict[str, Any]]:
    """The newest job of each open run, with what deciding on a Cancel needs: its id, status, and cancel request."""
    ids = [run.id for run in listed if run.status in OPEN_RUNS]
    found: dict[str, dict[str, Any]] = {}
    if ids:
        for job in conn.execute(select(jobs.c.run_id, jobs.c.id, jobs.c.status, jobs.c.cancel_requested_at)
                                .where(jobs.c.run_id.in_(ids)).order_by(jobs.c.created_at.desc(), jobs.c.id)):
            found.setdefault(job.run_id, {"id": job.id, "status": job.status,
                                          "cancel_requested_at": job.cancel_requested_at})
    return found


def _author(payload: str | None) -> str | None:
    """The login that opened the pull request or issue a GitHub delivery is about."""
    try:
        data = json.loads(payload) if payload else None
    except ValueError:
        return None
    for key in ("pull_request", "issue"):
        login = _text(_dict(_dict(_dict(data).get(key)).get("user")).get("login"))
        if login:
            return login[:LOGIN_MAX]
    return None


def authors(conn: Connection, by_subject: dict[str, list[Any]]) -> dict[str, str | None]:
    """Each subject's author, from the stored payload of its newest run's webhook delivery. None when none is kept."""
    newest = {}
    for subject, ordered in by_subject.items():
        newest[subject] = next((ref["delivery_id"] for run in reversed(ordered)
                                if isinstance((ref := _dict(run.trigger_ref)).get("delivery_id"), str)), None)
    wanted = {delivery for delivery in newest.values() if delivery}
    payloads = dict(conn.execute(select(webhook_deliveries.c.id, webhook_deliveries.c.payload)
                                 .where(webhook_deliveries.c.id.in_(wanted))).all()) if wanted else {}
    return {subject: _author(payloads.get(delivery)) if delivery else None for subject, delivery in newest.items()}


def summarize(subject: str, ordered: list[Any], hidden: set[str], show_cost: bool,
              author: str | None = None) -> dict[str, Any]:
    """One list row. Hidden workflows stay out of the counts and cost, never out of the status."""
    counted = [run for run in ordered if run.workflow not in hidden]
    repo, _ = _repo_number(subject, ordered)
    cost = sum(_cost(run.cost_usd) or 0.0 for run in counted)
    return {
        "subject": subject,
        "title": _latest(ordered, "title"),
        "latest_verdict": _latest(ordered, "verdict"),
        "status": status_of(ordered),
        "runs": len(counted),
        "reviews": sum(1 for run in counted if run.kind == "review"),
        "fixes": sum(1 for run in counted if run.kind == "fix"),
        "cost_usd": round(cost, 6) if show_cost else None,
        "last_activity": ordered[-1].started_at if ordered else None,
        "repo": repo,
        "team_id": ordered[-1].team_id if ordered else None,
        "author": author,
        "location": ordered[-1].location if ordered else None,
    }


def _grouped(conn: Connection, scope: rbac.Scope, team: str | None, q: str,
             cursor: tuple[datetime, str] | None, limit: int) -> list[Any]:
    last = func.max(runs.c.started_at)
    query = (select(runs.c.subject, last.label("last"))
             .where(runs.c.subject.is_not(None), rbac.scope_clause(scope, runs.c.team_id, runs.c.project_id))
             .group_by(runs.c.subject))
    if team:
        query = query.where(runs.c.team_id == team)
    if q:
        # A coarse match on the stored summary. The title check on the grouped row decides.
        query = query.where(or_(func.lower(runs.c.subject).contains(q, autoescape=True),
                                func.lower(cast(runs.c.summary, String)).contains(q, autoescape=True)))
    if cursor is not None:
        at, subject = cursor
        query = query.having(or_(last < at, and_(last == at, runs.c.subject < subject)))
    return conn.execute(query.order_by(last.desc(), runs.c.subject.desc()).limit(limit)).all()


def list_subjects(conn: Connection, scope: rbac.Scope, team: str | None, q: str, before: str | None,
                  limit: int) -> dict[str, Any]:
    settings = display.load(conn)
    hidden = set(display.hidden(settings))
    wanted = q.strip().lower()
    cursor = parse_cursor(before)
    rows: list[dict[str, Any]] = []
    more = False
    for _ in range(SEARCH_BATCHES):
        groups = _grouped(conn, scope, team, wanted, cursor, limit)
        more = len(groups) == limit
        if not groups:
            break
        names = [g.subject for g in groups]
        by_subject: dict[str, list[Any]] = {name: [] for name in names}
        query = _run_query(scope).where(runs.c.subject.in_(names))
        if team:
            query = query.where(runs.c.team_id == team)
        for run in conn.execute(query.order_by(runs.c.started_at, runs.c.id)).all():
            by_subject[run.subject].append(run)
        author_of = authors(conn, by_subject)
        for index, group in enumerate(groups):
            cursor = (group.last, group.subject)
            row = summarize(group.subject, by_subject[group.subject], hidden, settings["show_cost"],
                            author_of[group.subject])
            if wanted and wanted not in group.subject.lower() and wanted not in (row["title"] or "").lower():
                continue
            rows.append(row)
            if len(rows) == limit:
                more = more or index < len(groups) - 1
                break
        if len(rows) == limit or not more:
            break
    return {"subjects": rows, "next_before": make_cursor(*cursor) if more and cursor is not None else None}


def merged_links(ordered: list[Any]) -> list[dict[str, str]]:
    """Every run's links, latest first. A url already taken is skipped, and so is a label, except on a GitHub pull
    request link, so an issue with an Android and an iOS pull request links to both."""
    shown: list[dict[str, str]] = []
    urls: set[str] = set()
    labels: set[str] = set()
    for run in reversed(ordered):
        summary = _dict(run.summary)
        candidates = links(run.subject, summary)
        if _text(summary.get("post_url")):
            candidates = [*candidates, {"label": POSTED_LABEL, "url": summary["post_url"]}]
        for link in candidates:
            label, url = _text(_dict(link).get("label")), _text(_dict(link).get("url"))
            if not label or not url or not url.startswith("https://") or url in urls:
                continue
            if label in labels and not PULL_URL.fullmatch(url):
                continue
            urls.add(url)
            labels.add(label)
            shown.append({"label": label, "url": url})
    return shown


def first_reason(reasons: Any) -> str | None:
    first = reasons[0] if isinstance(reasons, list) and reasons else None
    return _text(_dict(first).get("text"))


def finding_history(rows: list[Any]) -> list[dict[str, Any]]:
    """One entry per finding key, its dispositions in run order, newest finding first."""
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = grouped.setdefault(row.finding_key, {"finding_key": row.finding_key, "title": None, "path": None,
                                                     "line": None, "severity": None, "history": []})
        for field in ("title", "path", "line", "severity"):
            value = getattr(row, field)
            if value is not None and value != "":
                entry[field] = value
        entry["history"].append({"run_id": row.run_id, "started_at": row.started_at,
                                 "disposition": row.disposition})
    for entry in grouped.values():
        entry["current_disposition"] = entry["history"][-1]["disposition"]
    return sorted(grouped.values(), key=lambda e: (e["history"][-1]["started_at"], e["finding_key"]), reverse=True)


def watching(conn: Connection, scope: rbac.Scope, subject: str, ordered: list[Any],
             labels: dict[str, Any]) -> list[dict[str, Any]]:
    """The issue pollers in the projects of the thread's visible runs that may still pick the issue up: their
    workflow hasn't run in the thread and they haven't seen the key. Only when, never how, so anyone who sees the
    runs may see them, and no query or connection detail is in them."""
    projects = {run.project_id for run in ordered if run.project_id}
    if not ISSUE_KEY.fullmatch(subject) or not projects:
        return []
    ran = {run.workflow_id for run in ordered if run.workflow_id} | {run.workflow for run in ordered if run.workflow}
    rows = conn.execute(
        select(triggers.c.id, triggers.c.team_id, triggers.c.kind, triggers.c.config, workflows.c.name,
               triggers.c.workflow_id, workflows.c.project_id, trigger_state.c.last_run_at, trigger_state.c.next_run_at)
        .select_from(triggers.join(workflows, workflows.c.id == triggers.c.workflow_id)
                     .outerjoin(trigger_state, trigger_state.c.trigger_id == triggers.c.id))
        .where(triggers.c.kind.in_(CONNECTION_KIND), triggers.c.enabled.is_(True),
               triggers.c.team_id.in_({run.team_id for run in ordered}))
        .order_by(workflows.c.name, triggers.c.id)).all()
    rows = [row for row in rows if row.workflow_id not in ran and row.name not in ran
            and not _dict(row.config).get("shadow") and not _dict(row.config).get("resident")]
    owners = {row.connection_id: row.project_id for row in conn.execute(
        select(connections.c.id.label("connection_id"), connections.c.project_id)
        .where(connections.c.id.in_(sorted({_dict(row.config).get("connection_id") for row in rows} - {None}))))}
    seen = set(conn.execute(select(trigger_seen.c.trigger_id).where(
        trigger_seen.c.trigger_id.in_([row.id for row in rows]), trigger_seen.c.item_key == subject)).scalars())
    shown = []
    for row in rows:
        config = _dict(row.config)
        project = config.get("project_id") or owners.get(config.get("connection_id")) or row.project_id
        if row.id in seen or project not in projects or not rbac.in_scope(scope, row.team_id, project):
            continue
        shown.append({"workflow": row.name,
                      "workflow_label": _text(_dict(labels.get(row.name)).get("label")) or row.name,
                      "source": CONNECTION_KIND[row.kind],
                      "interval_minutes": config.get("interval_minutes") or DEFAULT_INTERVAL,
                      "last_poll_at": row.last_run_at, "next_poll_at": row.next_run_at})
    return shown


def subject_detail(conn: Connection, scope: rbac.Scope, subject: str,
                   configured: Sequence[ActionConfig] = ()) -> dict[str, Any] | None:
    settings = display.load(conn)
    labels = settings["workflow_labels"]
    # A thread's page lists every run in it, so an issue's page shows the runs on its pull requests too.
    # The newest RUNS_MAX, so a long thread never hides its latest result.
    ordered = conn.execute(_run_query(scope).where(or_(runs.c.subject == subject, runs.c.thread == subject))
                           .order_by(runs.c.started_at.desc(), runs.c.id.desc()).limit(RUNS_MAX)).all()[::-1]
    if not ordered:
        return None
    # The page's own state comes from its own subject's runs, so a merged pull request never marks its issue merged.
    own = ([run for run in ordered if run.subject == subject]
           or [run for run in ordered if run.kind != "merge"] or ordered)
    run_ids = [run.id for run in ordered]
    artifacts: dict[str, list[str]] = {}
    for row in conn.execute(select(run_artifacts.c.run_id, run_artifacts.c.name)
                            .where(run_artifacts.c.run_id.in_(run_ids)).order_by(run_artifacts.c.name)).all():
        artifacts.setdefault(row.run_id, []).append(row.name)
    finding_rows = conn.execute(
        select(findings.c.finding_key, findings.c.title, findings.c.path, findings.c.line, findings.c.severity,
               findings.c.disposition, findings.c.run_id, runs.c.started_at)
        .select_from(findings.join(runs, runs.c.id == findings.c.run_id))
        .where(findings.c.run_id.in_(run_ids))
        .order_by(runs.c.started_at, findings.c.run_id, findings.c.id).limit(FINDINGS_MAX)).all()
    repo, number = _repo_number(subject, own)
    show_cost = settings["show_cost"]
    job_of = open_jobs(conn, ordered)

    def row(run: Any) -> dict[str, Any]:
        summary, ref = _dict(run.summary), _dict(run.trigger_ref)
        head = _text(ref.get("head"))
        return {
            "id": run.id, "subject": run.subject, "workflow": run.workflow,
            "workflow_label": _text(_dict(labels.get(run.workflow)).get("label")) or run.workflow,
            "kind": run.kind, "status": run.status, "started_at": run.started_at,
            "finished_at": run.finished_at, "cost_usd": _cost(run.cost_usd) if show_cost else None,
            "title": _text(summary.get("title")), "verdict": _text(summary.get("verdict")),
            "mode": _text(ref.get("mode")), "head": head[:HEAD_LENGTH] if head else None,
            "fields": _scalars(summary.get("fields")), "fix": _scalars(ref.get("fix")),
            "merge": _scalars(ref.get("merge")), "artifacts": artifacts.get(run.id, []),
            "outcome": run.outcome, "risk": run.risk, "risk_reason": first_reason(run.risk_reasons),
            "team_id": run.team_id, "project_id": run.project_id, "job": job_of.get(run.id),
            "host_cancel": actions.host_cancel(configured, run._mapping),
        }

    return {
        "subject": subject, "title": _latest(own, "title"), "status": status_of(own),
        "latest_verdict": _latest(own, "verdict"),
        "latest_outcome": next((run.outcome for run in reversed(own) if run.outcome), None),
        "risk": own[-1].risk, "repo": repo, "number": number,
        "links": merged_links(ordered), "runs": [row(run) for run in ordered],
        "findings": finding_history(finding_rows), "watching": watching(conn, scope, subject, ordered, labels),
    }

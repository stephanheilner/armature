"""Copy past runs from the armature-ops dashboard and ``traces.db`` into the server (`armature server import-history`).

Each dashboard run becomes a ``runs`` row with its steps, findings, and a
synthesized event timeline, so the run page reads as it did on the
dashboard. Sources are only ever read. The report holds counts, never a
payload, review, or finding.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Connection, Engine, delete, insert, select, update

from armature.server import audit
from armature.server.crypto import Keyring
from armature.server.db import new_id
from armature.server.db.schema import (
    findings,
    projects,
    run_events,
    run_steps,
    runs,
    settings,
    teams,
    users,
    workflows,
)
from armature.server.defs import resolve_workflow
from armature.server.ingest import redact, team_secret_values
from armature.server.writer import step_key

ACTOR = "import-history"
IMPORT_VERSION = 1
ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "armature-ops/dashboard/run")
RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,36}$")
FINDING_ID = re.compile(r"^[0-9a-f]{12}$")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
TEXT_CAP = 8192
ERROR_CAP = 4000
TITLE_CAP = 500
RUNS_PER_TRANSACTION = 100
ROWS_PER_INSERT = 1000
JIRA_TEAM = "mobile"
UNASSIGNED_SLUG = "unassigned"
UNASSIGNED_NAME = "Unassigned (imported)"
ISSUE_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9]+)-[0-9]+")
REVIEW_STAGES = ("reviews", "consults")
LEAD_STAGES = ("decide", "team_lead")

STATUS = {"done": "succeeded", "done_with_errors": "failed", "died": "lost"}
TRIGGER_KINDS = {"pr-role-review": "pr", "pr-review": "pr", "pr-fix": "fix", "repo-review": "daily",
                 "resynthesize": "manual", "jira-work": "jira", "jira-work-resume": "jira", "ios-port": "port",
                 "pr-merged": "merged", "slack-intake": "intake", "build-announce": "ci"}
KEPT_DECISIONS = frozenset({"block", "suggest", "act", "defer"})
DISPOSITIONS = frozenset({"keep", "drop_rebutted", "resolved"})


@dataclass
class SourceRun:
    """One past run and every row the dashboard keeps for it. ``source`` is ``dashboard`` or ``traces``."""

    run: dict[str, Any]
    stages: list[dict[str, Any]] = field(default_factory=list)
    reviews: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    comments: list[dict[str, Any]] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    jira: dict[str, Any] | None = None
    fix: dict[str, Any] | None = None
    merge: dict[str, Any] | None = None
    pr_author: str | None = None
    cancel: dict[str, Any] | None = None
    source: str = "dashboard"


class HistorySource(Protocol):
    def dashboard_ids(self, since: datetime | None) -> list[str]: ...

    def load_dashboard(self, ids: list[str]) -> list[SourceRun]: ...

    def unlinked(self) -> dict[str, int]: ...

    def trace_runs(self, since: datetime | None) -> list[tuple[str, str]]: ...

    def load_traces(self, ids: list[str]) -> list[SourceRun]: ...


@dataclass
class Report:
    counts: dict[str, Counter] = field(default_factory=dict)

    def add(self, section: str, outcome: str, n: int = 1) -> None:
        self.counts.setdefault(section, Counter())[outcome] += n

    def lines(self) -> list[str]:
        return [f"{section}: " + ", ".join(f"{n} {outcome}" for outcome, n in sorted(tally.items()))
                for section, tally in self.counts.items()]


@dataclass
class Plan:
    run_id: str
    run: dict[str, Any]
    steps: list[dict[str, Any]]
    events: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    rule: str


@dataclass
class _Target:
    """What the server already holds that a run maps onto."""

    conn: Connection
    keyring: Keyring
    now: datetime
    by_slug: dict[str, str]
    projects: dict[str, tuple[str, str]]
    users: dict[str, str]
    platform_repos: dict[str, str]
    tracker_repos: dict[str, str]
    jira_projects: set[str]
    intake_repos: list[str]
    secret_values: dict[str, tuple[str, ...]] = field(default_factory=dict)
    resolved: dict[tuple[str, str, str | None], str | None] = field(default_factory=dict)

    @classmethod
    def load(cls, conn: Connection, keyring: Keyring, now: datetime) -> _Target:
        team_rows = conn.execute(select(teams.c.id, teams.c.slug)).all()
        project_map = {}
        for row in conn.execute(select(projects.c.id, projects.c.team_id, projects.c.repo)).all():
            if row.repo:
                project_map.setdefault(row.repo.lower(), (row.team_id, row.id))
        stored = {row.key: row.value for row in conn.execute(select(settings.c.key, settings.c.value).where(
            settings.c.key.in_(("ops.jira-work", "ops.linear-intake", "env.JIRA_PROJECT")))).all()}
        platforms = _mapping(_mapping(stored.get("ops.jira-work")).get("platforms"))
        channels = _mapping(_mapping(stored.get("ops.linear-intake")).get("channels"))
        jira_project = stored.get("env.JIRA_PROJECT")
        return cls(conn=conn, keyring=keyring, now=now, by_slug={row.slug: row.id for row in team_rows if row.slug},
                   projects=project_map,
                   users={row.username.lower(): row.id
                          for row in conn.execute(select(users.c.id, users.c.username)).all() if row.username},
                   platform_repos={name: entry["repo"] for name, entry in platforms.items()
                                   if isinstance(entry, dict) and isinstance(entry.get("repo"), str)},
                   tracker_repos={str(entry["team"]).upper(): entry["repo"] for entry in channels.values()
                                  if isinstance(entry, dict) and isinstance(entry.get("repo"), str)
                                  and isinstance(entry.get("team"), str)},
                   jira_projects={key.upper() for key in re.split(r"[\s,]+", jira_project)
                                  if key} if isinstance(jira_project, str) else set(),
                   intake_repos=[entry["repo"] for entry in channels.values()
                                 if isinstance(entry, dict) and isinstance(entry.get("repo"), str)])

    def _team_of_repos(self, repos: Iterable[Any]) -> tuple[str, str | None] | None:
        named = [self.projects[r.lower()] for r in repos if isinstance(r, str) and r.lower() in self.projects]
        if not named or len({team for team, _ in named}) != 1:
            return None
        return named[0][0], named[0][1] if len({project for _, project in named}) == 1 else None

    def workflow_id(self, name: str, team_id: str, project_id: str | None) -> str | None:
        key = (name, team_id, project_id)
        if key not in self.resolved:
            row = resolve_workflow(self.conn, name, team_id, project_id)
            self.resolved[key] = row.id if row is not None else None
        return self.resolved[key]

    def team_for(self, src: SourceRun) -> tuple[str, str | None, str]:
        """The run's team and project, and the rule that chose them. With no evidence, the admin-only team."""
        run = src.run
        jira = src.jira or {}
        rules = (
            ("repo", [run.get("repo")]),
            ("target repos", REPO.findall(str(run.get("target") or ""))),
            ("jira-work platform", [self.platform_repos.get(str(jira.get("platform") or ""))]),
        )
        for rule, repos in rules:
            found = self._team_of_repos(repos)
            if found:
                return found[0], found[1], rule
        for key in (run.get("jira_key"), jira.get("issue"), run.get("target")):
            prefix = ISSUE_KEY.match(str(key or ""))
            if not prefix:
                continue
            project = prefix.group(1).upper()
            found = self._team_of_repos([self.tracker_repos.get(project)])
            if found:
                return found[0], None, "tracker team key"
            if project in self.jira_projects and JIRA_TEAM in self.by_slug:
                return self.by_slug[JIRA_TEAM], None, "jira project"
        if run.get("workflow") == "slack-intake":
            found = self._team_of_repos(self.intake_repos)
            if found:
                return found[0], None, "intake channel"
        return self._unassigned(), None, "unassigned"

    def _unassigned(self) -> str:
        if UNASSIGNED_SLUG not in self.by_slug:
            row = self.conn.execute(select(teams.c.id).where(teams.c.slug == UNASSIGNED_SLUG)).first()
            if row is None:
                team_id = new_id()
                self.conn.execute(insert(teams).values(id=team_id, name=UNASSIGNED_NAME, slug=UNASSIGNED_SLUG,
                                                       created_at=self.now))
                audit.record(self.conn, action="team.create", actor_id=ACTOR, team_id=team_id, target_kind="team",
                             target_id=team_id, detail={"source": ACTOR, "slug": UNASSIGNED_SLUG})
                self.by_slug[UNASSIGNED_SLUG] = team_id
            else:
                self.by_slug[UNASSIGNED_SLUG] = row.id
        return self.by_slug[UNASSIGNED_SLUG]

    def secrets_of(self, team_id: str) -> tuple[str, ...]:
        """The values live ingest redacts for this team, longest first. Refuses when any will not open."""
        if team_id not in self.secret_values:
            try:
                values = team_secret_values(self.conn, self.keyring, team_id)
            except Exception as exc:
                raise ValueError(f"the secrets of a team could not be opened ({exc.__class__.__name__}), "
                                 "so no text can be redacted") from exc
            self.secret_values[team_id] = tuple(sorted(set(values), key=len, reverse=True))
        return self.secret_values[team_id]


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def server_run_id(source_id: str) -> str:
    return source_id if RUN_ID.fullmatch(source_id) else str(uuid.uuid5(ID_NAMESPACE, source_id))


def _aware(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _money(value: float) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(repr(value)).quantize(Decimal("0.000001"))
    except InvalidOperation:
        return None


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _text(value: Any) -> str | None:
    if isinstance(value, dict) and value.get("__truncated"):
        value = value.get("raw")
    return value if isinstance(value, str) and value.strip() else None


def _first_line(value: str | None) -> str | None:
    if not value:
        return None
    line = next((part.strip() for part in value.splitlines() if part.strip()), "")
    return line[:TITLE_CAP] or None


def _text_payload(text: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"text": text[:TEXT_CAP], "imported": True}
    if len(text) > TEXT_CAP:
        payload["truncated"] = True
    return payload


def _normalized(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def finding_key(path: str | None, claim: str | None) -> str:
    """``findingKey`` from the dashboard's ``lib/derive.js``, so a finding keeps the key its reviews used."""
    return hashlib.sha1(f"{path or ''}\n{_normalized(claim)}".encode()).hexdigest()[:12]


def _lead_summary(stage: dict[str, Any]) -> str | None:
    summary = _text(stage.get("summary"))
    if summary:
        return summary
    content = _text(stage.get("content"))
    if not content:
        return None
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", content)
    try:
        parsed = json.loads(fenced.group(1) if fenced else content)
    except ValueError:
        return content
    return _text(parsed.get("summary")) if isinstance(parsed, dict) else None


def map_status(src: SourceRun) -> str:
    if src.cancel:
        return "canceled"
    if src.run.get("ended_at") is None:
        return "lost"
    return STATUS.get(str(src.run.get("state") or ""), "lost")


def trigger_kind(run: dict[str, Any]) -> str:
    if run.get("workflow") == "pr-role-review" and run.get("mode") == "discuss":
        return "discuss"
    return TRIGGER_KINDS.get(run.get("workflow"), "import")


def actor_of(src: SourceRun) -> str | None:
    run = src.run
    candidates: list[Any] = []
    if run.get("workflow") == "pr-merged":
        candidates.append((src.merge or {}).get("merged_by"))
    elif run.get("workflow") in ("pr-role-review", "pr-review", "pr-fix", "ios-port"):
        candidates.extend([(src.fix or {}).get("requester"), run.get("author"), src.pr_author])
    actor = next((c for c in candidates if isinstance(c, str) and c.strip()), None)
    return actor.strip()[:191] if actor else None


@dataclass
class _Step:
    step_id: str
    parent: str | None
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    cost: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None
    model: str | None = None
    output: dict[str, Any] | None = None
    text: str | None = None


def _stage_start(stage: dict[str, Any]) -> datetime | None:
    started = _aware(stage.get("started_at"))
    if started:
        return started
    completed = _aware(stage.get("completed_at"))
    return completed - timedelta(milliseconds=_number(stage.get("latency_ms"))) if completed else None


def _stage_error(stage: dict[str, Any]) -> str:
    kind = " ".join(str(part) for part in (stage.get("error_type"), stage.get("error_kind")) if part)
    return kind or "failed"


def _unique(step_id: str, taken: set[str]) -> str:
    candidate, n = step_id, 2
    while candidate in taken:
        candidate, n = f"{step_id}-{n}", n + 1
    taken.add(candidate)
    return candidate


def _review_steps(parent: str, reviews: list[dict[str, Any]], fallback: _Step) -> list[_Step]:
    children, taken = [], set()
    for review in reviews:
        finished = _aware(review.get("completed_at")) or fallback.finished_at
        duration = _number(review.get("duration_ms"))
        started = finished - timedelta(milliseconds=duration) if finished and duration else fallback.started_at
        failed = bool(review.get("is_error")) or bool(review.get("fan_out_error"))
        error = (_text(review.get("fan_out_error")) or ("the review reported an error" if failed else None))
        meta = {k: review[k] for k in ("repo", "platform", "model") if isinstance(review.get(k), str) and review[k]}
        children.append(_Step(
            step_id=_unique(str(review.get("role") or f"branch-{review.get('idx', len(children))}"), taken),
            parent=parent, status="failed" if failed else "succeeded", started_at=started, finished_at=finished,
            cost=_number(review.get("cost_usd")), error=error[:ERROR_CAP] if error else None,
            model=meta.get("model"), output=meta or None, text=_text(review.get("review_text"))))
    return children


def build_steps(src: SourceRun) -> list[_Step]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for stage in src.stages:
        grouped.setdefault(str(stage.get("stage_id")), []).append(stage)
    reviews: dict[str, list[dict[str, Any]]] = {}
    for review in src.reviews:
        reviews.setdefault(str(review.get("stage_id")), []).append(review)
    steps: list[_Step] = []
    for stage_id, rows in grouped.items():
        starts = [s for s in (_stage_start(row) for row in rows) if s]
        ends = [e for e in (_aware(row.get("completed_at")) for row in rows) if e]
        failed = [row for row in rows if not row.get("success", True)]
        models = [row.get("model") for row in rows if row.get("role_type") != "script" and row.get("model")]
        step = _Step(step_id=stage_id, parent=None, status="failed" if failed else "succeeded",
                     started_at=min(starts) if starts else None, finished_at=max(ends) if ends else None,
                     cost=sum(_number(row.get("cost_usd") or row.get("total_cost_usd")) for row in rows),
                     input_tokens=sum(_int(row.get("tokens_in")) for row in rows),
                     output_tokens=sum(_int(row.get("tokens_out")) for row in rows),
                     error=_stage_error(failed[0])[:ERROR_CAP] if failed else None,
                     model=str(models[0]) if models else None)
        steps.append(step)
        if stage_id in REVIEW_STAGES and reviews.get(stage_id):
            # The branches carry the cost, so the parent holds none and a sum over steps counts it once.
            step.cost = 0.0
            steps.extend(_review_steps(stage_id, reviews[stage_id], step))
    return steps


def _lead_step(src: SourceRun) -> str:
    present = {str(stage.get("stage_id")) for stage in src.stages}
    return next((name for name in LEAD_STAGES if name in present), "team_lead")


def build_findings(src: SourceRun, report: Report) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    by_id = {d["finding_id"]: d for d in src.decisions if isinstance(d.get("finding_id"), str)}
    matched: set[str] = set()
    for comment in src.comments:
        fid = comment.get("finding_id") if FINDING_ID.fullmatch(str(comment.get("finding_id") or "")) else None
        decision = by_id.get(fid) if fid else None
        if fid:
            matched.add(fid)
        body = _text(comment.get("body"))
        claim = _text(decision.get("claim")) if decision else None
        line = comment.get("line")
        out.append({"finding_key": fid or finding_key(comment.get("path"), body),
                    "path": comment.get("path"), "line": line if isinstance(line, int) and 0 <= line < 2 ** 31 else None,
                    "severity": _short(comment.get("severity"), 16),
                    "disposition": _disposition(decision), "title": (claim or _first_line(body) or "Finding")[:TITLE_CAP],
                    "body": body})
    for decision in src.decisions:
        fid = decision.get("finding_id") if FINDING_ID.fullmatch(str(decision.get("finding_id") or "")) else None
        if fid and fid in matched:
            continue
        if decision.get("decision") not in KEPT_DECISIONS and decision.get("disposition") not in DISPOSITIONS:
            report.add("findings", "decisions set aside, not imported")
            continue
        claim = _text(decision.get("claim"))
        out.append({"finding_key": fid or finding_key(None, claim), "path": None, "line": None, "severity": None,
                    "disposition": _disposition(decision), "title": (_first_line(claim) or "Finding")[:TITLE_CAP],
                    "body": _text(decision.get("reason"))})
    for item in src.items:
        if item.get("kind") == "change":
            action = _text(item.get("action"))
            out.append({"finding_key": finding_key(item.get("file"), action), "path": item.get("file"), "line": None,
                        "severity": _short(item.get("severity"), 16),
                        "disposition": "change", "title": _first_line(action) or "Change", "body": action})
        elif item.get("kind") == "conflict":
            topic = _text(item.get("topic"))
            out.append({"finding_key": finding_key(item.get("file"), topic), "path": item.get("file"), "line": None,
                        "severity": None, "disposition": "conflict", "title": _first_line(topic) or "Conflict",
                        "body": _text(item.get("resolution"))})
        else:
            report.add("findings", "no-action items, not imported")
    return out


def _short(value: Any, cap: int) -> str | None:
    return str(value)[:cap] if value else None


def _disposition(decision: dict[str, Any] | None) -> str | None:
    if not decision:
        return None
    return _short(decision.get("disposition") or decision.get("decision"), 32)


def _side_texts(src: SourceRun) -> list[str]:
    texts = []
    if src.jira:
        j = src.jira
        head = " ".join(str(p) for p in ("Jira work", j.get("issue"), f"({j['platform']})" if j.get("platform") else None,
                                         f"outcome {j['outcome']}" if j.get("outcome") else None) if p)
        texts.append("\n".join(p for p in (head, _text(j.get("pr_url")), _text(j.get("detail"))) if p))
    if src.fix:
        f = src.fix
        head = " ".join(str(p) for p in ("Fix", f"requested by {f['requester']}" if f.get("requester") else None,
                                         f"({f['scope']})" if f.get("scope") else None,
                                         f"outcome {f['outcome']}" if f.get("outcome") else None,
                                         f"applied {f['applied']}" if f.get("applied") is not None else None,
                                         f"skipped {f['skipped']}" if f.get("skipped") is not None else None,
                                         f"commit {f['commit']}" if f.get("commit") else None) if p)
        texts.append("\n".join(p for p in (head, _text(f.get("detail"))) if p))
    if src.merge:
        m = src.merge
        texts.append(" ".join(str(p) for p in (
            "Merged", f"by {m['merged_by']}" if m.get("merged_by") else None,
            f"and moved {m['issue']} to {m['moved_to']}" if m.get("issue") and m.get("moved_to") else None) if p))
    return texts


def _trigger_ref(src: SourceRun) -> dict[str, Any]:
    run = src.run
    ref: dict[str, Any] = {"repo": run.get("repo"), "number": run.get("number"),
                           "mode": run.get("mode") or (src.fix or {}).get("scope"), "jira_key": run.get("jira_key"),
                           "head": run.get("head"), "target": run.get("target"), "source": src.source}
    if not RUN_ID.fullmatch(str(run["run_id"])):
        ref["source_run_id"] = run["run_id"]
    if src.jira:
        ref["jira"] = {k: src.jira.get(k) for k in ("issue", "platform", "outcome", "pr_url")}
    if src.fix:
        ref["fix"] = {k: src.fix.get(k) for k in ("requester", "scope", "outcome", "applied", "skipped", "commit")}
    if src.merge:
        ref["merge"] = {k: src.merge.get(k) for k in ("merged_by", "merged_at", "issue", "moved_to", "transitioned")}
    if src.cancel and src.cancel.get("requested_by"):
        ref["cancelled_by"] = src.cancel["requested_by"]
    return _compact(ref)


def _compact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _compact(v) for k, v in value.items() if v is not None and v != {}}
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        return value[:500]
    return value


class _Timeline:
    def __init__(self) -> None:
        self.events: list[tuple[datetime, int, str, str | None, dict[str, Any]]] = []

    def add(self, ts: datetime, kind: str, step_id: str | None, payload: dict[str, Any]) -> None:
        self.events.append((ts, len(self.events), kind, step_id, {**payload, "imported": True}))

    def ordered(self) -> list[tuple[datetime, str, str | None, dict[str, Any]]]:
        return [(ts, kind, step_id, payload) for ts, _, kind, step_id, payload in sorted(self.events)]


def _redacted(src: SourceRun, secret_values: tuple[str, ...]) -> SourceRun:
    """Every text the run carries, scrubbed as live ingest scrubs event text. The run id stays as it is."""
    names = ("run", "stages", "reviews", "decisions", "comments", "items", "jira", "fix", "merge", "pr_author",
             "cancel")
    copy = replace(src, **{name: redact(getattr(src, name), secret_values) for name in names})
    copy.run["run_id"] = src.run["run_id"]
    return copy


def plan_run(src: SourceRun, target: _Target, report: Report) -> Plan | None:
    started = _aware(src.run.get("started_at"))
    if started is None:
        report.add("runs", "skipped, no start time")
        return None
    run_id = server_run_id(str(src.run["run_id"]))
    team_id, project_id, rule = target.team_for(src)
    src = _redacted(src, target.secrets_of(team_id))
    run = src.run
    status = map_status(src)
    steps = build_steps(src)
    found = build_findings(src, report)
    workflow_name = str(run.get("workflow") or "unknown")[:191]
    workflow_id = target.workflow_id(workflow_name, team_id, project_id)
    actor = actor_of(src)
    stage_ends = [s.finished_at for s in steps if s.finished_at]
    last_seen = max([t for t in (_aware(run.get("last_event_at")), *stage_ends) if t], default=started)
    finished = None
    if status == "canceled":
        finished = _aware(src.cancel.get("ended_at")) or _aware(run.get("ended_at")) or last_seen
    else:
        finished = _aware(run.get("ended_at")) or last_seen
    failed = [s.step_id for s in steps if s.status == "failed" and s.parent is None]
    error = None
    if status == "failed":
        error = f"{len(failed)} step(s) failed: {', '.join(failed)}" if failed else "Finished with errors"
    elif status == "lost":
        error = "Stopped without finishing" if run.get("state") == "died" else "Imported while still running"
    cost = _number(run.get("cost_usd"))
    summary = {k: run.get(k) for k in ("title", "verdict", "post_url", "post_event") if run.get(k)}
    lead_stage = next((stage for name in LEAD_STAGES for stage in src.stages if stage.get("stage_id") == name), {})
    values = {
        "team_id": team_id, "project_id": project_id, "workflow_id": workflow_id, "workflow_name": workflow_name,
        "status": status, "triggered_by": target.users.get(actor.lower()) if actor else None,
        "trigger_kind": trigger_kind(run), "actor": actor,
        "cancelled_by": target.users.get(str((src.cancel or {}).get("requested_by") or "").lower()),
        "started_at": started, "finished_at": finished, "cost_usd": _money(cost),
        "input_tokens": _int(run.get("tokens_in")), "output_tokens": _int(run.get("tokens_out")),
        "summary": _compact(summary) or None, "trigger_ref": _trigger_ref(src), "error": error,
        "last_event_at": last_seen,
    }

    timeline = _Timeline()
    lead = _lead_step(src)
    for step in steps:
        begin = step.started_at or step.finished_at or started
        end = step.finished_at or begin
        where = {"parent_step": step.parent} if step.parent else {}
        timeline.add(begin, "step_start", step.step_id, where)
        if step.cost or step.input_tokens or step.output_tokens:
            cost_payload = {"cost_usd": round(step.cost, 6), "input_tokens": step.input_tokens,
                            "output_tokens": step.output_tokens, **where}
            if step.model:
                cost_payload["model"] = step.model
            timeline.add(end, "cost", step.step_id, cost_payload)
        if step.text:
            timeline.add(end, "text", step.step_id, {**_text_payload(step.text), **where})
        if step.parent is None and step.step_id == lead:
            verdict = run.get("verdict") or _text(lead_stage.get("verdict"))
            lead_text = "\n\n".join(p for p in (f"Verdict: {verdict}" if verdict else None,
                                                _lead_summary(lead_stage)) if p)
            if lead_text:
                timeline.add(end, "text", step.step_id, _text_payload(lead_text))
            for finding in found:
                timeline.add(end, "finding", step.step_id, _finding_payload(finding))
        if step.status == "failed":
            timeline.add(end, "step_failed", step.step_id, {"error": step.error, **where})
        else:
            timeline.add(end, "step_end", step.step_id, {"status": step.status, **where})
    if found and lead not in {s.step_id for s in steps if s.parent is None}:
        for finding in found:
            timeline.add(last_seen, "finding", None, _finding_payload(finding))
    for text in _side_texts(src):
        timeline.add(finished or last_seen, "text", None, _text_payload(text))

    first = [(started, "run_start", None, {"workflow": workflow_name, "trigger_kind": values["trigger_kind"],
                                            "imported": True})]
    end_ts = finished or last_seen
    last = [(end_ts, "run_end", None, _compact(
        {"status": status, "cost_usd": round(cost, 6), "error": error, "imported": True}))]
    event_rows = [{"run_id": run_id, "seq": seq, "ts": ts, "kind": kind, "step_id": step_id, "payload": payload}
                  for seq, (ts, kind, step_id, payload) in enumerate(first + timeline.ordered() + last, start=1)]
    step_rows = [{"run_id": run_id, "step_key": step_key(s.step_id, s.parent), "step_id": s.step_id,
                  "parent_step": s.parent, "status": s.status, "started_at": s.started_at, "finished_at": s.finished_at,
                  "cost_usd": _money(s.cost), "input_tokens": s.input_tokens, "output_tokens": s.output_tokens,
                  "error": s.error, "output": s.output} for s in steps]
    created_at = next((s.finished_at for s in steps if s.parent is None and s.step_id == lead), None) or end_ts
    finding_rows = [{"run_id": run_id, "project_id": project_id, "step_id": lead, "created_at": created_at, **f}
                    for f in found]
    values["trigger_ref"]["import_hash"] = _hash(values, step_rows, event_rows, finding_rows)
    return Plan(run_id=run_id, run=values, steps=step_rows, events=event_rows, findings=finding_rows, rule=rule)


def _finding_payload(finding: dict[str, Any]) -> dict[str, Any]:
    return {k: finding[k] for k in ("title", "path", "line", "severity", "disposition", "finding_key")
            if finding.get(k) is not None}


def _hash(*parts: Any) -> str:
    blob = json.dumps([IMPORT_VERSION, *parts], sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _chunks(items: list[Any], size: int) -> Iterator[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _insert(conn: Connection, table, rows: list[dict[str, Any]]) -> None:
    for chunk in _chunks(rows, ROWS_PER_INSERT):
        conn.execute(insert(table), chunk)


def _write(conn: Connection, plans: list[Plan], report: Report) -> Counter:
    """Write the plans and return what happened to their runs."""
    tally: Counter = Counter()
    existing = {row.id: row.trigger_ref for row in conn.execute(
        select(runs.c.id, runs.c.trigger_ref).where(runs.c.id.in_([p.run_id for p in plans]))).all()}
    fresh, replaced = [], []
    for plan in plans:
        if plan.run_id not in existing:
            fresh.append(plan)
            continue
        ref = existing[plan.run_id] if isinstance(existing[plan.run_id], dict) else {}
        if "import_hash" not in ref:
            tally["kept, a live run has this id"] += 1
        elif ref["import_hash"] == plan.run["trigger_ref"]["import_hash"]:
            tally["unchanged"] += 1
        else:
            replaced.append(plan)
    if replaced:
        ids = [p.run_id for p in replaced]
        for table in (findings, run_events, run_steps):
            conn.execute(delete(table).where(table.c.run_id.in_(ids)))
        for plan in replaced:
            conn.execute(update(runs).where(runs.c.id == plan.run_id).values(**plan.run))
    _insert(conn, runs, [{"id": p.run_id, **p.run} for p in fresh])
    written = fresh + replaced
    _insert(conn, run_steps, [row for p in written for row in p.steps])
    _insert(conn, run_events, [row for p in written for row in p.events])
    _insert(conn, findings, [{"id": new_id(), **row} for p in written for row in p.findings])
    tally["created"] += len(fresh)
    tally["updated"] += len(replaced)
    for plan in written:
        report.add("rows written", "steps", len(plan.steps))
        report.add("rows written", "events", len(plan.events))
        report.add("rows written", "findings", len(plan.findings))
    return tally


def _import_batches(engine: Engine, keyring: Keyring, batches: Iterable[list[SourceRun]], total: int, *,
                    section: str, now: datetime, report: Report, dry_run: bool,
                    progress: Callable[[str], None]) -> None:
    done = 0
    for number, batch in enumerate(batches, start=1):
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                target = _Target.load(conn, keyring, now)
                plans = []
                for src in batch:
                    plan = plan_run(src, target, report)
                    if plan is None:
                        continue
                    plans.append(plan)
                    report.add("status", plan.run["status"])
                    report.add("team rule", plan.rule)
                    report.add("trigger kind", plan.run["trigger_kind"])
                    if plan.run["workflow_id"] is None:
                        report.add("workflow", "no server workflow, name kept")
                    if plan.run["triggered_by"]:
                        report.add("actor", "matched a user")
                    elif plan.run["actor"]:
                        report.add("actor", "kept as text")
                tally = _write(conn, plans, report) if plans else Counter()
                report.add(section, "created", 0)
                for outcome, n in tally.items():
                    report.add(section, outcome, n)
                if not dry_run:
                    audit.record(conn, action="history.import", actor_id=ACTOR, target_kind="runs", target_id=None,
                                 detail={"source": ACTOR, "section": section, "batch": number, "runs": len(batch),
                                         **dict(tally)})
            except BaseException:
                transaction.rollback()
                raise
            if dry_run:
                transaction.rollback()
            else:
                transaction.commit()
        done += len(batch)
        progress(f"{section}: {done}/{total}")


def import_history(engine: Engine, keyring: Keyring, source: HistorySource, *, now: datetime,
                   since: datetime | None = None,
                   limit: int | None = None, traces: bool = True, dry_run: bool = False,
                   progress: Callable[[str], None] = lambda _: None) -> Report:
    """Import in transactions of ``RUNS_PER_TRANSACTION`` runs. A dry run rolls every one back."""
    report = Report()
    every = source.dashboard_ids(None)
    every_dashboard_id = set(every)
    chosen = source.dashboard_ids(since) if since else every
    if limit is not None:
        chosen = chosen[:limit]
    for table, n in source.unlinked().items():
        report.add("linked rows", f"{table} skipped, no run", n)
    _import_batches(engine, keyring, (source.load_dashboard(ids) for ids in _chunks(chosen, RUNS_PER_TRANSACTION)),
                    len(chosen), section="dashboard runs", now=now, report=report, dry_run=dry_run,
                    progress=progress)
    if traces and (limit is None or len(chosen) < limit):
        with engine.connect() as conn:
            known = set(conn.execute(select(workflows.c.name)).scalars())
        trace_ids, skipped = [], 0
        for run_id, workflow in source.trace_runs(since):
            if run_id in every_dashboard_id:
                continue
            if workflow in known:
                trace_ids.append(run_id)
            else:
                skipped += 1
        report.add("trace-only runs", "found", len(trace_ids) + skipped)
        report.add("trace-only runs", "skipped, not a server workflow", skipped)
        if limit is not None:
            trace_ids = trace_ids[:limit - len(chosen)]
        _import_batches(engine, keyring, (source.load_traces(ids) for ids in _chunks(trace_ids, RUNS_PER_TRANSACTION)),
                        len(trace_ids), section="trace-only runs", now=now, report=report, dry_run=dry_run,
                        progress=progress)
    return report


# Sources.

RUN_COLUMNS = ("run_id, workflow, started_at, ended_at, last_event_at, complete, state, failed_stages, tokens_in, "
               "tokens_out, cost_usd, target, repo, number, head, author, title, verdict, post_url, post_event, "
               "jira_key, mode")
STAGE_COLUMNS = ("run_id, trace_id, stage_id, role_type, model, started_at, completed_at, latency_ms, success, "
                 "error_type, error_kind, tokens_in, tokens_out, output->>'cost_usd' AS cost_usd, "
                 "output->>'total_cost_usd' AS total_cost_usd, "
                 "CASE WHEN stage_id IN ('team_lead', 'decide') THEN output->'summary' END AS summary, "
                 "CASE WHEN stage_id IN ('team_lead', 'decide') THEN output->'content' END AS content, "
                 "CASE WHEN stage_id IN ('team_lead', 'decide') THEN output->>'verdict' END AS verdict")
REVIEW_COLUMNS = ("run_id, idx, stage_id, role, repo, platform, model, cost_usd, duration_ms, is_error, review_text, "
                  "fan_out_error, completed_at")
CHILDREN = {
    "stages": f"SELECT {STAGE_COLUMNS} FROM stages WHERE run_id = ANY(%s) ORDER BY run_id, trace_id",
    "reviews": f"SELECT {REVIEW_COLUMNS} FROM role_reviews WHERE run_id = ANY(%s) ORDER BY run_id, stage_id, idx",
    "decisions": "SELECT run_id, idx, claim, decision, reason, finding_id, disposition FROM lead_decisions "
                 "WHERE run_id = ANY(%s) ORDER BY run_id, idx",
    "comments": "SELECT run_id, idx, path, line, severity, body, finding_id FROM lead_comments "
                "WHERE run_id = ANY(%s) ORDER BY run_id, idx",
    "items": "SELECT run_id, kind, idx, repo, file, action, severity, topic, resolution FROM lead_items "
             "WHERE run_id = ANY(%s) ORDER BY run_id, kind, idx",
}
SINGLES = {
    "jira": "SELECT run_id, issue, platform, outcome, detail, pr_url FROM jira_work WHERE run_id = ANY(%s)",
    "fix": 'SELECT run_id, requester, scope, applied, skipped, outcome, "commit", detail FROM pr_fixes '
           "WHERE run_id = ANY(%s)",
    "merge": "SELECT run_id, merged_by, merged_at, issue, moved_to, transitioned FROM pr_merges WHERE run_id = ANY(%s)",
}
LINKED = ("jira_work", "pr_fixes", "pr_merges")


class OpsSource:
    """The dashboard's Postgres, read in one read-only transaction, and ``traces.db`` opened read-only."""

    def __init__(self, database_url: str, traces_path: Path | None):
        import psycopg
        from psycopg.rows import dict_row

        self._pg = psycopg.connect(database_url, row_factory=dict_row)
        self._pg.read_only = True
        self._traces = None
        if traces_path is not None and traces_path.is_file():
            self._traces = sqlite3.connect(f"{traces_path.resolve().as_uri()}?mode=ro", uri=True)
            self._traces.row_factory = sqlite3.Row
        self._authors: dict[tuple[str, int], str] | None = None
        self._cancels: dict[str, dict[str, Any]] | None = None

    def close(self) -> None:
        self._pg.rollback()
        self._pg.close()
        if self._traces is not None:
            self._traces.close()

    def _rows(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        return self._pg.execute(sql, params or None).fetchall()

    def dashboard_ids(self, since: datetime | None) -> list[str]:
        if since is None:
            rows = self._rows("SELECT run_id FROM runs ORDER BY started_at NULLS LAST, run_id")
        else:
            rows = self._rows("SELECT run_id FROM runs WHERE started_at >= %s ORDER BY started_at, run_id", since)
        return [row["run_id"] for row in rows]

    def unlinked(self) -> dict[str, int]:
        return {table: self._rows(f"SELECT count(*) AS n FROM {table} t "
                                  "WHERE NOT EXISTS (SELECT 1 FROM runs r WHERE r.run_id = t.run_id)")[0]["n"]
                for table in LINKED}

    def load_dashboard(self, ids: list[str]) -> list[SourceRun]:
        if self._authors is None:
            self._authors = {(r["repo"], r["number"]): r["author"]
                             for r in self._rows("SELECT repo, number, author FROM pr_authors WHERE author IS NOT NULL")}
            self._cancels = {r["run_id"]: r for r in self._rows(
                "SELECT args->>'run_id' AS run_id, requested_by, ended_at FROM ops "
                "WHERE kind = 'cancel-pr' AND state = 'done' AND exit_code = 0 AND args ? 'run_id'")}
        out = {}
        for row in self._rows(f"SELECT {RUN_COLUMNS} FROM runs WHERE run_id = ANY(%s)", ids):
            out[row["run_id"]] = SourceRun(run=row, pr_author=self._authors.get((row["repo"], row["number"])),
                                           cancel=self._cancels.get(row["run_id"]))
        for name, sql in CHILDREN.items():
            for row in self._rows(sql, ids):
                getattr(out[row["run_id"]], name).append(row)
        for name, sql in SINGLES.items():
            for row in self._rows(sql, ids):
                setattr(out[row["run_id"]], name, row)
        return [out[run_id] for run_id in ids if run_id in out]

    def trace_runs(self, since: datetime | None) -> list[tuple[str, str]]:
        if self._traces is None:
            return []
        having, params = "", []
        if since is not None:
            having, params = "HAVING MIN(timestamp) >= ?", [since.astimezone(UTC).isoformat()]
        # SQLite fills a bare column from the row that MIN() picked, so this is the first stage's workflow.
        rows = self._traces.execute(
            "SELECT run_id, workflow_name AS wf, MIN(timestamp) AS first "
            f"FROM traces GROUP BY run_id {having} ORDER BY first, run_id", params).fetchall()
        return [(row["run_id"], row["wf"]) for row in rows]

    def load_traces(self, ids: list[str]) -> list[SourceRun]:
        marks = ",".join("?" * len(ids))
        rows = self._traces.execute(
            "SELECT run_id, id, workflow_name, stage_id, role_type, model, input_tokens, output_tokens, latency_ms, "
            f"success, timestamp, error_type, error_kind FROM traces WHERE run_id IN ({marks}) ORDER BY run_id, id",
            ids).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(row["run_id"], []).append(row)
        return [trace_run(run_id, [dict(r) for r in grouped[run_id]]) for run_id in ids if run_id in grouped]


def trace_run(run_id: str, rows: list[dict[str, Any]]) -> SourceRun:
    """A run only ``traces.db`` knows, in the dashboard's shape. Each row's timestamp is when its stage completed."""
    stages = [{"stage_id": r["stage_id"], "role_type": r.get("role_type"), "model": r.get("model"),
               "completed_at": r["timestamp"], "latency_ms": r.get("latency_ms"), "success": bool(r.get("success")),
               "error_type": r.get("error_type"), "error_kind": r.get("error_kind"),
               "tokens_in": r.get("input_tokens"), "tokens_out": r.get("output_tokens")} for r in rows]
    starts = [s for s in (_stage_start(stage) for stage in stages) if s]
    ends = [e for e in (_aware(stage["completed_at"]) for stage in stages) if e]
    failed = any(not stage["success"] for stage in stages)
    run = {"run_id": run_id, "workflow": rows[0]["workflow_name"], "started_at": min(starts, default=None),
           "ended_at": max(ends, default=None), "last_event_at": max(ends, default=None),
           "state": "done_with_errors" if failed else "done",
           "tokens_in": sum(_int(s["tokens_in"]) for s in stages),
           "tokens_out": sum(_int(s["tokens_out"]) for s in stages)}
    return SourceRun(run=run, stages=stages, source="traces")


def ops_source(ops_dir: Path, traces_path: Path | None) -> OpsSource:
    path = ops_dir.expanduser() / "config" / "dashboard.json"
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("config/dashboard.json is missing or not JSON") from exc
    url = config.get("database_url") if isinstance(config, dict) else None
    if not isinstance(url, str) or not url:
        raise ValueError("config/dashboard.json has no database_url")
    return OpsSource(url, traces_path)

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, insert, select
from typer.testing import CliRunner

from armature.cli import app
from armature.server.db import new_id
from armature.server.db.schema import (
    audit,
    findings,
    projects,
    run_events,
    run_steps,
    runs,
    teams,
    users,
    workflows,
)
from armature.server.import_history import (
    TEXT_CAP,
    OpsSource,
    SourceRun,
    finding_key,
    import_history,
    server_run_id,
)

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
FEED_JS = Path(__file__).resolve().parents[2] / "armature" / "server" / "static" / "feed.js"
REVIEW_SECRET_LOOKALIKE = "ghp_" + "a" * 36


class MemorySource:
    """The dashboard and traces.db as lists, so the importer's own filtering and mapping are what is tested."""

    def __init__(self, dashboard: list[SourceRun], traces: list[SourceRun] = (), unlinked: dict | None = None):
        self.dashboard = {src.run["run_id"]: src for src in dashboard}
        self.traces = {src.run["run_id"]: src for src in traces}
        self._unlinked = unlinked or {}
        self.closed = False

    def dashboard_ids(self, since):
        chosen = [s for s in self.dashboard.values() if since is None or s.run["started_at"] >= since]
        return [s.run["run_id"] for s in sorted(chosen, key=lambda s: s.run["started_at"])]

    def load_dashboard(self, ids):
        return [self.dashboard[i] for i in ids]

    def unlinked(self):
        return dict(self._unlinked)

    def trace_runs(self, since):
        return [(i, s.run["workflow"]) for i, s in self.traces.items() if since is None or s.run["started_at"] >= since]

    def load_traces(self, ids):
        return [self.traces[i] for i in ids]

    def close(self):
        self.closed = True


def stage(stage_id, minute, *, seconds=30, success=True, cost=None, tokens=(0, 0), **extra):
    done = T0 + timedelta(minutes=minute)
    return {"stage_id": stage_id, "role_type": extra.pop("role_type", "script"), "model": extra.pop("model", ""),
            "started_at": done - timedelta(seconds=seconds), "completed_at": done, "latency_ms": seconds * 1000,
            "success": success, "error_type": None if success else "TimeoutExpired", "error_kind": None,
            "tokens_in": tokens[0], "tokens_out": tokens[1], "cost_usd": cost, **extra}


def review(role, idx, minute, *, text="Looks fine.", cost="0.25", is_error=False):
    return {"idx": idx, "stage_id": "reviews", "role": role, "repo": "fs-eng/android-tree", "platform": "android",
            "model": "us.anthropic.claude-opus-5-5", "cost_usd": Decimal(cost), "duration_ms": 60000,
            "is_error": is_error, "review_text": text, "fan_out_error": None,
            "completed_at": T0 + timedelta(minutes=minute)}


def pr_run(run_id="a1b2c3d4e5f6", *, state="done", started=T0, **run):
    values = {"run_id": run_id, "workflow": "pr-role-review", "started_at": started,
              "ended_at": started + timedelta(minutes=10), "last_event_at": started + timedelta(minutes=10),
              "complete": True, "state": state, "failed_stages": 0, "tokens_in": 1200, "tokens_out": 300,
              "cost_usd": Decimal("1.2345"), "target": "fs-eng/android-tree#1821", "repo": "fs-eng/android-tree",
              "number": 1821, "head": "abc123", "author": "StephanHeilner", "title": "Fix the header",
              "verdict": "request_changes", "post_url": "https://github.com/fs-eng/android-tree/pull/1821",
              "post_event": "COMMENT", "jira_key": "MEMTEMDIS-1", "mode": "review"}
    values.update(run)
    return SourceRun(
        run=values,
        stages=[stage("resolve", 1), stage("reviews", 4, seconds=180, cost="0.25"),
                stage("reviews", 5, seconds=200, cost="0.30"), stage("verify", 6, success=False),
                stage("team_lead", 9, seconds=100, role_type="judge", model="us.anthropic.claude-opus-5-5",
                      tokens=(1200, 300), summary="Two problems, one blocks.", verdict="request_changes"),
                stage("post", 10)],
        reviews=[review("correctness", 0, 4, text="x" * (TEXT_CAP + 50) + REVIEW_SECRET_LOOKALIKE),
                 review("correctness", 1, 5, cost="0.30", is_error=True)],
        decisions=[{"idx": 0, "claim": "Header overlaps the status bar", "decision": "block", "reason": "Real.",
                    "finding_id": "0123456789ab", "disposition": "keep"},
                   {"idx": 1, "claim": "Rename a variable", "decision": "drop", "reason": "Taste.",
                    "finding_id": None, "disposition": None},
                   {"idx": 2, "claim": "Old finding fixed", "decision": "drop", "reason": "Fixed in abc.",
                    "finding_id": "fedcba987654", "disposition": "resolved"}],
        comments=[{"idx": 0, "path": "app/Header.kt", "line": 42, "severity": "high",
                   "body": "The header overlaps.\nMore detail.", "finding_id": "0123456789ab"},
                  {"idx": 1, "path": "app/List.kt", "line": 7, "severity": "low", "body": "Nit: spacing.",
                   "finding_id": None}],
        pr_author="someone-else")


@pytest.fixture
def target(sqlite_engine):
    ids = {}
    with sqlite_engine.begin() as conn:
        for slug in ("mobile", "talentstorehouse", "product-prototype"):
            ids[slug] = new_id()
            conn.execute(insert(teams).values(id=ids[slug], name=slug.title(), slug=slug, created_at=T0))
        for team, repo, pipelines in (("mobile", "fs-eng/android-tree", ["daily_review", "fix", "pr_review"]),
                                      ("mobile", "fs-eng/mobile-ios", ["daily_review", "fix", "pr_review"]),
                                      ("talentstorehouse", "MountainRiverPartners/talentstorehouse",
                                       ["discuss", "fix", "pr_review"])):
            ids[repo] = new_id()
            conn.execute(insert(projects).values(id=ids[repo], team_id=ids[team], name=repo.split("/")[1],
                                                 repo=repo, enabled_workflows=pipelines, created_at=T0))
        for name in ("pr-role-review", "pr-fix", "repo-review", "pr-merged", "jira-work", "slack-intake"):
            ids[name] = new_id()
            conn.execute(insert(workflows).values(id=ids[name], team_id=None, scope="*", name=name,
                                                  current_version=1, created_at=T0, updated_at=T0))
        ids["user"] = new_id()
        conn.execute(insert(users).values(id=ids["user"], username="stephanheilner", created_at=T0,
                                          last_totp_step=0, is_admin=False, disabled=False))
    return ids


def run_import(engine, source, **kw):
    kw.setdefault("now", NOW)
    return import_history(engine, source, **kw)


def rows(engine, table, run_id):
    query = select(table).where(table.c.run_id == run_id)
    if table is run_events:
        query = query.order_by(run_events.c.seq)
    with engine.connect() as conn:
        return conn.execute(query).mappings().all()


def the_run(engine, run_id):
    with engine.connect() as conn:
        return conn.execute(select(runs).where(runs.c.id == run_id)).mappings().one()


def feed_kinds() -> set[str]:
    source = FEED_JS.read_text()
    body = source[source.index("export function describe"):]
    body = body[body.index("const lines = {"):]
    body = body[:body.index("\n  };")]
    return set(re.findall(r"^\s+([a-z_]+): \(\) =>", body, re.MULTILINE))


def test_a_pull_request_review_maps_onto_run_steps_and_findings(sqlite_engine, target):
    report = run_import(sqlite_engine, MemorySource([pr_run()]))
    run = the_run(sqlite_engine, "a1b2c3d4e5f6")
    assert run["team_id"] == target["mobile"] and run["project_id"] == target["fs-eng/android-tree"]
    assert run["workflow_id"] == target["pr-role-review"] and run["workflow_name"] == "pr-role-review"
    assert run["status"] == "succeeded" and run["trigger_kind"] == "pr"
    assert run["actor"] == "StephanHeilner" and run["triggered_by"] == target["user"]
    assert run["cost_usd"] == Decimal("1.234500") and (run["input_tokens"], run["output_tokens"]) == (1200, 300)
    assert run["started_at"] == T0 and run["finished_at"] == T0 + timedelta(minutes=10)
    ref = run["trigger_ref"]
    assert {k: ref[k] for k in ("repo", "number", "mode", "jira_key", "head", "source")} == {
        "repo": "fs-eng/android-tree", "number": 1821, "mode": "review", "jira_key": "MEMTEMDIS-1",
        "head": "abc123", "source": "dashboard"}
    assert len(ref["import_hash"]) == 64 and "source_run_id" not in ref
    assert run["summary"]["verdict"] == "request_changes"

    steps = {s["step_key"]: s for s in rows(sqlite_engine, run_steps, "a1b2c3d4e5f6")}
    assert set(steps) == {"resolve", "reviews", "reviews/correctness", "reviews/correctness-2", "verify",
                          "team_lead", "post"}
    assert steps["reviews"]["cost_usd"] is None
    assert steps["reviews"]["started_at"] == T0 + timedelta(minutes=4, seconds=-180)
    assert steps["reviews/correctness"]["parent_step"] == "reviews"
    assert steps["reviews/correctness"]["cost_usd"] == Decimal("0.250000")
    assert steps["reviews/correctness-2"]["status"] == "failed"
    assert steps["verify"]["status"] == "failed" and steps["verify"]["error"] == "TimeoutExpired"
    assert steps["team_lead"]["input_tokens"] == 1200

    found = sorted(rows(sqlite_engine, findings, "a1b2c3d4e5f6"), key=lambda f: f["finding_key"])
    assert [(f["finding_key"], f["disposition"], f["path"], f["line"], f["severity"]) for f in found] == sorted([
        ("0123456789ab", "keep", "app/Header.kt", 42, "high"),
        (finding_key("app/List.kt", "Nit: spacing."), None, "app/List.kt", 7, "low"),
        ("fedcba987654", "resolved", None, None, None)])
    header = next(f for f in found if f["finding_key"] == "0123456789ab")
    assert header["title"] == "Header overlaps the status bar" and header["step_id"] == "team_lead"
    assert header["project_id"] == target["fs-eng/android-tree"]
    assert report.counts["findings"]["decisions set aside, not imported"] == 1


def test_the_timeline_is_contiguous_and_renders_with_the_feed(sqlite_engine, target):
    run_import(sqlite_engine, MemorySource([pr_run()]))
    events = rows(sqlite_engine, run_events, "a1b2c3d4e5f6")
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[0]["kind"] == "run_start" and events[-1]["kind"] == "run_end"
    assert events[-1]["payload"]["status"] == "succeeded"
    assert all(e["payload"]["imported"] is True for e in events)
    assert {e["kind"] for e in events} <= feed_kinds() | {"text"}
    assert {"step_start", "step_end", "step_failed", "cost", "finding", "text"} <= {e["kind"] for e in events}

    child = [e for e in events if e["step_id"] == "correctness"]
    assert [e["kind"] for e in child] == ["step_start", "cost", "text", "step_end"]
    assert all(e["payload"]["parent_step"] == "reviews" for e in child)
    text = child[2]["payload"]
    assert text["truncated"] is True and len(text["text"]) == TEXT_CAP
    lead = [e for e in events if e["step_id"] == "team_lead"]
    assert [e["kind"] for e in lead] == ["step_start", "cost", "text", "finding", "finding", "finding", "step_end"]
    assert lead[2]["payload"]["text"] == "Verdict: request_changes\n\nTwo problems, one blocks."
    assert {e["payload"]["title"] for e in lead if e["kind"] == "finding"} == {
        f["title"] for f in rows(sqlite_engine, findings, "a1b2c3d4e5f6")}
    starts = [e for e in events if e["kind"] == "step_start" and e["step_id"] == "reviews"]
    assert events.index(starts[0]) < events.index(child[0])


def test_review_text_is_redacted(sqlite_engine, target):
    src = pr_run()
    src.reviews[0]["review_text"] = f"token {REVIEW_SECRET_LOOKALIKE} leaked"
    run_import(sqlite_engine, MemorySource([src]))
    texts = [e["payload"]["text"] for e in rows(sqlite_engine, run_events, "a1b2c3d4e5f6") if e["kind"] == "text"]
    assert not any(REVIEW_SECRET_LOOKALIKE in t for t in texts)


def test_a_rerun_skips_unchanged_runs_and_replaces_changed_ones(sqlite_engine, target):
    first = run_import(sqlite_engine, MemorySource([pr_run()]))
    assert first.counts["dashboard runs"]["created"] == 1
    counts = {t.name: len(rows(sqlite_engine, t, "a1b2c3d4e5f6")) for t in (run_steps, run_events, findings)}

    again = run_import(sqlite_engine, MemorySource([pr_run()]))
    assert again.counts["dashboard runs"]["unchanged"] == 1 and again.counts["dashboard runs"]["created"] == 0
    assert "rows written" not in again.counts

    changed = pr_run(verdict="approve")
    changed.comments.pop()
    third = run_import(sqlite_engine, MemorySource([changed]))
    assert third.counts["dashboard runs"]["updated"] == 1
    assert len(rows(sqlite_engine, run_steps, "a1b2c3d4e5f6")) == counts["run_steps"]
    assert len(rows(sqlite_engine, findings, "a1b2c3d4e5f6")) == counts["findings"] - 1
    events = rows(sqlite_engine, run_events, "a1b2c3d4e5f6")
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert the_run(sqlite_engine, "a1b2c3d4e5f6")["summary"]["verdict"] == "approve"


def test_a_dry_run_writes_nothing(sqlite_engine, target):
    report = run_import(sqlite_engine, MemorySource([pr_run()]), dry_run=True)
    assert report.counts["dashboard runs"]["created"] == 1
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(runs)).scalar() == 0
        assert conn.execute(select(func.count()).select_from(audit)
                            .where(audit.c.action == "history.import")).scalar() == 0


def test_a_committed_import_is_audited_with_counts(sqlite_engine, target):
    run_import(sqlite_engine, MemorySource([pr_run()]))
    with sqlite_engine.connect() as conn:
        entry = conn.execute(select(audit).where(audit.c.action == "history.import")).mappings().one()
    assert entry["detail"]["dashboard runs"] == {"created": 1, "updated": 0}


@pytest.mark.parametrize(("state", "ended", "last_seen", "cancel", "status", "error"), [
    ("done", True, 0, None, "succeeded", None),
    ("done_with_errors", True, 0, None, "failed", "1 step(s) failed: verify"),
    ("died", False, 0, None, "lost", "Stopped without finishing"),
    ("running", False, 3, None, "running", None),
    ("running", False, 30, None, "lost", "Stopped without finishing"),
    ("unknown", False, 30, None, "lost", "Stopped without finishing"),
    ("done", True, 0, {"requested_by": "stephanheilner", "ended_at": T0 + timedelta(minutes=3)}, "canceled", None),
])
def test_status_mapping(sqlite_engine, target, state, ended, last_seen, cancel, status, error):
    seen = NOW - timedelta(hours=last_seen) if last_seen else T0 + timedelta(minutes=10)
    src = pr_run(state=state, ended_at=T0 + timedelta(minutes=10) if ended else None, last_event_at=seen)
    src.cancel = cancel
    run_import(sqlite_engine, MemorySource([src]))
    run = the_run(sqlite_engine, "a1b2c3d4e5f6")
    assert (run["status"], run["error"]) == (status, error)
    events = rows(sqlite_engine, run_events, "a1b2c3d4e5f6")
    if status == "running":
        assert run["finished_at"] is None and events[-1]["kind"] != "run_end"
    else:
        assert run["finished_at"] is not None and events[-1]["payload"]["status"] == status
    if cancel:
        assert run["cancelled_by"] == target["user"]
        assert run["finished_at"] == T0 + timedelta(minutes=3)


def bare(run_id, workflow, **run):
    return SourceRun(run={"run_id": run_id, "workflow": workflow, "started_at": T0, "ended_at": T0, "state": "done",
                          **run})


def test_team_and_project_resolution_rules(sqlite_engine, target):
    with sqlite_engine.begin() as conn:
        team_workflow = new_id()
        conn.execute(insert(workflows).values(id=team_workflow, team_id=target["talentstorehouse"],
                                              scope=target["talentstorehouse"], name="ts-only", current_version=1,
                                              created_at=T0, updated_at=T0))
    report = run_import(sqlite_engine, MemorySource([
        bare("r-repo", "pr-fix", repo="MountainRiverPartners/TalentStorehouse"),
        bare("r-target1", "repo-review", target="fs-eng/android-tree, fs-eng/mobile-ios"),
        bare("r-target2", "pr-role-review", target="fs-eng/mobile-ios#5"),
        bare("r-workflow", "ts-only"),
        bare("r-pipeline", "pr-role-review", mode="discuss"),
        bare("r-default", "slack-intake"),
    ]))
    team = {rid: (the_run(sqlite_engine, rid)["team_id"], the_run(sqlite_engine, rid)["project_id"])
            for rid in ("r-repo", "r-target1", "r-target2", "r-workflow", "r-pipeline", "r-default")}
    assert team["r-repo"] == (target["talentstorehouse"], target["MountainRiverPartners/talentstorehouse"])
    assert team["r-target1"] == (target["mobile"], None)
    assert team["r-target2"] == (target["mobile"], target["fs-eng/mobile-ios"])
    assert team["r-workflow"] == (target["talentstorehouse"], None)
    assert team["r-pipeline"] == (target["talentstorehouse"], None)
    assert team["r-default"] == (target["mobile"], None)
    assert report.counts["team rule"] == {"repo": 1, "target": 2, "workflow": 1, "pipeline": 1, "default": 1}
    assert the_run(sqlite_engine, "r-pipeline")["trigger_kind"] == "discuss"


def test_side_tables_become_trigger_details_and_text(sqlite_engine, target):
    fix = bare("fix1", "pr-fix", repo="fs-eng/android-tree", number=9, author="author-login")
    fix.fix = {"requester": "StephanHeilner", "scope": "thread", "applied": 2, "skipped": 1, "outcome": "pushed",
               "commit": "deadbeef", "detail": "applied=2/3"}
    merge = bare("merge1", "pr-merged", repo="fs-eng/mobile-ios", number=7)
    merge.merge = {"merged_by": "miles", "merged_at": T0, "issue": "MEMTEMDIS-9", "moved_to": "Done",
                   "transitioned": True}
    jira = bare("jira1", "jira-work", repo="fs-eng/mobile-ios", jira_key="MEMTEMDIS-3")
    jira.jira = {"issue": "MEMTEMDIS-3", "platform": "ios", "outcome": "opened",
                 "pr_url": "https://github.com/fs-eng/mobile-ios/pull/1", "detail": "Opened."}
    report = run_import(sqlite_engine, MemorySource([fix, merge, jira], unlinked={"pr_fixes": 2}))
    fixed = the_run(sqlite_engine, "fix1")
    assert fixed["actor"] == "StephanHeilner" and fixed["triggered_by"] == target["user"]
    assert fixed["trigger_kind"] == "fix" and fixed["trigger_ref"]["fix"]["commit"] == "deadbeef"
    assert fixed["trigger_ref"]["mode"] == "thread"
    merged = the_run(sqlite_engine, "merge1")
    assert merged["actor"] == "miles" and merged["triggered_by"] is None and merged["trigger_kind"] == "merged"
    assert merged["trigger_ref"]["merge"]["merged_at"] == T0.isoformat()
    assert the_run(sqlite_engine, "jira1")["trigger_ref"]["jira"]["outcome"] == "opened"
    texts = {rid: [e["payload"]["text"] for e in rows(sqlite_engine, run_events, rid) if e["kind"] == "text"]
             for rid in ("fix1", "merge1", "jira1")}
    assert texts["merge1"] == ["Merged by miles and moved MEMTEMDIS-9 to Done"]
    assert texts["fix1"][0].startswith("Fix requested by StephanHeilner (thread) outcome pushed")
    assert texts["jira1"][0].startswith("Jira work MEMTEMDIS-3 (ios) outcome opened")
    assert report.counts["linked rows"]["pr_fixes skipped, no run"] == 2


def test_lead_items_become_findings(sqlite_engine, target):
    src = bare("daily1", "repo-review", target="fs-eng/android-tree")
    src.stages = [stage("team_lead", 3, role_type="judge", content='```json\n{"summary": "Two changes."}\n```')]
    src.items = [{"kind": "change", "idx": 0, "repo": "fs-eng/android-tree", "file": "a.kt", "action": "Fix it\nnow",
                  "severity": "medium"},
                 {"kind": "conflict", "idx": 0, "topic": "Retry policy", "resolution": "Keep it", "file": None},
                 {"kind": "no_action", "idx": 0, "action": "Fine"}]
    report = run_import(sqlite_engine, MemorySource([src]))
    found = {f["disposition"]: f for f in rows(sqlite_engine, findings, "daily1")}
    assert set(found) == {"change", "conflict"}
    assert found["change"]["title"] == "Fix it" and found["change"]["path"] == "a.kt"
    assert found["conflict"]["body"] == "Keep it"
    assert report.counts["findings"]["no-action items, not imported"] == 1
    lead_text = [e for e in rows(sqlite_engine, run_events, "daily1") if e["kind"] == "text"]
    assert lead_text[0]["payload"]["text"] == "Two changes."


def test_trace_only_runs_import_when_their_workflow_exists(sqlite_engine, target, tmp_path):
    path = tmp_path / "traces.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE traces (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, workflow_name TEXT, "
               "stage_id TEXT, role_type TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, "
               "latency_ms REAL, success INTEGER, timestamp TEXT, error_type TEXT, error_kind TEXT)")
    rows_in = [("dup000000001", "pr-role-review", "resolve", "script", "", 0, 0, 1000, 1, T0),
               ("trace0000001", "pr-merged", "comment", "script", "", 0, 0, 2000, 1, T0),
               ("trace0000001", "pr-merged", "transition", "judge", "haiku", 50, 10, 3000, 0,
                T0 + timedelta(seconds=5)),
               ("test00000001", "echo-workflow", "echo", "script", "", 0, 0, 10, 1, T0)]
    db.executemany("INSERT INTO traces (run_id, workflow_name, stage_id, role_type, model, input_tokens, "
                   "output_tokens, latency_ms, success, timestamp) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   [(*r[:-1], r[-1].isoformat()) for r in rows_in])
    db.commit()
    db.close()

    source = OpsSource.__new__(OpsSource)
    source._traces = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    source._traces.row_factory = sqlite3.Row
    listed = source.trace_runs(None)
    assert listed == [("dup000000001", "pr-role-review"), ("test00000001", "echo-workflow"),
                      ("trace0000001", "pr-merged")]
    assert source.trace_runs(T0 + timedelta(seconds=1)) == []
    traced = {s.run["run_id"]: s for s in source.load_traces([r for r, _ in listed])}
    source._traces.close()

    report = run_import(sqlite_engine, MemorySource([bare("dup000000001", "pr-role-review")], traces=list(
        traced.values())))
    assert report.counts["trace-only runs"] == {"found": 2, "skipped, not a server workflow": 1, "created": 1,
                                                "updated": 0}
    run = the_run(sqlite_engine, "trace0000001")
    assert run["status"] == "failed" and run["trigger_ref"]["source"] == "traces"
    assert (run["input_tokens"], run["output_tokens"]) == (50, 10)
    assert run["started_at"] == T0 - timedelta(seconds=2)
    steps = {s["step_id"]: s for s in rows(sqlite_engine, run_steps, "trace0000001")}
    assert steps["transition"]["status"] == "failed" and steps["transition"]["input_tokens"] == 50


def test_since_and_limit_choose_the_oldest_runs_first(sqlite_engine, target):
    source = MemorySource([pr_run(f"run{i:09d}", started=T0 + timedelta(days=i)) for i in range(4)])
    report = run_import(sqlite_engine, source, since=T0 + timedelta(days=1), limit=2)
    assert report.counts["dashboard runs"]["created"] == 2
    with sqlite_engine.connect() as conn:
        assert sorted(conn.execute(select(runs.c.id)).scalars()) == ["run000000001", "run000000002"]


def test_a_live_run_with_the_same_id_is_kept(sqlite_engine, target):
    with sqlite_engine.begin() as conn:
        conn.execute(insert(runs).values(id="a1b2c3d4e5f6", team_id=target["mobile"], status="running",
                                         started_at=NOW, trigger_ref={}))
    report = run_import(sqlite_engine, MemorySource([pr_run()]))
    assert report.counts["dashboard runs"]["kept, a live run has this id"] == 1
    assert the_run(sqlite_engine, "a1b2c3d4e5f6")["status"] == "running"


def test_a_long_source_id_gets_a_stable_uuid():
    long_id = "x" * 40
    assert server_run_id(long_id) == server_run_id(long_id) != long_id
    assert len(server_run_id(long_id)) == 36
    assert server_run_id("a1b2c3d4e5f6") == "a1b2c3d4e5f6"


def test_long_source_ids_keep_the_original(sqlite_engine, target):
    run_import(sqlite_engine, MemorySource([pr_run("y" * 40)]))
    run = the_run(sqlite_engine, server_run_id("y" * 40))
    assert run["trigger_ref"]["source_run_id"] == "y" * 40


def test_cli_prints_counts_and_never_a_payload(sqlite_engine, target, tmp_path, monkeypatch):
    config = tmp_path / "server.yaml"
    config.write_text(f"database_url: {sqlite_engine.url}\nmaster_key: env:UNUSED_KEY\n")
    source = MemorySource([pr_run()])
    monkeypatch.setattr("armature.server.import_history.ops_source", lambda ops_dir, traces: source)
    result = CliRunner().invoke(app, ["server", "import-history", str(tmp_path), "--config", str(config),
                                      "--no-traces", "--since", "2026-10-01"])
    assert result.exit_code == 0, result.output
    assert "dashboard runs: 1 created" in result.output and "Import committed" in result.output
    assert "Header overlaps" not in result.output and "Looks fine" not in result.output
    assert source.closed


def test_cli_refuses_a_bad_since(tmp_path, monkeypatch):
    config = tmp_path / "server.yaml"
    config.write_text(f"database_url: sqlite:///{tmp_path / 'x.db'}\n")
    monkeypatch.setattr("armature.server.import_history.ops_source", lambda ops_dir, traces: MemorySource([]))
    result = CliRunner().invoke(app, ["server", "import-history", str(tmp_path), "--config", str(config),
                                      "--since", "last tuesday"])
    assert result.exit_code == 1 and "--since" in result.output


def test_status_change_with_time_updates_the_run(sqlite_engine, target):
    src = pr_run(state="running", ended_at=None, last_event_at=NOW - timedelta(hours=1))
    run_import(sqlite_engine, MemorySource([src]))
    assert the_run(sqlite_engine, "a1b2c3d4e5f6")["status"] == "running"
    later = run_import(sqlite_engine, MemorySource([src]), now=NOW + timedelta(days=2))
    assert later.counts["dashboard runs"]["updated"] == 1
    assert the_run(sqlite_engine, "a1b2c3d4e5f6")["status"] == "lost"

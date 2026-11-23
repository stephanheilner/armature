"""The database-only review tools a pack calls after making its own GitHub calls, through the broker."""
from __future__ import annotations

import pytest
from sqlalchemy import insert, select

from armature.server import connections, risk
from armature.server.db import new_id
from armature.server.db.schema import review_marks, runs, settings
from armature.server.tools import NONE, marks, registry
from armature.server.vendors import github as github_vendor
from tests.server.tool_support import (  # noqa: F401
    HEAD,
    NUMBER,
    REPO,
    app,
    calls,
    clock,
    dns,
    pem,
    start_run,
    tool,
    vendor,
    world,
)

NEW = "b" * 40
PR = {"repo": REPO, "number": NUMBER}


@pytest.fixture
def run(sqlite_engine, keyring, clock, world):
    return start_run(sqlite_engine, keyring, clock, world, risk=None)


def _ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def _risk(app, run, level="low", head=HEAD, **params):
    params.setdefault("total", len(set(params.get("paths") or [])))
    return _ok(tool(app, run, "record_risk", "review.record-risk", {**PR, "head": head, "level": level, **params}))


def _post(app, run, head=HEAD, posted=True, approved=True, **params):
    return _ok(tool(app, run, "record_post", "review.record-post",
                    {**PR, "head": head, "posted": posted, "approved": approved, **params}))


def _mark(app, run, head=HEAD, status_id=900):
    return _ok(tool(app, run, "record_mark", "review.record-mark", {**PR, "head": head, "status_id": status_id}))


def _is_marked(app, run, head=HEAD):
    return _ok(tool(app, run, "is_marked", "review.is-marked", {**PR, "head": head}))


def _row(engine, run_id):
    with engine.connect() as conn:
        return conn.execute(select(runs).where(runs.c.id == run_id)).first()


def _marks(engine):
    with engine.connect() as conn:
        return conn.execute(select(review_marks)).all()


@pytest.fixture
def boom(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a database-only tool touched a connection")
    monkeypatch.setattr(connections, "resolve", refuse)
    monkeypatch.setattr(connections, "credentials", refuse)
    monkeypatch.setattr(github_vendor, "mint", refuse)


def test_the_database_only_tools_resolve_no_connection(app, sqlite_engine, run, vendor, boom):
    assert {name for name, spec in registry().items() if spec.kind == NONE} == {
        "review.decisions", "review.record-risk", "review.record-post", "review.record-mark", "review.is-marked",
        "review.record-branch-review", "review.record-branch-mark", "insights.fix-context", "insights.fix-patch",
        "insights.fix-risk", "insights.fix-apply", "workflow.handoff", "workflow.again", "eval.replay-start", "eval.replay-report"}
    _risk(app, run)
    _post(app, run)
    assert _mark(app, run)["marked"] is True
    assert _is_marked(app, run)["valid"] is True
    decided = tool(app, run, "decisions", "review.decisions",
                   {**PR, "decisions": [{"claim": "x", "decision": "drop", "reason": "y"}]})
    assert _ok(decided) == {"recorded": 1}
    assert vendor.requests == []


def test_a_repository_outside_the_jobs_project_is_refused(app, run, boom):
    response = tool(app, run, "is_marked", "review.is-marked", {"repo": "acme/other", "number": 1, "head": HEAD})
    assert response.status_code == 403


# review.record-risk

def test_record_risk_records_the_level_head_and_low_risk_panel(app, sqlite_engine, run):
    out = _risk(app, run, reasons=["copy change"], paths=["src/a.swift"])
    assert (out["level"], out["floor"], out["model"], out["head"]) == ("low", "low", "low", HEAD)
    assert out["panel"] == ["correctness", "security", "conventions", "simplicity"]
    assert out["reasons"] == [{"source": "model", "text": "copy change"}]
    stored = _row(sqlite_engine, run.id)
    assert (stored.risk, stored.head_sha) == ("low", HEAD)


def test_record_risk_applies_the_servers_floors_to_the_reported_paths(app, sqlite_engine, keyring, clock, world,
                                                                     run):
    out = _risk(app, run, paths=["db/migrations/0001.sql"])
    assert (out["level"], out["floor"]) == ("high", "high") and len(out["panel"]) == 9
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=world.team, key="risk.floors", value={"medium": ["docs/**"]}))
    other = start_run(sqlite_engine, keyring, clock, world, risk=None)
    assert _risk(app, other, paths=["docs/a.md"])["level"] == "medium"
    third = start_run(sqlite_engine, keyring, clock, world, risk=None)
    assert _risk(app, third, paths=["src/net/Client.swift"], added=["src/net/Client.swift"])["level"] == "high"
    fourth = start_run(sqlite_engine, keyring, clock, world, risk=None)
    assert _risk(app, fourth, total=3001)["level"] == "high"


def test_a_listing_cut_short_is_high(app, sqlite_engine, keyring, clock, world, run):
    out = _risk(app, run, paths=["src/a.swift", "src/a.swift"], total=2)
    assert out["level"] == "high" and out["reasons"][0]["text"] == "GitHub listed fewer files than the diff changes"
    other = start_run(sqlite_engine, keyring, clock, world, risk=None)
    assert _risk(app, other, paths=["src/a.swift"], truncated=True)["level"] == "high"
    third = start_run(sqlite_engine, keyring, clock, world, risk=None)
    assert _risk(app, third, paths=["src/a.swift", "src/b.swift"], total=2)["level"] == "low"


def test_record_risk_never_lowers_the_runs_level(app, sqlite_engine, run):
    _risk(app, run, level="high")
    assert _risk(app, run, level="low")["level"] == "high"
    assert _row(sqlite_engine, run.id).risk == "high"


def test_record_risk_refuses_a_bad_level_or_head(app, run):
    assert tool(app, run, "record_risk", "review.record-risk",
                {**PR, "head": HEAD, "level": "none", "total": 0}).status_code == 400
    assert tool(app, run, "record_risk", "review.record-risk",
                {**PR, "head": "abc", "level": "low", "total": 0}).status_code == 400


def test_record_risk_requires_the_diffs_file_count(app, sqlite_engine, run):
    missing = tool(app, run, "record_risk", "review.record-risk", {**PR, "head": HEAD, "level": "low"})
    assert missing.status_code == 400 and "total" in missing.json()["error"]
    assert _row(sqlite_engine, run.id).risk is None


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_record_risk_without_triggers_runs_the_panel_it_always_did(app, run, level):
    out = _risk(app, run, level=level, paths=["src/a.swift"], small=True, lines=3)
    assert out["panel"] == (["correctness", "security", "conventions", "simplicity"] if level == "low"
                            else list(risk.SPECIALISTS))
    assert out["triggers"] == {"model": [], "path": []}


def test_record_risk_picks_specialists_from_triggers_by_source(app, run):
    out = _risk(app, run, level="medium", paths=["package.json", "src/a.swift"], triggers=["ui"])
    assert out["panel"] == ["correctness", "simplicity", "conventions", "test_adequacy", "security", "beauty",
                            "compatibility"]
    assert out["triggers"] == {"model": ["ui"], "path": ["security", "compatibility"]}
    assert out["skipped"] == ["performance", "concurrency"]


@pytest.mark.parametrize("path", ["app/build.gradle.kts", "gradle/libs.versions.toml"])
def test_record_risk_runs_security_on_a_dependency_or_build_change(app, fresh, path):
    for model in ([], ["performance"]):
        out = _risk(app, fresh(), level="medium", paths=[path, "src/a.kt"], triggers=model)
        assert {"security", "compatibility"} <= set(out["panel"]), model
    plain = _risk(app, fresh(), level="medium", paths=["src/a.kt"], triggers=[])
    assert plain["panel"] == list(risk.CORE_PANEL)


def test_record_risk_runs_the_reviewer_alone_for_a_small_low_change(app, sqlite_engine, keyring, clock, world, run):
    out = _risk(app, run, paths=["src/a.swift"], triggers=[], small=True, lines=12)
    assert out["panel"] == [risk.REVIEWER] and out["skipped"] == list(risk.SPECIALISTS)
    other = start_run(sqlite_engine, keyring, clock, world, risk=None)
    six = [f"src/{i}.swift" for i in range(6)]
    assert _risk(app, other, paths=six, triggers=[], small=True, lines=12)["panel"] == \
        list(risk.DEFAULT_LOW_RISK_PANEL)


def test_record_risk_counts_reported_paths_beyond_total(app, run):
    six = [f"src/{i}.swift" for i in range(6)]
    out = _risk(app, run, paths=six, total=3, triggers=[], small=True, lines=10)
    assert out["panel"] == list(risk.DEFAULT_LOW_RISK_PANEL)


def test_record_risk_reads_mistyped_trigger_inputs_as_the_larger_panel(app, run):
    for bad in ({"small": "yes"}, {"small": 1, "lines": 10}, {"small": True, "lines": -1},
                {"small": True, "lines": 2.5}, {"small": True, "lines": "10"}, {"small": True, "lines": [10]}):
        out = _risk(app, run, paths=["src/a.swift"], triggers=["performance"], **bad)
        assert out["panel"] == [*risk.DEFAULT_LOW_RISK_PANEL, "performance"], bad
    for bad in ("security", [1], {"ui": True}, 3, ["telepathy"]):
        out = _risk(app, run, level="medium", paths=["src/a.swift"], triggers=bad, small=True, lines=1)
        assert out["panel"] == list(risk.SPECIALISTS) and out["triggers"] == {"model": [], "path": []}, bad
    assert _risk(app, run, triggers=None, small=None, lines=None)["panel"] == list(risk.SPECIALISTS)


WHOLE = {"paths": ["auth/session.py", "src/a.swift"], "triggers": [], "small": True, "lines": 10}


@pytest.fixture
def fresh(sqlite_engine, keyring, clock, world):
    return lambda: start_run(sqlite_engine, keyring, clock, world, risk=None)


@pytest.mark.parametrize("since", [{"since_paths": None}, {"since_paths": "src/a.swift"}, {"since_paths": [1]},
                                   {"since_paths": {"src/a.swift": 1}}, {"since_added": ["src/a.swift"]},
                                   {"since_paths": ["src/a.swift"], "since_added": "src/a.swift"},
                                   {"since_paths": ["src/a.swift"], "since_added": [None]},
                                   {"since_paths": ["src/a.swift"], "triggers": None},
                                   {"since_paths": ["src/a.swift"]},
                                   {"since_paths": ["src/a.swift"], "since_added": None},
                                   {"since_paths": [], "since_added": []}, {"since_paths": [""], "since_added": []}])
def test_an_increment_the_server_cant_use_leaves_the_panel_as_before(app, fresh, since):
    params = {**WHOLE, **since}
    before = _risk(app, fresh(), **{k: v for k, v in params.items() if not k.startswith("since_")})
    out = _risk(app, fresh(), **params)
    assert out == before and out["scope"] == "full" and out["panel_level"] == out["level"]


def test_a_small_increment_picks_the_panel_and_the_whole_pull_request_keeps_the_level(app, sqlite_engine, fresh):
    run = fresh()
    out = _risk(app, run, **WHOLE, since_paths=["src/a.swift"], since_added=[])
    assert (out["level"], out["panel"], out["scope"], out["panel_level"]) == ("high", [risk.REVIEWER], "since", "low")
    whole = _risk(app, fresh(), **WHOLE)
    assert (out["floor"], out["reasons"]) == (whole["floor"], whole["reasons"])
    assert out["triggers"] == {"model": [], "path": []}
    stored = _row(sqlite_engine, run.id)
    assert (stored.risk, stored.head_sha) == ("high", HEAD)


def test_an_increment_on_a_high_floor_runs_all_nine(app, fresh):
    out = _risk(app, fresh(), **WHOLE, since_paths=["auth/session.py"], since_added=[])
    assert (out["panel"], out["panel_level"]) == (list(risk.SPECIALISTS), "high")
    added = _risk(app, fresh(), **WHOLE, since_paths=["src/api/Client.swift"], since_added=["src/api/Client.swift"])
    assert (added["panel"], added["panel_level"]) == (list(risk.SPECIALISTS), "high")


def test_only_the_increments_paths_force_triggers(app, fresh):
    whole = {**WHOLE, "paths": ["package.json", "app/build.gradle.kts", "src/a.kt"], "small": False}
    outside = _risk(app, fresh(), **whole, since_paths=["src/a.kt"], since_added=[])
    assert outside["triggers"]["path"] == [] and "compatibility" not in outside["panel"]
    assert outside["panel"] == list(risk.DEFAULT_LOW_RISK_PANEL) and outside["panel_level"] == "low"
    for since in (["app/build.gradle.kts"], ["package.json"]):
        inside = _risk(app, fresh(), **whole, since_paths=since, since_added=[])
        assert inside["triggers"]["path"] == ["security", "compatibility"] and inside["panel_level"] == "medium"
        assert inside["panel"] == [*risk.CORE_PANEL, "security", "compatibility"]


def test_a_truncated_listing_ignores_the_increment(app, fresh):
    out = _risk(app, fresh(), paths=["src/a.swift"], truncated=True, triggers=[], small=True, lines=1,
                since_paths=["src/a.swift"], since_added=[])
    assert (out["panel"], out["scope"], out["panel_level"]) == (list(risk.SPECIALISTS), "full", "high")


def test_an_increment_as_long_as_the_compare_api_returns_is_not_trusted(app, fresh):
    since = [f"a/{i:03}.md" for i in range(marks.COMPARE_MAX)] + ["auth/session.py"]
    out = _risk(app, fresh(), **WHOLE, since_paths=since, since_added=[])
    assert (out["panel"], out["scope"], out["panel_level"]) == (list(risk.SPECIALISTS), "full", "high")


# review.record-post and review.record-mark

def test_record_mark_refuses_without_an_approving_post_of_the_runs_head(app, sqlite_engine, run):
    assert _mark(app, run) == {"marked": False, "reason": "the run has no reviewed head"}
    _risk(app, run)
    assert _mark(app, run)["reason"] == "this run posted no approving review of its head"
    _post(app, run, approved=False)
    assert _mark(app, run)["marked"] is False
    _post(app, run, head=NEW)
    assert _mark(app, run)["marked"] is False
    _post(app, run, posted=False)
    assert _mark(app, run)["marked"] is False
    assert _marks(sqlite_engine) == []


def test_record_mark_refuses_a_head_other_than_the_one_reviewed(app, sqlite_engine, run, clock):
    _risk(app, run)
    _post(app, run)
    clock.advance(seconds=1)
    out = _mark(app, run, head=NEW)
    assert out["marked"] is False and "not bbbbbbbbbbbb" in out["reason"]
    assert _marks(sqlite_engine) == []


def test_a_skipped_post_takes_a_null_review_id(app, sqlite_engine, run):
    assert _post(app, run, posted=False, approved=False, review_id=None)["posted"] is False
    assert tool(app, run, "record_post", "review.record-post", {**PR, "head": HEAD, "posted": True,
                                                                "approved": True, "review_id": 0}).status_code == 400


def test_a_non_approving_post_leaves_the_run_recommended(app, sqlite_engine, run):
    _risk(app, run)
    _post(app, run, approved=False, review_id=55)
    assert _row(sqlite_engine, run.id).outcome == "recommended"
    [row] = [c for c in calls(sqlite_engine) if c.tool == "review.record-post"]
    assert (row.outcome, row.target) == ("ok", f"{REPO.lower()}#{NUMBER}")
    assert row.detail == {"head": HEAD, "number": NUMBER, "posted": True, "approved": False, "review_id": 55}


def test_an_approved_head_gets_the_row_the_server_trusts(app, sqlite_engine, world, run, clock):
    _risk(app, run)
    _post(app, run)
    clock.advance(seconds=1)
    assert _mark(app, run) == {"marked": True, "head": HEAD, "check_run_id": 900, "outcome": "approved"}
    [mark] = _marks(sqlite_engine)
    assert (mark.project_id, mark.head_sha, mark.run_id, mark.check_run_id, mark.app_id, mark.conclusion) == (
        world.project, HEAD, run.id, 900, None, "success")
    stored = _row(sqlite_engine, run.id)
    assert (stored.outcome, stored.outcome_source) == ("approved", "server")


def test_only_a_newer_status_id_replaces_the_mark(app, sqlite_engine, run, clock):
    _risk(app, run)
    _post(app, run)
    clock.advance(seconds=1)
    _mark(app, run, status_id=900)
    assert _mark(app, run, status_id=800)["check_run_id"] == 900
    assert [m.check_run_id for m in _marks(sqlite_engine)] == [900]
    assert _mark(app, run, status_id=901)["check_run_id"] == 901
    assert [m.check_run_id for m in _marks(sqlite_engine)] == [901]


def test_record_mark_never_overwrites_an_app_check_runs_mark(app, sqlite_engine, world, run, clock):
    _risk(app, run)
    _post(app, run)
    clock.advance(seconds=1)
    with sqlite_engine.begin() as conn:
        conn.execute(insert(review_marks).values(id=new_id(), team_id=world.team, project_id=world.project,
                                                 repo=REPO.lower(), pr_number=NUMBER, head_sha=HEAD, run_id=run.id,
                                                 conclusion="success", check_run_id=5, app_id=4242,
                                                 created_at=clock()))
    assert _mark(app, run, status_id=900)["check_run_id"] == 5
    assert [(m.check_run_id, m.app_id) for m in _marks(sqlite_engine)] == [(5, 4242)]


def test_a_mark_another_run_inserted_first_is_updated_cleanly(app, sqlite_engine, world, run, clock, monkeypatch):
    _risk(app, run)
    _post(app, run)
    clock.advance(seconds=1)
    with sqlite_engine.begin() as conn:
        conn.execute(insert(review_marks).values(id=new_id(), team_id=world.team, project_id=world.project,
                                                 repo=REPO.lower(), pr_number=NUMBER, head_sha=HEAD, run_id=run.id,
                                                 conclusion="success", check_run_id=5, created_at=clock()))
    real, seen = marks.mark_of, []

    def stale(conn, project_id, head):
        seen.append(head)
        return None if len(seen) == 1 else real(conn, project_id, head)
    monkeypatch.setattr(marks, "mark_of", stale)
    assert _mark(app, run)["marked"] is True
    assert [m.check_run_id for m in _marks(sqlite_engine)] == [900]


def test_review_mark_ignores_a_recorded_post(app, sqlite_engine, vendor, run, clock):
    _risk(app, run)
    _post(app, run)
    clock.advance(seconds=1)
    out = _ok(tool(app, run, "mark", "review.mark", PR))
    assert out["marked"] is False and vendor.check_runs == []


def test_record_mark_ignores_a_live_post(app, sqlite_engine, vendor, run, clock):
    _risk(app, run)
    live = tool(app, run, "review", "github.post-review", {**PR, "head": HEAD, "verdict": "approve", "summary": "s",
                                                           "comments": [], "decisions": []})
    assert live.status_code == 200, live.text
    clock.advance(seconds=1)
    assert _mark(app, run)["reason"] == "this run posted no approving review of its head"
    assert _marks(sqlite_engine) == []


def test_a_record_risk_counts_as_the_re_review_after_a_push(app, sqlite_engine, world, run, clock):
    with sqlite_engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=world.team, key="github.push_authors", value=["*"]))
    _risk(app, run)
    pushed = tool(app, run, "push", "github.push", {**PR, "branch": "feature", "commit": NEW})
    assert pushed.json()["approved"] is True
    clock.advance(seconds=1)
    with sqlite_engine.connect() as conn:
        assert marks.push_refusal(conn, run.id, to_pr=True) == "the code this run last pushed was never re-reviewed"
    _risk(app, run, head=NEW)
    with sqlite_engine.connect() as conn:
        assert marks.push_refusal(conn, run.id, to_pr=True) is None


# review.record-branch-review and review.record-branch-mark

BRANCH = {"repo": REPO, "branch": "armature/MOB-1", "base": "dev"}
BLOCK = {"claim": "The lock leaks", "path": "src/a.kt", "raised_by": ["concurrency"], "decision": "block",
         "reason": "It leaks."}
COMMENT = {"path": "src/a.kt", "line": 3, "severity": "high", "blocking": True, "body": "It leaks.",
           "claim": "The lock leaks"}


def _branch_risk(app, run, head=HEAD, **params):
    return _ok(tool(app, run, "record_risk", "review.record-risk",
                    {**BRANCH, "head": head, "level": "low", "total": 1, "paths": ["src/a.kt"], **params}))


def _branch_review(app, run, head=HEAD, verdict="approve", decisions=(), complete=True, **params):
    return _ok(tool(app, run, "branch_review", "review.record-branch-review",
                    {**BRANCH, "head": head, "verdict": verdict, "decisions": list(decisions), "complete": complete,
                     "round": 1, **params}))


def _branch_mark(app, run, head=HEAD, status_id=900, **params):
    return tool(app, run, "branch_mark", "review.record-branch-mark",
                {**BRANCH, "head": head, "status_id": status_id, **params})


def _reviewed(app, run, clock, head=HEAD, **params):
    _branch_risk(app, run, head=head)
    clock.advance(seconds=1)
    return _branch_review(app, run, head=head, **params)


def _refused(response, reason):
    assert response.status_code == 422, response.text
    assert reason in response.json()["error"]


def test_record_risk_rates_a_branch_and_sets_the_runs_head(app, sqlite_engine, run):
    out = _branch_risk(app, run)
    assert (out["level"], out["head"]) == ("low", HEAD)
    stored = _row(sqlite_engine, run.id)
    assert (stored.risk, stored.head_sha) == ("low", HEAD)
    [row] = [c for c in calls(sqlite_engine) if c.tool == "review.record-risk"]
    assert (row.outcome, row.target) == ("ok", f"{REPO.lower()}:armature/MOB-1")


@pytest.mark.parametrize("form", [{**PR, "branch": "x", "base": "dev"}, {**PR, "branch": "x"}, {"repo": REPO},
                                  {"repo": REPO, "branch": "x"}, {"repo": REPO, "base": "dev"}])
def test_record_risk_takes_exactly_one_of_number_or_branch_and_base(app, sqlite_engine, run, form):
    response = tool(app, run, "record_risk", "review.record-risk", {**form, "head": HEAD, "level": "low", "total": 0})
    assert response.status_code == 400, response.text
    assert _row(sqlite_engine, run.id).head_sha is None


def test_the_server_decides_whether_a_branch_review_approved(app, sqlite_engine, run, clock):
    assert _reviewed(app, run, clock)["approved"] is True
    for given in ({"verdict": "comment"}, {"decisions": [BLOCK]}, {"decisions": [{**BLOCK, "decision": None}]},
                  {"complete": False}, {"head": NEW}, {"comments": [COMMENT]}):
        assert _branch_review(app, run, **given)["approved"] is False, given
    assert _branch_review(app, run, decisions=[{**BLOCK, "decision": "suggest"}, {**BLOCK, "decision": "drop"}])[
        "approved"] is True
    assert _branch_review(app, run, comments=[{**COMMENT, "blocking": False}])["approved"] is True
    rows = [c for c in calls(sqlite_engine) if c.tool == "review.record-branch-review"]
    assert {(r.outcome, r.target) for r in rows} == {("ok", f"{REPO.lower()}:armature/MOB-1")}
    assert rows[0].detail == {"head": HEAD, "branch": "armature/MOB-1", "base": "dev", "round": 1, "complete": True,
                              "blocking": 0, "approved": True}
    assert rows[2].detail["blocking"] == 1
    assert rows[6].detail["blocking"] == 1


def test_a_branch_review_comment_must_say_whether_it_blocks(app, run):
    unsaid = {key: value for key, value in COMMENT.items() if key != "blocking"}
    for comments in ([unsaid], [{**COMMENT, "blocking": "false"}]):
        response = tool(app, run, "branch_review", "review.record-branch-review",
                        {**BRANCH, "head": HEAD, "verdict": "approve", "decisions": [], "complete": True, "round": 1,
                         "comments": comments})
        assert response.status_code == 400, response.text


def test_a_branch_review_needs_a_rated_head(app, run):
    assert _branch_review(app, run)["approved"] is False


def test_an_approved_branch_head_gets_a_mark_with_no_pull_request(app, sqlite_engine, world, run, clock):
    _reviewed(app, run, clock)
    assert _ok(_branch_mark(app, run)) == {"marked": True, "head": HEAD, "check_run_id": 900}
    [mark] = _marks(sqlite_engine)
    assert (mark.project_id, mark.repo, mark.pr_number, mark.head_sha, mark.run_id, mark.check_run_id, mark.app_id,
            mark.conclusion) == (world.project, REPO.lower(), None, HEAD, run.id, 900, None, "success")
    assert _row(sqlite_engine, run.id).outcome is None


def test_a_branch_mark_takes_no_status_id(app, sqlite_engine, run, clock):
    _reviewed(app, run, clock)
    assert _ok(_branch_mark(app, run, status_id=None))["check_run_id"] is None
    assert _ok(_branch_mark(app, run, status_id=901))["check_run_id"] == 901


def test_a_branch_mark_needs_a_rated_head(app, sqlite_engine, run):
    _branch_review(app, run)
    _refused(_branch_mark(app, run), "the run has no rated head")
    assert _marks(sqlite_engine) == []


def test_a_branch_mark_refuses_a_head_other_than_the_rated_one(app, sqlite_engine, run, clock):
    _reviewed(app, run, clock)
    _refused(_branch_mark(app, run, head=NEW), "this run rated aaaaaaaaaaaa, not bbbbbbbbbbbb")
    assert _marks(sqlite_engine) == []


def test_a_branch_mark_refuses_a_head_that_isnt_a_full_sha(app, sqlite_engine, run, clock):
    _reviewed(app, run, clock)
    assert _branch_mark(app, run, head=HEAD[:12]).status_code == 400
    assert _marks(sqlite_engine) == []


def test_an_approval_of_an_earlier_head_never_marks_a_later_one(app, sqlite_engine, run, clock):
    _reviewed(app, run, clock, head=NEW)
    clock.advance(seconds=1)
    _branch_risk(app, run, head=HEAD)
    _refused(_branch_mark(app, run), "recorded no approving review of aaaaaaaaaaaa against dev")
    assert _marks(sqlite_engine) == []


def test_a_branch_review_older_than_the_latest_rating_never_marks(app, sqlite_engine, run, clock):
    _reviewed(app, run, clock)
    clock.advance(seconds=1)
    _branch_risk(app, run)
    _refused(_branch_mark(app, run), "rated its head again after its last branch review")
    assert _marks(sqlite_engine) == []


def test_a_branch_review_at_the_same_moment_as_its_rating_never_marks(app, sqlite_engine, run):
    _branch_risk(app, run)
    _branch_review(app, run)
    _refused(_branch_mark(app, run), "rated its head again after its last branch review")


@pytest.mark.parametrize("given", [{"verdict": "comment"}, {"decisions": [BLOCK]}, {"complete": False},
                                   {"comments": [COMMENT]}])
def test_a_branch_review_that_didnt_approve_never_marks(app, sqlite_engine, run, clock, given):
    _reviewed(app, run, clock, **given)
    _refused(_branch_mark(app, run), "recorded no approving review")
    assert _marks(sqlite_engine) == []


def test_only_the_last_branch_review_counts(app, sqlite_engine, run, clock):
    _reviewed(app, run, clock)
    clock.advance(seconds=1)
    _branch_review(app, run, verdict="comment")
    _refused(_branch_mark(app, run), "recorded no approving review")
    assert _marks(sqlite_engine) == []


@pytest.mark.parametrize("given", [{"base": "main"}, {"branch": "armature/MOB-2"}])
def test_a_branch_mark_needs_a_review_of_the_same_branch_and_base(app, sqlite_engine, run, clock, given):
    _reviewed(app, run, clock)
    _refused(_branch_mark(app, run, **given), "recorded no approving review")
    assert _marks(sqlite_engine) == []


def test_another_runs_branch_review_never_marks(app, sqlite_engine, keyring, clock, world, run):
    _branch_risk(app, run)
    other = start_run(sqlite_engine, keyring, clock, world, risk=None)
    _reviewed(app, other, clock)
    _refused(_branch_mark(app, run), "recorded no approving review")
    assert _marks(sqlite_engine) == []


def test_a_second_branch_mark_of_the_same_head_is_idempotent(app, sqlite_engine, run, clock):
    _reviewed(app, run, clock)
    assert _ok(_branch_mark(app, run))["check_run_id"] == 900
    assert _ok(_branch_mark(app, run)) == {"marked": True, "head": HEAD, "check_run_id": 900}
    assert _ok(_branch_mark(app, run, status_id=800))["check_run_id"] == 900
    assert _ok(_branch_mark(app, run, status_id=None))["check_run_id"] == 900
    assert [(m.check_run_id, m.pr_number) for m in _marks(sqlite_engine)] == [(900, None)]


def test_a_branch_mark_never_overwrites_an_app_check_runs_mark(app, sqlite_engine, world, run, clock):
    _reviewed(app, run, clock)
    with sqlite_engine.begin() as conn:
        conn.execute(insert(review_marks).values(id=new_id(), team_id=world.team, project_id=world.project,
                                                 repo=REPO.lower(), pr_number=NUMBER, head_sha=HEAD, run_id=run.id,
                                                 conclusion="success", check_run_id=5, app_id=4242,
                                                 created_at=clock()))
    assert _ok(_branch_mark(app, run))["check_run_id"] == 5
    assert [(m.check_run_id, m.app_id, m.pr_number) for m in _marks(sqlite_engine)] == [(5, 4242, NUMBER)]


def test_a_branch_mark_another_run_inserted_first_is_updated_cleanly(app, sqlite_engine, world, run, clock,
                                                                      monkeypatch):
    _reviewed(app, run, clock)
    with sqlite_engine.begin() as conn:
        conn.execute(insert(review_marks).values(id=new_id(), team_id=world.team, project_id=world.project,
                                                 repo=REPO.lower(), pr_number=NUMBER, head_sha=HEAD, run_id=run.id,
                                                 conclusion="success", check_run_id=5, created_at=clock()))
    real, seen = marks.mark_of, []

    def stale(conn, project_id, head):
        seen.append(head)
        return None if len(seen) == 1 else real(conn, project_id, head)
    monkeypatch.setattr(marks, "mark_of", stale)
    assert _ok(_branch_mark(app, run))["marked"] is True
    assert [(m.check_run_id, m.pr_number) for m in _marks(sqlite_engine)] == [(900, None)]


def test_a_dry_run_records_a_branch_mark_without_writing_one(app, sqlite_engine, keyring, clock, world):
    run = start_run(sqlite_engine, keyring, clock, world, trigger_ref={"dry_run": True}, risk=None)
    _reviewed(app, run, clock)
    assert _ok(_branch_mark(app, run))["dry_run"] is True
    assert _marks(sqlite_engine) == []


def test_a_pull_request_with_a_branch_marked_head_is_marked(app, sqlite_engine, keyring, clock, world, run):
    _reviewed(app, run, clock)
    _ok(_branch_mark(app, run))
    review = start_run(sqlite_engine, keyring, clock, world, risk=None)
    assert _is_marked(app, review) == {"valid": True, "head": HEAD}
    pr_mode = start_run(sqlite_engine, keyring, clock, world, risk=None)
    assert _ok(tool(app, pr_mode, "marked", "review.marked", PR)) == {"valid": True, "head": HEAD,
                                                                       "source": "database"}
    assert _row(sqlite_engine, pr_mode.id).outcome == "noop"


# review.is-marked

def test_is_marked_is_true_only_after_record_mark_and_settles_noop(app, sqlite_engine, run, clock):
    assert _is_marked(app, run) == {"valid": False, "head": HEAD}
    assert _row(sqlite_engine, run.id).outcome is None
    _risk(app, run)
    _post(app, run)
    clock.advance(seconds=1)
    _mark(app, run)
    assert _is_marked(app, run, head=NEW)["valid"] is False
    assert _is_marked(app, run) == {"valid": True, "head": HEAD}
    stored = _row(sqlite_engine, run.id)
    assert (stored.outcome, stored.outcome_source) == ("noop", "server")


def test_a_dry_run_records_a_mark_without_writing_one(app, sqlite_engine, keyring, clock, world):
    run = start_run(sqlite_engine, keyring, clock, world, trigger_ref={"dry_run": True}, risk=None)
    _risk(app, run)
    _post(app, run)
    assert _mark(app, run)["dry_run"] is True
    assert _marks(sqlite_engine) == [] and _is_marked(app, run)["valid"] is False
    assert _row(sqlite_engine, run.id).risk == "low"


def test_a_recorded_post_credits_armature_with_the_review_its_webhook_stored(app, sqlite_engine, world, run, clock):
    from armature.server.db.schema import gh_pull_requests, gh_repos, gh_reviews
    from tests.server.tool_support import BOT
    repo, pr, review = new_id(), new_id(), new_id()
    now = clock()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(gh_repos).values(id=repo, team_id=world.team, gh_repo_id=1, full_name=REPO.lower(),
                                             enabled=True, created_at=now, updated_at=now))
        conn.execute(insert(gh_pull_requests).values(
            id=pr, team_id=world.team, repo_row_id=repo, gh_pr_id=2, number=NUMBER, author_gh_id=3,
            author_login="octo", state="open", created_at_gh=now, gh_updated_at=now, created_at=now, updated_at=now))
        conn.execute(insert(gh_reviews).values(id=review, team_id=world.team, pr_row_id=pr, gh_review_id=55,
                                               reviewer_gh_id=9001, reviewer_login=BOT, state="commented",
                                               head_sha=HEAD, submitted_at=now, created_at=now, updated_at=now))
    _post(app, run, approved=False, review_id=55)
    with sqlite_engine.connect() as conn:
        assert conn.execute(select(gh_reviews.c.actor_source).where(gh_reviews.c.id == review)).scalar() == "armature"


def test_a_posted_reviews_row_keeps_its_review_id():
    params = {"head": HEAD, "number": NUMBER}
    assert marks.review_detail(params, {"posted": True, "review_id": 77})["review_id"] == 77
    assert "review_id" not in marks.review_detail(params, {"posted": False, "review_id": 77})

"""The subject list and page: grouping, status, finding history, scoping, and subjects with '#' and '/'."""
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote

import pytest
from sqlalchemy import insert, select, update

from armature.server import subjects
from armature.server.db import new_id
from armature.server.ingest import list_row, load_summary
from armature.server.db.schema import (
    INSTANCE_SCOPE,
    findings,
    gh_pull_requests,
    gh_repos,
    projects,
    run_artifacts,
    runs,
    settings,
    teams,
    trigger_seen,
    trigger_state,
    triggers,
    workflows,
)
from tests.server.app_support import (
    Clock,
    add_membership,
    add_user,
    make_client,
    sign_in_as,
)

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
PR = "acme/app#12"
OTHER_PR = "acme/app#13"
ISSUE = "PROJ-7"
FOREIGN = "rival/app#1"
KINDS = {"code-review": "review", "pr-fix": "fix", "pr-merged": "merge", "dev-work": "work"}


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 23, hour, minute, tzinfo=UTC)


def _run(conn, team_id, workflow, started, subject, *, status="succeeded", cost=None, summary=None,
         trigger_ref=None, project_id=None):
    run_id = new_id()
    workflow_id = conn.execute(select(workflows.c.id).where(workflows.c.name == workflow)).scalar()
    conn.execute(insert(runs).values(id=run_id, team_id=team_id, project_id=project_id, workflow_id=workflow_id,
                                     workflow_name=workflow, status=status, started_at=started, finished_at=started,
                                     cost_usd=Decimal(str(cost)) if cost is not None else None, subject=subject,
                                     summary=summary, trigger_ref=trigger_ref))
    return run_id


def _finding(conn, run_id, key, disposition, *, title=None, path=None, line=None, created=NOW):
    conn.execute(insert(findings).values(id=new_id(), run_id=run_id, finding_key=key, disposition=disposition,
                                         title=title, path=path, line=line, created_at=created))


@pytest.fixture
def seeded(engine, keyring):
    clock = Clock(NOW)
    with engine.begin() as conn:
        own, other = new_id(), new_id()
        conn.execute(insert(teams).values(id=own, name="own", created_at=NOW))
        conn.execute(insert(teams).values(id=other, name="other", created_at=NOW))
        for name, kind in KINDS.items():
            conn.execute(insert(workflows).values(id=new_id(), name=name, kind=kind, current_version=1,
                                                  created_at=NOW, updated_at=NOW))
        project = new_id()
        conn.execute(insert(projects).values(id=project, team_id=own, name="app", created_at=NOW))
        ref = {"repo": "acme/app", "number": 12}
        first = _run(conn, own, "code-review", at(1), PR, cost=1.5, project_id=project,
                     summary={"title": "Old title", "verdict": "Changes Recommended",
                              "links": [{"label": "Pull request", "url": "https://github.com/acme/app/pull/12"},
                                        {"label": "Review", "url": "https://github.com/acme/app/pull/12#r1"}]},
                     trigger_ref={**ref, "head": "a" * 40})
        fix = _run(conn, own, "pr-fix", at(2), PR, cost=0.5,
                   trigger_ref={**ref, "mode": "pr", "fix": {"outcome": "pushed", "applied": 3, "deep": {"x": 1}}})
        second = _run(conn, own, "code-review", at(3), PR, cost=1.0,
                      summary={"title": "New title", "verdict": "Approval Recommended",
                               "links": [{"label": "Pull request", "url": "https://github.com/acme/app/pull/12"},
                                         {"label": "Review", "url": "https://github.com/acme/app/pull/12#r2"},
                                         {"label": "Bad", "url": "javascript:alert(1)"}]},
                      trigger_ref={**ref, "head": "b" * 40})
        _run(conn, own, "pr-merged", at(4), PR, cost=9,
             trigger_ref={**ref, "merge": {"merged_by": "dev", "transitioned": True}})
        _run(conn, own, "code-review", at(5), OTHER_PR, status="running", summary={"title": "Second PR"})
        _run(conn, own, "dev-work", at(6), ISSUE, status="failed", cost=2, summary={"title": "Add the field"})
        _run(conn, own, "code-review", at(7), None)
        _run(conn, other, "code-review", at(8), FOREIGN, summary={"title": "Rival"})
        _finding(conn, first, "k1", "keep", title="Leak", path="a.py", line=4)
        _finding(conn, second, "k1", "resolved")
        _finding(conn, second, "k2", "keep", title="New issue")
        conn.execute(insert(run_artifacts).values(run_id=first, name="review.md", media_type="text/markdown",
                                                  content="# r", size=3, created_at=NOW))
    viewer, _ = add_user(engine, keyring, clock, "vee")
    add_membership(engine, clock, viewer, "team", own, "viewer")
    project_viewer, _ = add_user(engine, keyring, clock, "pat")
    add_membership(engine, clock, project_viewer, "project", project, "viewer")
    admin, _ = add_user(engine, keyring, clock, "root", is_admin=True)
    return {"clock": clock, "own": own, "other": other, "viewer": viewer, "admin": admin,
            "project_viewer": project_viewer, "first": first, "second": second, "fix": fix}


def _client(engine, keyring, seeded, who):
    client = make_client(engine, keyring, seeded["clock"])
    sign_in_as(client, engine, keyring, seeded["clock"], seeded[who])
    return client


def _path(subject):
    return f"/api/subjects/{quote(subject, safe='')}"


def test_list_groups_runs_by_subject_newest_first(engine, keyring, seeded):
    body = _client(engine, keyring, seeded, "viewer").get("/api/subjects").json()
    rows = {row["subject"]: row for row in body["subjects"]}
    assert [row["subject"] for row in body["subjects"]] == [ISSUE, OTHER_PR, PR]
    assert body["next_before"] is None
    pr = rows[PR]
    assert pr["title"] == "New title" and pr["status"] == "merged"
    assert pr["latest_verdict"] == "Approval Recommended"
    # pr-merged is hidden by default, so it stays out of the counts and cost but still marks the status.
    assert (pr["runs"], pr["reviews"], pr["fixes"]) == (3, 2, 1)
    assert pr["cost_usd"] == pytest.approx(3.0)
    assert pr["repo"] == "acme/app" and pr["team_id"] == seeded["own"]
    assert rows[OTHER_PR]["status"] == "running"
    assert rows[ISSUE]["status"] == "failed" and rows[ISSUE]["repo"] is None


def test_status_falls_back_to_the_latest_review_verdict(engine, keyring, seeded):
    with engine.begin() as conn:
        _run(conn, seeded["own"], "code-review", at(9), OTHER_PR, summary={"verdict": "Not approved"})
    rows = {r["subject"]: r for r in _client(engine, keyring, seeded, "viewer").get("/api/subjects").json()["subjects"]}
    assert rows[OTHER_PR]["status"] == "Not approved"
    assert rows[OTHER_PR]["title"] == "Second PR"


def test_list_pages_with_a_cursor(engine, keyring, seeded):
    client = _client(engine, keyring, seeded, "viewer")
    first = client.get("/api/subjects", params={"limit": 2}).json()
    assert [r["subject"] for r in first["subjects"]] == [ISSUE, OTHER_PR]
    rest = client.get("/api/subjects", params={"limit": 2, "before": first["next_before"]}).json()
    assert [r["subject"] for r in rest["subjects"]] == [PR]
    assert rest["next_before"] is None
    assert client.get("/api/subjects", params={"before": "nonsense"}).status_code == 400


def test_search_matches_subject_or_latest_title(engine, keyring, seeded):
    client = _client(engine, keyring, seeded, "viewer")
    by_title = client.get("/api/subjects", params={"q": "NEW TITLE"}).json()["subjects"]
    assert [r["subject"] for r in by_title] == [PR]
    by_subject = client.get("/api/subjects", params={"q": "app#13"}).json()["subjects"]
    assert [r["subject"] for r in by_subject] == [OTHER_PR]
    # An older run's title matched the coarse filter, but the row's title is the latest one.
    assert client.get("/api/subjects", params={"q": "old title"}).json()["subjects"] == []
    assert client.get("/api/subjects", params={"q": "100%"}).json()["subjects"] == []


def test_search_fills_a_page_across_batches(engine, keyring, seeded):
    with engine.begin() as conn:
        for minute in range(5):
            _run(conn, seeded["own"], "code-review", at(10, minute), f"acme/app#{100 + minute}",
                 summary={"title": "Noise", "verdict": "title-ish"})
    client = _client(engine, keyring, seeded, "viewer")
    body = client.get("/api/subjects", params={"q": "title", "limit": 1}).json()
    assert [r["subject"] for r in body["subjects"]] == [PR]


def test_list_is_scoped_to_what_the_viewer_sees(engine, keyring, seeded):
    viewer = _client(engine, keyring, seeded, "viewer")
    assert FOREIGN not in {r["subject"] for r in viewer.get("/api/subjects").json()["subjects"]}
    assert viewer.get("/api/subjects", params={"team": seeded["other"]}).status_code == 404
    assert viewer.get("/api/subjects", params={"team": "missing"}).status_code == 404
    project_viewer = _client(engine, keyring, seeded, "project_viewer")
    rows = project_viewer.get("/api/subjects").json()["subjects"]
    assert [(r["subject"], r["runs"]) for r in rows] == [(PR, 1)]
    admin = _client(engine, keyring, seeded, "admin")
    narrowed = admin.get("/api/subjects", params={"team": seeded["other"]}).json()["subjects"]
    assert [r["subject"] for r in narrowed] == [FOREIGN]


def test_run_list_rows_carry_the_pull_request_state(engine, keyring, seeded):
    rows = {r["id"]: r for r in _client(engine, keyring, seeded, "viewer").get("/api/runs").json()["runs"]}
    fix = rows[seeded["fix"]]
    assert fix["title"] is None and (fix["outcome"], fix["fix_outcome"]) == (None, "pushed")
    assert fix["pr"] == {"title": "New title", "verdict": "Approval Recommended", "merged": True, "state": "merged"}
    issue = next(r for r in rows.values() if r["subject"] == ISSUE)
    assert issue["pr"] is None and issue["outcome"] is None and issue["fix_outcome"] is None
    assert FOREIGN not in {r["subject"] for r in rows.values()}
    scoped = _client(engine, keyring, seeded, "project_viewer").get("/api/runs").json()["runs"]
    assert [(r["id"], r["pr"]) for r in scoped] == [
        (seeded["first"], {"title": "Old title", "verdict": "Changes Recommended", "merged": False, "state": "open"})]


def _pulls(conn, team_id, full_name, states):
    repo = new_id()
    conn.execute(insert(gh_repos).values(id=repo, team_id=team_id, gh_repo_id=1, full_name=full_name, enabled=True,
                                         created_at=NOW, updated_at=NOW))
    for number, state in states.items():
        conn.execute(insert(gh_pull_requests).values(
            id=new_id(), team_id=team_id, repo_row_id=repo, gh_pr_id=number, number=number, author_gh_id=1,
            author_login="octo", state=state, created_at_gh=NOW, gh_updated_at=NOW, created_at=NOW, updated_at=NOW))


def test_a_pull_requests_state_takes_a_merge_run_or_its_own_teams_github_state(engine, keyring, seeded):
    with engine.begin() as conn:
        for number in (14, 15):
            _run(conn, seeded["own"], "code-review", at(9), f"acme/app#{number}")
        _pulls(conn, seeded["own"], "Acme/App", {13: "closed", 14: "open", 15: "merged"})
        _pulls(conn, seeded["other"], "acme/app", {14: "closed"})
    rows = {r["subject"]: r["pr"] for r in _client(engine, keyring, seeded, "viewer").get("/api/runs").json()["runs"]}
    names = (PR, OTHER_PR, "acme/app#14", "acme/app#15")
    assert {name: (rows[name]["state"], rows[name]["merged"]) for name in names} == {
        PR: ("merged", True), OTHER_PR: ("closed", False), "acme/app#14": ("open", False),
        "acme/app#15": ("merged", False)}

    with engine.begin() as conn:
        _run(conn, seeded["other"], "code-review", at(10), "acme/app#14")
    admin = _client(engine, keyring, seeded, "admin")
    both = admin.get("/api/runs", params={"subject": "acme/app#14"}).json()["runs"]
    assert sorted((r["team_id"] == seeded["own"], r["pr"]["state"]) for r in both) == [(False, "closed"), (True, "open")]
    done = admin.get("/api/runs", params={"subject": "acme/app#14", "pr": "done"}).json()["runs"]
    assert [r["team_id"] for r in done] == [seeded["other"]]


def test_a_streamed_run_row_has_the_listed_rows_fields_but_those_only_a_list_computes(engine, keyring, seeded):
    listed = _client(engine, keyring, seeded, "admin").get("/api/runs", params={"include_hidden": 1}).json()["runs"]
    merge = next(r for r in listed if r["workflow"] == "pr-merged")
    with engine.connect() as conn:
        streamed = list_row(load_summary(conn, merge["id"]))
    assert set(streamed) == set(merge) - {"pr", "fix_outcome", "job"}
    assert streamed["kind"] == merge["kind"] == "merge"


def _page_through(client, **params):
    first = client.get("/api/runs", params={"limit": 2, **params}).json()
    rest = client.get("/api/runs", params={"limit": 50, "before": first["next_before"], **params}).json()
    whole = client.get("/api/runs", params={"limit": 50, **params}).json()["runs"]
    assert first["next_before"] and len(whole) > 2
    return [r["id"] for r in first["runs"] + rest["runs"]], [r["id"] for r in whole]


def test_finished_lists_page_with_a_cursor(engine, keyring, seeded):
    with engine.begin() as conn:
        for minute in range(3):
            _run(conn, seeded["own"], "code-review", at(10, minute), f"acme/app#{20 + minute}")
            _run(conn, seeded["own"], "code-review", at(11, minute), f"acme/app#{30 + minute}")
            _run(conn, seeded["own"], "pr-fix", at(12, minute), f"acme/app#{30 + minute}")
        _pulls(conn, seeded["own"], "acme/app", {30: "merged", 31: "closed", 32: "merged"})
    client = _client(engine, keyring, seeded, "viewer")
    for pr in ("done", "not_done"):
        for group in (1, 0):
            paged, whole = _page_through(client, finished=1, pr=pr, group=group)
            assert paged == whole, (pr, group)


def _subjects(client, **params):
    response = client.get("/api/runs", params={"group": 1, "finished": 1, **params})
    assert response.status_code == 200, response.text
    return [r["subject"] for r in response.json()["runs"]]


def test_finished_lists_leave_out_open_threads_and_split_on_the_pull_requests_state(engine, keyring, seeded):
    client = _client(engine, keyring, seeded, "viewer")
    assert _subjects(client, pr="not_done") == [None, ISSUE]
    assert _subjects(client, pr="done") == [PR]
    assert _subjects(client, pr="not_done", status="failed") == [ISSUE]
    assert _subjects(client, pr="not_done", group=0) == [None, ISSUE]
    with engine.begin() as conn:
        _run(conn, seeded["own"], "code-review", at(9), OTHER_PR)
    assert OTHER_PR not in _subjects(client, pr="not_done") + _subjects(client, pr="done")
    assert _subjects(client, pr="not_done", group=0)[0] == OTHER_PR

    with engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.subject == OTHER_PR).values(status="succeeded"))
    assert _subjects(client, pr="not_done") == [OTHER_PR, None, ISSUE]
    with engine.begin() as conn:
        _pulls(conn, seeded["own"], "acme/app", {13: "closed"})
    assert _subjects(client, pr="done") == [OTHER_PR, PR]
    assert _subjects(client, pr="done", limit=1)[0] == OTHER_PR
    assert client.get("/api/runs", params={"pr": "open"}).status_code == 400


def test_finished_lists_never_read_another_teams_runs_or_github_state(engine, keyring, seeded):
    with engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.subject == OTHER_PR).values(status="succeeded"))
        _run(conn, seeded["other"], "pr-merged", at(9), OTHER_PR)
        _run(conn, seeded["other"], "dev-work", at(9), ISSUE, status="running")
        _pulls(conn, seeded["other"], "acme/app", {12: "closed", 13: "closed"})
    viewer = _client(engine, keyring, seeded, "viewer")
    assert _subjects(viewer, pr="not_done") == [None, ISSUE, OTHER_PR]
    assert _subjects(viewer, pr="done") == [PR]
    admin = _client(engine, keyring, seeded, "admin")
    assert ISSUE not in _subjects(admin, pr="not_done")
    assert OTHER_PR in _subjects(admin, pr="done")
    project_viewer = _client(engine, keyring, seeded, "project_viewer")
    assert (_subjects(project_viewer, pr="not_done"), _subjects(project_viewer, pr="done")) == ([PR], [])


def test_hidden_workflows_change_counts_never_access(engine, keyring, seeded):
    with engine.begin() as conn:
        conn.execute(insert(settings).values(scope=INSTANCE_SCOPE, key="display.workflow_labels",
                                             value={"pr-fix": {"label": "", "color": "", "hidden": True}},
                                             updated_at=NOW))
        conn.execute(insert(settings).values(scope=INSTANCE_SCOPE, key="display.show_cost", value=False,
                                             updated_at=NOW))
    client = _client(engine, keyring, seeded, "viewer")
    row = next(r for r in client.get("/api/subjects").json()["subjects"] if r["subject"] == PR)
    assert (row["runs"], row["fixes"], row["cost_usd"]) == (3, 0, None)
    detail = client.get(_path(PR)).json()["subject"]
    assert [r["workflow"] for r in detail["runs"]] == ["code-review", "pr-fix", "code-review", "pr-merged"]
    assert all(r["cost_usd"] is None for r in detail["runs"])


def test_a_renamed_workflow_keeps_its_kind(engine, keyring, seeded):
    with engine.begin() as conn:
        conn.execute(update(workflows).where(workflows.c.name == "code-review").values(name="pr-check"))
        conn.execute(update(workflows).where(workflows.c.name == "pr-merged").values(name="pr-landed"))
        conn.execute(update(workflows).where(workflows.c.name == "pr-fix").values(kind="other"))
    client = _client(engine, keyring, seeded, "viewer")
    row = next(r for r in client.get("/api/subjects").json()["subjects"] if r["subject"] == PR)
    assert (row["reviews"], row["fixes"], row["latest_verdict"]) == (2, 0, "Approval Recommended")
    detail = client.get(_path(PR)).json()["subject"]
    assert detail["status"] == "merged"
    assert [(r["workflow"], r["kind"]) for r in detail["runs"]] == [
        ("pr-check", "review"), ("pr-fix", "other"), ("pr-check", "review"), ("pr-landed", "merge")]


def test_a_run_with_no_workflow_row_is_other(engine, keyring, seeded):
    with engine.begin() as conn:
        _run(conn, seeded["own"], "jira-work", at(9), "PROJ-8")
    detail = _client(engine, keyring, seeded, "viewer").get(_path("PROJ-8")).json()["subject"]
    assert [(r["workflow"], r["kind"]) for r in detail["runs"]] == [("jira-work", "other")]


def test_detail_has_runs_links_and_finding_history(engine, keyring, seeded):
    detail = _client(engine, keyring, seeded, "viewer").get(_path(PR)).json()["subject"]
    assert (detail["subject"], detail["title"], detail["status"]) == (PR, "New title", "merged")
    assert (detail["repo"], detail["number"]) == ("acme/app", 12)
    assert detail["links"] == [{"label": "Pull request", "url": "https://github.com/acme/app/pull/12"},
                               {"label": "Review", "url": "https://github.com/acme/app/pull/12#r2"}]
    kinds = [(r["kind"], r["workflow_label"]) for r in detail["runs"]]
    assert kinds == [("review", "code-review"), ("fix", "pr-fix"), ("review", "code-review"),
                     ("merge", "pr-merged")]
    first, fix, second, merge = detail["runs"]
    assert first["head"] == "a" * 12 and first["artifacts"] == ["review.md"] and second["artifacts"] == []
    assert first["verdict"] == "Changes Recommended" and second["cost_usd"] == pytest.approx(1.0)
    assert fix["mode"] == "pr" and fix["fix"] == {"outcome": "pushed", "applied": 3}
    assert merge["merge"] == {"merged_by": "dev", "transitioned": True}
    by_key = {f["finding_key"]: f for f in detail["findings"]}
    assert [f["finding_key"] for f in detail["findings"]] == ["k2", "k1"]
    k1 = by_key["k1"]
    assert (k1["title"], k1["path"], k1["line"]) == ("Leak", "a.py", 4)
    assert [(h["run_id"], h["disposition"]) for h in k1["history"]] == [(seeded["first"], "keep"),
                                                                          (seeded["second"], "resolved")]
    assert k1["current_disposition"] == "resolved"
    assert by_key["k2"]["current_disposition"] == "keep"


def test_detail_carries_each_runs_outcome_and_risk_and_the_thread_takes_the_newest_runs_risk(engine, keyring, seeded):
    with engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == seeded["first"]).values(
            outcome="recommended", risk="high",
            risk_reasons=[{"source": "floor", "text": "migrations and schema: db/1.sql"}, {"source": "model"}]))
        conn.execute(update(runs).where(runs.c.id == seeded["second"]).values(outcome="approved", risk_reasons=[7]))
    detail = _client(engine, keyring, seeded, "viewer").get(_path(PR)).json()["subject"]
    first, fix, second, _ = detail["runs"]
    assert (first["outcome"], first["risk"], first["risk_reason"]) == \
        ("recommended", "high", "migrations and schema: db/1.sql")
    assert (fix["outcome"], fix["risk"], fix["risk_reason"]) == (None, None, None)
    assert (second["outcome"], second["risk"], second["risk_reason"]) == ("approved", None, None)
    assert (detail["latest_outcome"], detail["risk"]) == ("approved", None)
    with engine.begin() as conn:
        newest = conn.execute(select(runs.c.id).where(runs.c.subject == PR)
                              .order_by(runs.c.started_at.desc())).scalars().first()
        conn.execute(update(runs).where(runs.c.id == newest).values(risk="medium"))
    detail = _client(engine, keyring, seeded, "viewer").get(_path(PR)).json()["subject"]
    assert detail["risk"] == "medium"


def test_an_issue_page_lists_the_runs_on_its_pull_requests(engine, keyring, seeded):
    with engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.subject.in_([PR, OTHER_PR])).values(thread=ISSUE))
    detail = _client(engine, keyring, seeded, "viewer").get(_path(ISSUE)).json()["subject"]
    assert [r["subject"] for r in detail["runs"]] == [PR, PR, PR, PR, OTHER_PR, ISSUE]
    assert {f["finding_key"] for f in detail["findings"]} == {"k1", "k2"}
    # Its state is its own run's: the pull request merged, but the issue's work failed.
    assert (detail["status"], detail["title"], detail["repo"], detail["number"]) == ("failed", "Add the field", None, None)
    pulls = [link["url"] for link in detail["links"] if link["label"] == "Pull request"]
    assert pulls == ["https://github.com/acme/app/pull/13", "https://github.com/acme/app/pull/12"]


def test_a_thread_with_no_run_of_its_own_is_never_merged_by_its_pull_request(engine, keyring, seeded):
    with engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.subject == PR).values(thread="PROJ-9"))
    detail = _client(engine, keyring, seeded, "viewer").get(_path("PROJ-9")).json()["subject"]
    assert detail["status"] == "Approval Recommended" and detail["title"] == "New title"


def test_detail_keeps_the_newest_runs_when_there_are_too_many(engine, keyring, seeded, monkeypatch):
    monkeypatch.setattr(subjects, "RUNS_MAX", 2)
    detail = _client(engine, keyring, seeded, "viewer").get(_path(PR)).json()["subject"]
    assert [r["kind"] for r in detail["runs"]] == ["review", "merge"]
    assert detail["runs"][0]["id"] == seeded["second"]


def test_detail_is_404_without_a_visible_run(engine, keyring, seeded):
    viewer = _client(engine, keyring, seeded, "viewer")
    assert viewer.get(_path(FOREIGN)).status_code == 404
    assert viewer.get(_path("acme/app#999")).status_code == 404
    assert _client(engine, keyring, seeded, "admin").get(_path(FOREIGN)).status_code == 200
    project_viewer = _client(engine, keyring, seeded, "project_viewer")
    detail = project_viewer.get(_path(PR)).json()["subject"]
    assert [r["id"] for r in detail["runs"]] == [seeded["first"]]
    assert [f["finding_key"] for f in detail["findings"]] == ["k1"]


def test_subject_with_hash_and_slashes_round_trips(engine, keyring, seeded):
    odd = "group/sub/repo#5"
    with engine.begin() as conn:
        _run(conn, seeded["own"], "code-review", at(11), odd, summary={"title": "Nested"})
    client = _client(engine, keyring, seeded, "viewer")
    detail = client.get(_path(odd)).json()["subject"]
    assert detail["subject"] == odd and detail["repo"] == "group/sub/repo" and detail["number"] == 5
    # The UI sends slashes literally, because Apache refuses %2F in a path.
    assert client.get("/api/subjects/group/sub/repo%235").json()["subject"]["subject"] == odd
    assert client.get(_path(ISSUE)).json()["subject"]["number"] is None


def test_merged_links_skip_unsafe_and_repeated_entries():
    class Run:
        def __init__(self, summary):
            self.summary = summary
            self.subject = None

    ordered = [Run({"post_url": "https://x/old"}), Run({"links": "not a list", "post_url": "http://x/insecure"}),
               Run({"links": [{"label": "PR", "url": "https://x/pr"}, {"label": "PR", "url": "https://x/other"},
                              {"url": "https://x/no-label"}]})]
    assert subjects.merged_links(ordered) == [{"label": "PR", "url": "https://x/pr"},
                                              {"label": "Posted result", "url": "https://x/old"}]


def test_a_thread_shared_with_another_team_never_leaks_its_runs_links_or_findings(engine, keyring, seeded):
    with engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.subject == PR).values(thread=ISSUE))
        rival = _run(conn, seeded["other"], "code-review", at(9), "rival/app#2", summary={
            "title": "Rival secret", "links": [{"label": "Pull request", "url": "https://github.com/rival/app/pull/2"}]})
        conn.execute(update(runs).where(runs.c.id == rival).values(thread=ISSUE))
        _run(conn, seeded["other"], "dev-work", at(9, 30), ISSUE, summary={"title": "Rival issue"})
        _finding(conn, rival, "rk", "keep", title="Rival finding")
    for who in ("viewer", "project_viewer"):
        detail = _client(engine, keyring, seeded, who).get(_path(ISSUE)).json()["subject"]
        assert {r["subject"] for r in detail["runs"]} <= {PR, ISSUE}
        assert "rival" not in str(detail).lower()
    viewer = _client(engine, keyring, seeded, "viewer").get(_path(ISSUE)).json()["subject"]
    assert viewer["title"] != "Rival issue" and len(viewer["runs"]) == 5
    project = _client(engine, keyring, seeded, "project_viewer").get(_path(ISSUE)).json()["subject"]
    assert [r["id"] for r in project["runs"]] == [seeded["first"]]


def test_list_rows_name_the_author_and_the_latest_runs_environment(engine, keyring, seeded):
    import json

    from armature.server.db.schema import jobs, webhook_deliveries
    from armature.server.db.schema import webhooks as webhooks_table
    hook, delivery = new_id(), new_id()
    payload = {"action": "opened", "pull_request": {"number": 13, "user": {"login": "octocat"}}}
    with engine.begin() as conn:
        conn.execute(insert(webhooks_table).values(id=hook, team_id=seeded["own"], name="gh", source="github",
                                                   path_token=new_id(), secret_name="HOOK", created_at=NOW,
                                                   updated_at=NOW))
        conn.execute(insert(webhook_deliveries).values(id=delivery, webhook_id=hook, received_at=NOW, status="accepted",
                                                       payload=json.dumps(payload)))
        latest = _run(conn, seeded["own"], "code-review", at(9), OTHER_PR, status="running",
                      trigger_ref={"delivery_id": delivery})
        conn.execute(update(runs).where(runs.c.id == latest).values(
            location={"kind": "aws-linux", "label": "AWS Linux", "instance": "i-1"}))
        workflow = conn.execute(select(workflows.c.id).where(workflows.c.name == "code-review")).scalar()
        conn.execute(insert(jobs).values(id=new_id(), team_id=seeded["own"], workflow_id=workflow, run_id=latest,
                                         status="running", created_at=NOW))
    client = _client(engine, keyring, seeded, "viewer")
    rows = {r["subject"]: r for r in client.get("/api/subjects").json()["subjects"]}
    assert rows[OTHER_PR]["author"] == "octocat"
    assert rows[OTHER_PR]["location"] == {"kind": "aws-linux", "label": "AWS Linux", "instance": "i-1"}
    assert rows[PR]["author"] is None and rows[PR]["location"] is None
    page = client.get(_path(OTHER_PR)).json()["subject"]
    [current] = [run for run in page["runs"] if run["id"] == latest]
    assert current["job"]["status"] == "running" and current["job"]["cancel_requested_at"] is None
    assert current["team_id"] == seeded["own"]
    assert all(run["job"] is None for run in page["runs"] if run["id"] != latest)
    listed = {r["id"]: r for r in client.get("/api/runs?every=1").json()["runs"]}
    assert listed[latest]["job"]["status"] == "running" and listed[seeded["first"]]["job"] is None


def _poller(conn, team_id, workflow, *, project_id=None, enabled=True, **config):
    trigger_id = new_id()
    workflow_id = conn.execute(select(workflows.c.id).where(workflows.c.name == workflow)).scalar()
    conn.execute(insert(triggers).values(id=trigger_id, team_id=team_id, workflow_id=workflow_id, kind="jira_poll",
                                         enabled=enabled, created_at=NOW,
                                         config={"jql": "project = SECRET", "connection_id": new_id(),
                                                 "project_id": project_id, **config}))
    conn.execute(insert(trigger_state).values(trigger_id=trigger_id, last_run_at=at(9), next_run_at=at(9, 5),
                                              failures=0))
    return trigger_id


def test_an_issue_lists_the_pollers_that_may_still_pick_it_up_in_scope(engine, keyring, seeded):
    with engine.begin() as conn:
        project = conn.execute(select(projects.c.id).where(projects.c.name == "app")).scalar()
        hidden = new_id()
        conn.execute(insert(projects).values(id=hidden, team_id=seeded["own"], name="hidden", created_at=NOW))
        for name in ("bug-triage", "bug-fix", "bug-ship"):
            conn.execute(insert(workflows).values(id=new_id(), name=name, kind="work", current_version=1,
                                                  created_at=NOW, updated_at=NOW))
        _run(conn, seeded["own"], "bug-triage", at(8), None, project_id=project)
        _run(conn, seeded["own"], "dev-work", at(8, 30), None, project_id=hidden)
        conn.execute(update(runs).where(runs.c.subject.is_(None), runs.c.project_id.is_not(None)).values(thread="PROJ-9"))
        _poller(conn, seeded["own"], "bug-triage", project_id=project, interval_minutes=15)
        _poller(conn, seeded["own"], "bug-fix", project_id=project, interval_minutes=5)
        _poller(conn, seeded["own"], "bug-ship", project_id=hidden)
        _poller(conn, seeded["own"], "pr-fix", project_id=project, shadow=True)
        _poller(conn, seeded["own"], "pr-merged", project_id=project, enabled=False)
        _poller(conn, seeded["other"], "code-review", project_id=project)
        seen = _poller(conn, seeded["own"], "code-review", project_id=project)
        conn.execute(insert(trigger_seen).values(trigger_id=seen, item_key="PROJ-9", outcome="queued",
                                                 first_seen_at=NOW, last_seen_at=NOW))
        _poller(conn, seeded["own"], "code-review", project_id=new_id())
    team = _client(engine, keyring, seeded, "viewer").get(_path("PROJ-9")).json()["subject"]
    assert [r["workflow"] for r in team["runs"]] == ["bug-triage", "dev-work"]
    assert team["watching"] == [
        {"workflow": "bug-fix", "workflow_label": "bug-fix", "source": "jira", "interval_minutes": 5,
         "last_poll_at": at(9).isoformat().replace("+00:00", "Z"),
         "next_poll_at": at(9, 5).isoformat().replace("+00:00", "Z")},
        {"workflow": "bug-ship", "workflow_label": "bug-ship", "source": "jira", "interval_minutes": 5,
         "last_poll_at": at(9).isoformat().replace("+00:00", "Z"),
         "next_poll_at": at(9, 5).isoformat().replace("+00:00", "Z")}]
    narrow = _client(engine, keyring, seeded, "project_viewer").get(_path("PROJ-9")).json()["subject"]
    assert [r["workflow"] for r in narrow["runs"]] == ["bug-triage"]
    assert [w["workflow"] for w in narrow["watching"]] == ["bug-fix"]
    pull = _client(engine, keyring, seeded, "viewer").get(_path(PR)).json()["subject"]
    assert pull["watching"] == []

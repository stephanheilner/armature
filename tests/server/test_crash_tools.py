"""crash-watch's tools: Crashlytics clusters from BigQuery, Jira search and the crash create, release containment,
and the broker's daily cap on crash issues."""
from __future__ import annotations

import base64
import json
import re
import threading
from datetime import datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import insert, update

from armature.server import broker, connections
from armature.server.db import new_id
from armature.server.db.schema import connections as connections_table
from armature.server.db.schema import projects, settings, tool_calls
from armature.server.tools import github as github_tools
from armature.server.vendors import gcp
from tests.server.tool_support import (  # noqa: F401
    REPO,
    app,
    calls,
    clock,
    dns,
    fresh_members,
    grant,
    pem,
    start_run,
    tool,
    use,
    vendor,
    world,
)

ISSUE_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
LABEL = f"crashlytics-{ISSUE_ID}"
MERGE = "c" * 40
COLUMNS = ["issue_id", "users", "events", "first_seen", "last_seen", "symbol", "file", "line", "library", "version",
           "version_users", "version_last_seen"]
TYPES = ["STRING", "INTEGER", "INTEGER", "TIMESTAMP", "TIMESTAMP", "STRING", "STRING", "INTEGER", "STRING", "STRING",
         "INTEGER", "TIMESTAMP"]
MICROS = 1_761_000_000_000_000


@pytest.fixture
def run(sqlite_engine, keyring, clock, world):
    return start_run(sqlite_engine, keyring, clock, world)


def _setting(engine, key, value, world):
    with engine.begin() as conn:
        conn.execute(insert(settings).values(team_id=world.team, project_id=world.project, key=key, value=value))


@pytest.fixture
def jira_project(sqlite_engine, world):
    _setting(sqlite_engine, "jira.project", "MEMTEMDIS", world)
    with sqlite_engine.begin() as conn:
        conn.execute(update(connections_table).where(connections_table.c.id == world.jira)
                     .values(identity={"account_id": "acct-1", "display": "Engineer"}))


@pytest.fixture
def gcp_connection(sqlite_engine, keyring, clock, world, pem):
    key = gcp.service_account(json.dumps({"type": "service_account", "private_key": pem, "token_uri": "https://evil",
                                          "client_email": "crash-watch@proj-1234.iam.gserviceaccount.com"}))
    row_id = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(connections_table).values(
            id=row_id, team_id=world.team, project_id=world.project, kind="gcp", name="crashlytics",
            config={"project_id": "proj-1234", "dataset": "firebase_crashlytics", "table": "app_IOS"}, state="ok",
            identity={}, key_id=keyring.key_id, created_at=clock(), updated_at=clock(),
            sealed=keyring.seal(json.dumps({"service_account_json": key}), connections.sealed_aad(row_id, "gcp"),
                                label=connections.LABEL)))
    return row_id


def _row(*values):
    return {"f": [{"v": v} for v in values]}


def _bigquery(rows):
    return {"jobComplete": True, "schema": {"fields": [{"name": n, "type": t} for n, t in zip(COLUMNS, TYPES)]},
            "rows": rows}


def _claims(jwt: str) -> dict:
    body = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


# Crashlytics.


def test_clusters_query_only_allowlisted_columns_of_the_connections_table(app, run, vendor, gcp_connection):
    vendor.bigquery = _bigquery([
        _row(ISSUE_ID, "40", "90", str(MICROS), str(MICROS + 5), "-[TreeView load]", "TreeView.m", "88", "Tree",
             "5.7.1", "30", str(MICROS + 5)),
        _row(ISSUE_ID, "40", "90", str(MICROS), str(MICROS + 5), "-[TreeView load]", "TreeView.m", "88", "Tree",
             "5.7.0", "12", str(MICROS)),
        _row("bad id!", "20", "20", str(MICROS), str(MICROS), "x", "y", "1", "z", "5.7.1", "20", str(MICROS))])
    response = tool(app, run, "clusters", "crashlytics.clusters", {})
    assert response.status_code == 200, response.text
    [cluster] = response.json()["clusters"]
    assert response.json()["labels"] == [LABEL] and response.json()["min_users"] == 10
    assert datetime.fromisoformat(response.json()["as_of"]).tzinfo is not None
    assert cluster["label"] == LABEL and cluster["users"] == 40 and cluster["title"] == "-[TreeView load]"
    assert [(v["version"], v["users"]) for v in cluster["versions"]] == [("5.7.1", 30), ("5.7.0", 12)]
    token, query = vendor.requests
    form = parse_qs(token.content.decode())
    assert (token.headers["host"], token.url.path) == ("oauth2.googleapis.com", "/token")
    assert form["grant_type"] == [gcp.GRANT]
    claims = _claims(form["assertion"][0])
    assert claims["scope"] == gcp.SCOPE and claims["aud"] == gcp.TOKEN_URL
    assert claims["iss"] == "crash-watch@proj-1234.iam.gserviceaccount.com"
    assert query.url.path == "/bigquery/v2/projects/proj-1234/queries"
    assert query.headers["authorization"] == "Bearer ya29.harness"
    body = vendor.body(query)
    sql = body["query"]
    assert "FROM `proj-1234.firebase_crashlytics.app_IOS`" in sql and "_REALTIME" not in sql
    assert "event_timestamp >= TIMESTAMP_SUB(@as_of, INTERVAL @days DAY)" in sql and "error_type = 'FATAL'" in sql
    assert "HAVING users >= @min_users" in sql
    for column in ("exceptions", "logs", "breadcrumbs", "custom_keys", "issue_title", "issue_subtitle", "user",
                   "device", "memory", "storage", "threads", "operating_system", "crashlytics_sdk_version"):
        assert not re.search(rf"\b{column}\b", sql), column
    read = set(re.findall(r"\b(?:blame_frame|application)\.\w+", sql))
    assert read == {"blame_frame.symbol", "blame_frame.file", "blame_frame.line", "blame_frame.library",
                    "application.display_version"}
    assert body["maximumBytesBilled"] == str(10 * 1000 ** 3) and body["useLegacySql"] is False
    params = {p["name"]: p["parameterValue"]["value"] for p in body["queryParameters"]}
    assert params["days"] == "7" and params["min_users"] == "10" and params["top"] == "20"


def test_a_failed_query_fails_the_step_and_reads_need_the_projects_gcp_connection(app, run, vendor, gcp_connection,
                                                                                 sqlite_engine):
    vendor.fail = ("/queries",)
    assert tool(app, run, "clusters", "crashlytics.clusters", {}).status_code == 502
    vendor.fail = ()
    vendor.bigquery = {"jobComplete": False}
    assert tool(app, run, "clusters", "crashlytics.clusters", {}).status_code == 502
    vendor.bigquery = {**_bigquery([]), "pageToken": "next"}
    assert tool(app, run, "clusters", "crashlytics.clusters", {}).status_code == 502
    vendor.bigquery = _bigquery([])
    assert tool(app, run, "clusters", "crashlytics.clusters", {}).json()["clusters"] == []
    with sqlite_engine.begin() as conn:
        conn.execute(connections_table.delete().where(connections_table.c.id == gcp_connection))
    assert tool(app, run, "clusters", "crashlytics.clusters", {}).status_code == 409


def test_a_refused_token_fails_the_clusters_step(app, run, vendor, gcp_connection, monkeypatch):
    real = type(vendor).__call__
    monkeypatch.setattr(type(vendor), "__call__", lambda self, request: httpx.Response(401, json={"error": "x"})
                        if request.url.path == "/token" else real(self, request))
    response = tool(app, run, "clusters", "crashlytics.clusters", {})
    assert response.status_code == 502 and "HTTP 401" in response.text
    assert not [r for r in vendor.requests if r.url.path.endswith("/queries")]


def test_a_service_account_key_keeps_only_what_signing_needs(pem):
    stored = json.loads(gcp.service_account(json.dumps({
        "type": "service_account", "private_key": pem, "client_email": "a-b@proj-1234.iam.gserviceaccount.com",
        "token_uri": "https://evil.example/token", "client_id": "1"})))
    assert set(stored) == {"type", "private_key", "client_email"}
    with pytest.raises(gcp.VendorError):
        gcp.service_account(json.dumps({"type": "authorized_user", "private_key": pem}))


# Jira search and create.


def _issue(key, status="done", resolution="Fixed", resolved="2026-10-01T10:00:00.000-0600", comments=(),
           author="acct-1"):
    return {"key": key, "fields": {
        "summary": "Crash", "labels": [LABEL], "status": {"name": "Closed", "statusCategory": {"key": status}},
        "resolution": {"name": resolution} if resolution else None, "resolutiondate": resolved,
        "comment": {"comments": [{"author": {"accountId": author}, "body": {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}]}} for text in comments]}}}


def test_jira_search_asks_for_the_labels_in_the_policy_project_only(app, run, vendor, jira_project):
    vendor.jira_found = [_issue("MEMTEMDIS-5", comments=["Merged fs-eng/mobile-ios#812 by me."])]
    response = tool(app, run, "search", "jira.search", {"labels": [LABEL, "crashlytics-ff"]})
    assert response.status_code == 200, response.text
    [request] = vendor.requests
    query = vendor.query(request)
    assert query["jql"] == f'project = MEMTEMDIS AND labels in ("{LABEL}", "crashlytics-ff") ORDER BY created DESC'
    assert set(query["fields"].split(",")) == {"summary", "status", "resolution", "resolutiondate", "labels",
                                               "created", "comment"}
    [found] = response.json()["issues"]
    assert found["merged_prs"] == [{"repo": "fs-eng/mobile-ios", "number": 812}]
    assert (found["status_category"], found["resolution"]) == ("done", "Fixed")


def test_a_merged_comment_from_another_author_names_no_fix(app, run, vendor, jira_project):
    vendor.jira_found = [_issue("MEMTEMDIS-5", comments=["Merged fs-eng/mobile-ios#666"], author="someone-else")]
    [found] = tool(app, run, "search", "jira.search", {"labels": [LABEL]}).json()["issues"]
    assert found["merged_prs"] == []


def test_a_search_with_no_labels_asks_jira_nothing(app, run, vendor, jira_project):
    assert tool(app, run, "search", "jira.search", {"labels": []}).json() == {"issues": [], "count": 0}
    assert not vendor.requests


@pytest.mark.parametrize("labels", [["intake"], ["crashlytics-x) OR project = EVIL"], ["crashlytics-"]])
def test_jira_search_takes_only_crashlytics_labels(app, run, vendor, jira_project, labels):
    assert tool(app, run, "search", "jira.search", {"labels": labels}).status_code == 400 and not vendor.requests


@pytest.mark.parametrize("answer", [{"total": 0}, {"issues": [{"fields": {}}]}])
def test_a_malformed_search_answer_fails_closed(app, run, vendor, jira_project, monkeypatch, answer):
    real = vendor.__call__
    monkeypatch.setattr(type(vendor), "__call__", lambda self, request: httpx.Response(200, json=answer)
                        if request.url.path.endswith("/search/jql") else real(request))
    assert tool(app, run, "search", "jira.search", {"labels": [LABEL]}).status_code == 502


CRASH = {"type": "Defect", "summary": "Crash: -[TreeView load]", "description": "Blamed frame", "labels": [LABEL],
         "assign_to_me": True, "link": "MEMTEMDIS-5"}


def test_a_crash_issue_goes_out_in_one_post_with_its_sprint_last(app, sqlite_engine, run, vendor, world, jira_project):
    _setting(sqlite_engine, "jira.components", ["Mobile iOS"], world)
    _setting(sqlite_engine, "jira.board", 3887, world)
    vendor.jira_found = [_issue("MEMTEMDIS-5")]
    response = tool(app, run, "create", "jira.create", CRASH)
    assert response.status_code == 200, response.text
    assert response.json()["issue"] == "MOB-99" and response.json()["sprint"] == 41
    assert [(r.method, r.url.path) for r in vendor.requests] == [
        ("GET", "/rest/api/3/search/jql"), ("GET", "/rest/agile/1.0/board/3887/sprint"),
        ("POST", "/rest/api/3/issue"), ("POST", "/rest/agile/1.0/sprint/41/issue")]
    body = vendor.body(vendor.requests[2])
    fields = body["fields"]
    assert fields["components"] == [{"name": "Mobile iOS"}] and fields["assignee"] == {"accountId": "acct-1"}
    assert fields["labels"] == [LABEL] and fields["project"] == {"key": "MEMTEMDIS"}
    assert body["update"] == {"issuelinks": [{"add": {"type": {"name": "Relates"},
                                                      "outwardIssue": {"key": "MEMTEMDIS-5"}}}]}
    assert vendor.query(vendor.requests[1]) == {"state": "active"}
    assert vendor.body(vendor.requests[3]) == {"issues": ["MOB-99"]}
    [call] = calls(sqlite_engine)
    assert (call.outcome, call.target, call.detail) == ("ok", LABEL, {"issue": "MOB-99"})


@pytest.mark.parametrize("sprints", [[], [{"id": 41}, {"id": 42}]])
def test_zero_or_several_active_sprints_file_nothing_and_leave_the_cap_free(app, sqlite_engine, run, vendor, world,
                                                                             jira_project, sprints):
    _setting(sqlite_engine, "jira.board", 3887, world)
    vendor.sprints = sprints
    assert tool(app, run, "create", "jira.create", CRASH).status_code == 409
    assert not vendor.writes() and calls(sqlite_engine)[-1].outcome == "refused"
    vendor.sprints = [{"id": 41}]
    for label in (LABEL, "crashlytics-b2"):
        assert tool(app, run, "create", "jira.create", {**CRASH, "labels": [label]}).json()["created"] is True


def test_a_failed_sprint_move_names_the_issue_it_left_behind(app, sqlite_engine, run, vendor, world, jira_project):
    _setting(sqlite_engine, "jira.board", 3887, world)
    vendor.fail = ("/sprint/41/issue",)
    response = tool(app, run, "create", "jira.create", CRASH)
    assert response.status_code == 502 and "Created MOB-99" in response.text and "sprint 41" in response.text
    call = calls(sqlite_engine)[-1]
    assert call.outcome == "failed" and "MOB-99" in call.detail["error"]


def test_a_failed_label_search_before_the_post_files_nothing(app, sqlite_engine, run, vendor, jira_project):
    vendor.fail = ("/search/jql",)
    assert tool(app, run, "create", "jira.create", CRASH).status_code == 502
    assert not vendor.writes() and calls(sqlite_engine)[-1].outcome == "failed"


def test_assign_to_me_needs_the_connections_account(app, sqlite_engine, run, vendor, world, jira_project):
    with sqlite_engine.begin() as conn:
        conn.execute(update(connections_table).where(connections_table.c.id == world.jira).values(identity={}))
    assert tool(app, run, "create", "jira.create", CRASH).status_code == 409 and not vendor.writes()


@pytest.mark.parametrize("key,value", [("jira.components", "Mobile iOS"), ("jira.components", [""]),
                                       ("jira.board", "3887"), ("jira.board", True), ("jira.board", 0)])
def test_malformed_crash_settings_are_refused(app, sqlite_engine, run, vendor, world, jira_project, key, value):
    _setting(sqlite_engine, key, value, world)
    assert tool(app, run, "create", "jira.create", CRASH).status_code == 409 and not vendor.requests


def test_an_issue_without_a_crash_label_ignores_the_crash_settings(app, sqlite_engine, run, vendor, world,
                                                                   jira_project):
    _setting(sqlite_engine, "jira.components", ["Mobile iOS"], world)
    _setting(sqlite_engine, "jira.board", 3887, world)
    vendor.sprints = []
    response = tool(app, run, "create", "jira.create", {"type": "Task", "summary": "Intake", "labels": ["intake"]})
    assert response.status_code == 200 and "sprint" not in response.json()
    [request] = vendor.requests
    assert request.url.path == "/rest/api/3/issue" and "components" not in vendor.body(request)["fields"]
    assert calls(sqlite_engine)[-1].target is None


def test_an_open_issue_with_the_label_files_nothing(app, sqlite_engine, run, vendor, jira_project):
    vendor.jira_found = [_issue("MEMTEMDIS-7", status="indeterminate", resolution=None)]
    response = tool(app, run, "create", "jira.create", CRASH)
    assert response.json() == {"issue": None, "created": False, "reason": "MEMTEMDIS-7 already tracks this crash"}
    assert not vendor.writes() and calls(sqlite_engine)[-1].outcome == "refused"


def test_a_link_goes_only_to_the_policy_project(app, sqlite_engine, run, vendor, jira_project):
    assert tool(app, run, "create", "jira.create", {**CRASH, "link": "EVIL-1"}).status_code == 403
    assert not vendor.writes()


def test_the_cap_refuses_a_third_crash_issue_and_the_same_label_twice(app, sqlite_engine, keyring, run, vendor, world,
                                                                      jira_project, clock):
    vendor.fail = ("/rest/api/3/issue",)
    assert tool(app, run, "create", "jira.create", CRASH).status_code == 502
    vendor.fail = ()
    again = tool(app, run, "create", "jira.create", CRASH).json()
    assert again["created"] is False and "already filed today" in again["reason"]
    assert tool(app, run, "create", "jira.create", {**CRASH, "labels": ["crashlytics-b2"]}).json()["created"]
    third = tool(app, run, "create", "jira.create", {**CRASH, "labels": ["crashlytics-c3"]}).json()
    assert third["created"] is False and "cap of 2" in third["reason"]
    assert [c.outcome for c in calls(sqlite_engine)] == ["failed", "refused", "ok", "refused"]
    clock.advance(hours=20, seconds=1)
    later = start_run(sqlite_engine, keyring, clock, world)
    assert tool(app, later, "create", "jira.create", {**CRASH, "labels": ["crashlytics-c3"]}).json()["created"]


def test_a_plain_create_is_not_capped(app, sqlite_engine, run, vendor, jira_project):
    plain = {"type": "Task", "summary": "Follow up", "labels": ["intake"]}
    for _ in range(3):
        assert tool(app, run, "create", "jira.create", plain).json()["created"] is True
    assert [r.url.path for r in vendor.requests] == ["/rest/api/3/issue"] * 3


def test_concurrent_runs_file_at_most_two_crash_issues(sqlite_engine, keyring, clock, world, vendor, app,
                                                       jira_project, monkeypatch):
    locked, real = [], github_tools.key_lock
    monkeypatch.setattr(github_tools, "key_lock", lambda key: locked.append(key) or real(key))
    token = grant(app, start_run(sqlite_engine, keyring, clock, world), "create")
    barrier, results = threading.Barrier(5), []

    def file(label):
        barrier.wait()
        results.append(broker.call_tool(sqlite_engine, keyring, public_url="https://arm.test", token=token,
                                        tool="jira.create", params={**CRASH, "labels": [label]}, now=clock(), ip=None,
                                        transport=httpx.MockTransport(vendor)))

    threads = [threading.Thread(target=file, args=(f"crashlytics-x{n}",)) for n in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(r["created"] for r in results) == [False, False, False, True, True]
    assert locked == [f"crash:{world.project}"] * 5
    assert len(vendor.writes("POST")) == 2


def test_a_dry_run_records_a_crash_create_without_counting_it(app, sqlite_engine, keyring, clock, world, vendor,
                                                              jira_project):
    dry = start_run(sqlite_engine, keyring, clock, world, trigger_ref={"dry_run": True})
    for n in range(3):
        assert tool(app, dry, "create", "jira.create", {**CRASH, "labels": [f"crashlytics-d{n}"]}).json()["dry_run"]
    assert not vendor.requests and {c.mode for c in calls(sqlite_engine)} == {"dry"}
    live = start_run(sqlite_engine, keyring, clock, world)
    assert tool(app, live, "create", "jira.create", {**CRASH, "labels": ["crashlytics-d0"]}).json()["created"]


def _filed(engine, run, world, project, label, at):
    with engine.begin() as conn:
        conn.execute(insert(tool_calls).values(id=new_id(), run_id=run.id, team_id=world.team, project_id=project,
                                               step_id="create", tool="jira.create", mode="live", outcome="ok",
                                               target=label, created_at=at))


@pytest.mark.parametrize("offset,created", [(timedelta(0), False), (timedelta(microseconds=1), True)])
def test_the_cap_window_is_20_hours_inclusive(app, sqlite_engine, run, vendor, world, jira_project, clock, offset,
                                              created):
    at = clock() - broker.CRASH_WINDOW - offset
    for label in ("crashlytics-e1", "crashlytics-e2"):
        _filed(sqlite_engine, run, world, world.project, label, at)
    assert tool(app, run, "create", "jira.create", CRASH).json()["created"] is created


def test_another_projects_crash_issues_dont_count(app, sqlite_engine, run, vendor, world, jira_project, clock):
    other = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(projects).values(id=other, team_id=world.team, name="android", repo="Acme/Droid",
                                             created_at=clock()))
    for label in ("crashlytics-o1", "crashlytics-o2", LABEL):
        _filed(sqlite_engine, run, world, other, label, clock())
    assert tool(app, run, "create", "jira.create", CRASH).json()["created"] is True


# GitHub release containment.


def _tags(vendor):
    vendor.fix_prs = {812: {"number": 812, "merged": True, "merge_commit_sha": MERGE,
                            "head": {"ref": "memtemdis-5-tree-crash"}, "title": "Fix the tree crash",
                            "merged_at": "2026-09-30T10:00:00Z"}}
    vendor.tags = {"familytree_5.7.1": {"behind_by": 0, "date": "2026-10-05T12:00:00Z"},
                   "familytree_5.7.0": {"behind_by": 3, "date": "2026-09-20T12:00:00Z"}}


def test_release_contains_compares_each_versions_tag_with_the_merge_commit(app, run, vendor):
    _tags(vendor)
    response = tool(app, run, "contains", "github.release-contains",
                    {"repo": REPO, "pr": 812, "issue": "MEMTEMDIS-5", "tag_prefix": "familytree_",
                     "versions": ["5.7.1", "5.7.0", "5.6.9"]})
    assert response.status_code == 200, response.text
    found = response.json()
    assert (found["merged"], found["pr"], found["merge_commit_sha"]) == (True, 812, MERGE)
    assert [(v["version"], v["contained"], v["tag_date"]) for v in found["versions"]] == [
        ("5.7.1", True, "2026-10-05T12:00:00Z"), ("5.7.0", False, None), ("5.6.9", None, None)]
    paths = [r.url.path for r in vendor.requests if "/compare/" in r.url.path]
    assert paths == [f"/repos/{REPO}/compare/{MERGE}...familytree_{v}" for v in ("5.7.1", "5.7.0", "5.6.9")]
    dated = [r.url.path for r in vendor.requests if "/commits/" in r.url.path]
    assert dated == [f"/repos/{REPO}/commits/familytree_5.7.1"]


def test_release_contains_finds_the_fix_on_the_issues_branch_and_needs_a_merge(app, run, vendor):
    _tags(vendor)
    vendor.branch_fixes = {"armature/MEMTEMDIS-5": [{"number": 812, "merged_at": "2026-09-30T10:00:00Z",
                                                      "merge_commit_sha": MERGE},
                                                     {"number": 800, "merged_at": None}]}
    found = tool(app, run, "contains", "github.release-contains",
                 {"repo": REPO, "pr": None, "issue": "MEMTEMDIS-5", "versions": ["5.7.1"],
                  "tag_prefix": "familytree_"}).json()
    assert found["pr"] == 812 and found["versions"][0]["contained"] is True
    [lookup] = [r for r in vendor.requests if r.url.path == f"/repos/{REPO}/pulls"]
    assert vendor.query(lookup)["head"] == "Acme:armature/MEMTEMDIS-5" and vendor.query(lookup)["state"] == "closed"
    vendor.fix_prs[812]["merged"] = False
    unmerged = tool(app, run, "contains", "github.release-contains",
                    {"repo": REPO, "pr": 812, "versions": ["5.7.1"], "tag_prefix": "familytree_"}).json()
    assert unmerged["merged"] is False and unmerged["versions"] == []


def test_release_contains_reads_only_the_projects_own_repository(app, run, vendor):
    response = tool(app, run, "contains", "github.release-contains",
                    {"repo": "Acme/Other", "pr": 1, "versions": [], "tag_prefix": "x"})
    assert response.status_code == 403 and not vendor.requests


def test_a_merged_comment_naming_an_unrelated_pull_request_is_no_fix(app, run, vendor):
    _tags(vendor)
    vendor.fix_prs[812].update(head={"ref": "memtemdis-51-other"}, title="Fix MEMTEMDIS-50 instead")
    found = tool(app, run, "contains", "github.release-contains",
                 {"repo": REPO, "pr": 812, "issue": "MEMTEMDIS-5", "versions": ["5.7.1"],
                  "tag_prefix": "familytree_"}).json()
    assert found["merged"] is False and found["versions"] == []
    vendor.fix_prs[812].update(title="MEMTEMDIS-5: fix the tree crash")
    found = tool(app, run, "contains", "github.release-contains",
                 {"repo": REPO, "pr": 812, "issue": "MEMTEMDIS-5", "versions": ["5.7.1"],
                  "tag_prefix": "familytree_"}).json()
    assert found["merged"] is True

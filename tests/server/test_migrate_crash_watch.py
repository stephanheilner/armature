"""crash-watch imported for mobile-ios: a disabled daily schedule, the Jira settings it files with, and a run that
files a regression and a new crash from fake Crashlytics, Jira, and GitHub answers."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select

from armature.server.db import new_id
from armature.server.db.schema import projects, settings, teams, triggers, workflows
from armature.server.migrate_ops import MigrateError, import_ops
from armature.server.vendors import gcp
from armature.server.vendors import github as github_vendor
from tests.server import ops_harness
from tests.server.tool_support import Vendors

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
MERGE = "c" * 40
COLUMNS = [("issue_id", "STRING"), ("users", "INTEGER"), ("events", "INTEGER"), ("first_seen", "TIMESTAMP"),
           ("last_seen", "TIMESTAMP"), ("symbol", "STRING"), ("file", "STRING"), ("line", "INTEGER"),
           ("library", "STRING"), ("version", "STRING"), ("version_users", "INTEGER"),
           ("version_last_seen", "TIMESTAMP")]


@pytest.fixture
def fs(sqlite_engine):
    team, project, other = new_id(), new_id(), new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(teams).values(id=team, name="FamilySearch", slug="familysearch", created_at=NOW))
        conn.execute(insert(projects).values([
            {"id": project, "team_id": team, "name": "mobile-ios", "repo": "fs-eng/mobile-ios", "created_at": NOW},
            {"id": other, "team_id": team, "name": "android-tree", "repo": "fs-eng/android-tree", "created_at": NOW}]))
        changes, notes = import_ops(conn, "crash-watch", team_id=team, project_id=project, now=NOW)
        workflow = conn.execute(select(workflows.c.id).where(workflows.c.project_id == project)).scalar_one()
    return {"team": team, "project": project, "other": other, "workflow": workflow, "changes": changes,
            "notes": notes}


def test_mobile_ios_gets_a_disabled_daily_schedule_and_the_jira_settings(sqlite_engine, fs):
    with sqlite_engine.connect() as conn:
        [trigger] = conn.execute(select(triggers)).all()
        found = dict(conn.execute(select(settings.c.key, settings.c.value)
                                  .where(settings.c.scope == fs["project"])).all())
    assert trigger.kind == "schedule" and not trigger.enabled
    assert (trigger.config["cron"], trigger.config["timezone"]) == ("30 6 * * *", "America/Denver")
    assert not trigger.config.get("inputs")
    assert found == {"jira.components": ["Mobile iOS"], "jira.board": 3887}
    assert any("GCP connection" in note for note in fs["notes"]) and any("jira.project" in n for n in fs["notes"])


def test_crash_watch_runs_only_on_mobile_ios(sqlite_engine, fs):
    with sqlite_engine.begin() as conn, pytest.raises(MigrateError, match="only on fs-eng/mobile-ios"):
        import_ops(conn, "crash-watch", team_id=fs["team"], project_id=fs["other"], now=NOW)


def test_a_second_import_changes_nothing(sqlite_engine, fs):
    with sqlite_engine.begin() as conn:
        changes, _ = import_ops(conn, "crash-watch", team_id=fs["team"], project_id=fs["project"], now=NOW)
    assert {c.action for c in changes} == {"unchanged"}


def _micros(text: str) -> str:
    return str(int(datetime.fromisoformat(text).timestamp() * 1_000_000))


def _row(issue_id, users, symbol, version, seen):
    values = [issue_id, str(users), str(users * 2), _micros("2026-11-02T10:00:00+00:00"), _micros(seen), symbol,
              "Tree.m", "88", "Tree", version, str(users), _micros(seen)]
    return {"f": [{"v": v} for v in values]}


def _issue(key, label, category, resolution=None, resolved=None, comment=None):
    comments = [{"author": {"accountId": ops_harness.JIRA_ACCOUNT}, "body": {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": comment}]}]}}] if comment else []
    return {"key": key, "fields": {"labels": [label], "status": {"statusCategory": {"key": category}},
                                   "resolution": {"name": resolution} if resolution else None,
                                   "resolutiondate": resolved, "comment": {"comments": comments}}}


def test_a_day_files_the_regression_and_the_worst_new_crash_and_no_more(sqlite_engine, fs, tmp_path, monkeypatch):
    monkeypatch.setattr(gcp, "token", lambda creds, now, http: "ya29.test")
    monkeypatch.setattr(github_vendor, "_tokens", {})
    monkeypatch.setattr(github_vendor, "run_gh", lambda host: "gho_" + "c" * 36)
    vendor = Vendors()
    vendor.bigquery = {"jobComplete": True, "schema": {"fields": [{"name": n, "type": t} for n, t in COLUMNS]},
                       "rows": [_row("a1", 50, "-[Tree load]", "5.7.1", "2026-11-08T10:00:00+00:00"),
                                _row("b2", 40, "-[Memories open]", "5.7.1", "2026-11-08T10:00:00+00:00"),
                                _row("c3", 30, "-[Search run]", "5.7.1", "2026-11-08T10:00:00+00:00"),
                                _row("d4", 60, "-[Photos show]", "5.7.1", "2026-11-08T10:00:00+00:00"),
                                _row("e5", 9, "-[Few users]", "5.7.1", "2026-11-08T10:00:00+00:00")]}
    vendor.jira_found = [
        _issue("MEMTEMDIS-1", "crashlytics-a1", "done", "Fixed", "2026-10-01T10:00:00.000-0600",
               "Merged fs-eng/mobile-ios#812 by stephan."),
        _issue("MEMTEMDIS-4", "crashlytics-d4", "indeterminate")]
    vendor.fix_prs = {812: {"number": 812, "merged": True, "merge_commit_sha": MERGE,
                            "head": {"ref": "armature/MEMTEMDIS-1"},
                            "merged_at": "2026-09-30T10:00:00Z"}}
    vendor.tags = {"familytree_5.7.1": {"behind_by": 0, "date": "2026-10-05T12:00:00Z"}}
    replay = ops_harness.run_stored(sqlite_engine, fs["workflow"], {},
                                    tmp_path, monkeypatch, vendor=vendor)
    assert [c["tool"] for c in replay.calls] == ["crashlytics.clusters", "jira.search", "github.release-contains",
                                                 "jira.create", "jira.create"]
    search = next(c for c in replay.calls if c["tool"] == "jira.search")
    assert search["params"]["labels"] == ["crashlytics-a1", "crashlytics-b2", "crashlytics-c3", "crashlytics-d4",
                                          "crashlytics-e5"]
    contains = next(c for c in replay.calls if c["tool"] == "github.release-contains")
    assert contains["params"] == {"repo": "fs-eng/mobile-ios", "pr": 812, "issue": "MEMTEMDIS-1",
                                  "versions": ["5.7.1"], "tag_prefix": "familytree_"}
    first, second = [c["params"] for c in replay.calls if c["tool"] == "jira.create"]
    assert (first["labels"], first["link"], first["summary"]) == (
        ["crashlytics-a1"], "MEMTEMDIS-1", "Crash regression: -[Tree load]")
    assert (second["labels"], second["link"], second["summary"]) == (["crashlytics-b2"], "", "Crash: -[Memories open]")
    assert first["type"] == second["type"] == "Defect" and first["assign_to_me"] is True
    creates = [e for e in replay.effects if e.get("op") == "create"]
    assert [e["project"] for e in creates] == ["MEMTEMDIS", "MEMTEMDIS"] and replay.sessions == []


def test_a_quiet_day_files_nothing(sqlite_engine, fs, tmp_path, monkeypatch):
    monkeypatch.setattr(gcp, "token", lambda creds, now, http: "ya29.test")
    vendor = Vendors()
    vendor.bigquery = {"jobComplete": True, "schema": {"fields": [{"name": n, "type": t} for n, t in COLUMNS]},
                       "rows": []}
    replay = ops_harness.run_stored(sqlite_engine, fs["workflow"], {},
                                    tmp_path, monkeypatch, vendor=vendor)
    assert [c["tool"] for c in replay.calls] == ["crashlytics.clusters", "jira.search"]
    assert replay.effects == []

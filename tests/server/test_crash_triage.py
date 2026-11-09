"""crash-watch's pure decisions: which fixes to check, and which crashes to file as new or regressed."""
from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from armature.registry.registry import ToolRegistry
from armature.server import crash_triage as triage

AS_OF = "2026-11-09T13:30:00+00:00"
REPO = "fs-eng/mobile-ios"


def cluster(issue_id="a1", users=40, versions=(("5.7.1", "2026-11-08T10:00:00+00:00"),)):
    return {"issue_id": issue_id, "label": f"crashlytics-{issue_id}", "title": f"-[Tree {issue_id}]", "users": users,
            "events": users * 2, "first_seen": "2026-11-02T10:00:00+00:00", "last_seen": "2026-11-08T10:00:00+00:00",
            "frame": {"symbol": f"-[Tree {issue_id}]", "file": "Tree.m", "line": 88, "library": "Tree"},
            "versions": [{"version": v, "users": users, "last_seen": seen} for v, seen in versions]}


def issue(key, issue_id="a1", category="done", resolution="Fixed", resolved="2026-10-01T10:00:00.000-0600",
          prs=((REPO, 812),)):
    return {"issue": key, "labels": [f"crashlytics-{issue_id}"], "status_category": category,
            "resolution": resolution if category == "done" else None, "resolved_at": resolved,
            "merged_prs": [{"repo": r, "number": n} for r, n in prs]}


def shipped(key, *tags, merged=True):
    return {"issue": key, "merged": merged, "pr": 812, "repo": REPO,
            "versions": [{"version": v, "tag": f"familytree_{v}", "contained": c, "tag_date": d} for v, c, d in tags]}


def decide(clusters, issues=(), fixes=(), as_of=AS_OF):
    return triage.triage(list(clusters), list(issues), list(fixes), as_of)


def decisions(result):
    return [(d["issue_id"], d["decision"]) for d in result["decisions"]]


def test_a_crash_with_no_issue_is_new():
    result = decide([cluster("b10", users=10)])
    assert decisions(result) == [("b10", "new")]
    [action] = result["actions"]
    assert action == {**action, "kind": "new", "issue_id": "b10", "labels": ["crashlytics-b10"], "link": "",
                      "summary": "Crash: -[Tree b10]"}


def test_an_open_issue_wins_over_a_resolved_one_with_the_same_label():
    result = decide([cluster("a1")], [issue("MEMTEMDIS-1"), issue("MEMTEMDIS-8", category="indeterminate")],
                    [shipped("MEMTEMDIS-1", ("5.7.1", True, "2026-10-05T12:00:00Z"))])
    assert decisions(result) == [("a1", "skip")] and result["actions"] == []


def test_an_open_issue_skips_and_a_resolution_other_than_fixed_ignores():
    result = decide([cluster("a1"), cluster("b2")],
                    [issue("MEMTEMDIS-1", "a1", category="indeterminate"),
                     issue("MEMTEMDIS-2", "b2", resolution="Won't Do")])
    assert decisions(result) == [("a1", "skip"), ("b2", "ignore")] and result["actions"] == []


def test_a_fix_with_no_merged_pull_request_is_ignored():
    result = decide([cluster("a1"), cluster("b2")], [issue("MEMTEMDIS-1", "a1"), issue("MEMTEMDIS-2", "b2")],
                    [shipped("MEMTEMDIS-1", merged=False)])
    assert decisions(result) == [("a1", "ignore"), ("b2", "ignore")]
    assert all("no merged pull request" in d["reason"] for d in result["decisions"])


def test_a_tag_holding_the_fix_that_crashed_after_it_is_a_regression_linked_to_the_old_issue():
    crash = cluster("a1", versions=(("5.7.0", "2026-11-08T10:00:00+00:00"), ("5.7.1", "2026-11-08T11:00:00+00:00")))
    result = decide([crash], [issue("MEMTEMDIS-1")],
                    [shipped("MEMTEMDIS-1", ("5.7.0", False, "2026-09-20T12:00:00Z"),
                             ("5.7.1", True, "2026-10-05T12:00:00Z"))])
    assert decisions(result) == [("a1", "regression")]
    [action] = result["actions"]
    assert action["link"] == "MEMTEMDIS-1" and action["labels"] == ["crashlytics-a1"]
    assert action["summary"] == "Crash regression: -[Tree a1]"
    assert "familytree_5.7.1 holds" in action["description"] and "on 5.7.1" in action["description"]


def test_containment_is_per_crashing_version_with_no_version_ordering():
    # 5.7.1 holds the fix, but only 5.7.0 still crashes, so nothing regressed.
    crash = cluster("a1", versions=(("5.7.0", "2026-11-08T10:00:00+00:00"),))
    result = decide([crash], [issue("MEMTEMDIS-1", resolved="2026-11-01T10:00:00Z")],
                    [shipped("MEMTEMDIS-1", ("5.7.0", False, "2026-09-20T12:00:00Z"))])
    assert decisions(result) == [("a1", "wait")]


def test_a_version_with_no_tag_is_unknown_and_never_a_regression():
    result = decide([cluster("a1")], [issue("MEMTEMDIS-1", resolved="2026-11-01T10:00:00Z")],
                    [shipped("MEMTEMDIS-1", ("5.7.1", None, None))])
    assert decisions(result) == [("a1", "wait")]


def test_a_crash_from_before_the_tag_is_not_a_regression():
    crash = cluster("a1", versions=(("5.7.1", "2026-10-04T10:00:00+00:00"),))
    result = decide([crash], [issue("MEMTEMDIS-1")], [shipped("MEMTEMDIS-1", ("5.7.1", True, "2026-10-05T12:00:00Z"))])
    assert decisions(result) == [("a1", "ignore")]


@pytest.mark.parametrize("resolved,decision", [("2026-10-19T13:30:01+00:00", "wait"),
                                               ("2026-10-19T13:30:00+00:00", "ignore")])
def test_an_unshipped_fix_waits_21_days(resolved, decision):
    result = decide([cluster("a1")], [issue("MEMTEMDIS-1", resolved=resolved)],
                    [shipped("MEMTEMDIS-1", ("5.7.1", False, "2026-10-05T12:00:00Z"))])
    assert decisions(result) == [("a1", decision)]


def test_at_most_two_are_filed_most_users_first_then_by_issue_id():
    result = decide([cluster("c3", users=40), cluster("b2", users=50), cluster("a1", users=50),
                     cluster("d4", users=60)], [issue("MEMTEMDIS-4", "d4", category="new")])
    assert [a["issue_id"] for a in result["actions"]] == ["a1", "b2"] and result["count"] == 2
    assert decisions(result) == [("d4", "skip"), ("a1", "new"), ("b2", "new"), ("c3", "new")]


def test_a_rerun_skips_what_the_first_run_filed():
    clusters = [cluster("a1", users=50), cluster("b2", users=40), cluster("c3", users=30)]
    first = decide(clusters)
    filed = [issue(f"MEMTEMDIS-{n}", a["issue_id"], category="new") for n, a in enumerate(first["actions"], 1)]
    second = decide(clusters, filed)
    assert [a["issue_id"] for a in first["actions"]] == ["a1", "b2"]
    assert [a["issue_id"] for a in second["actions"]] == ["c3"]


def test_the_latest_resolved_issue_decides_and_its_fix_is_checked_in_this_repo_only():
    issues = [issue("MEMTEMDIS-1", resolved="2026-09-01T10:00:00Z", prs=((REPO, 700),)),
              issue("MEMTEMDIS-9", resolved="2026-10-01T10:00:00Z", prs=(("fs-eng/android-tree", 5), (REPO, 812))),
              issue("MEMTEMDIS-3", "b2", category="indeterminate"),
              issue("MEMTEMDIS-4", "c3", resolution="Duplicate")]
    crash = cluster("a1", versions=(("5.7.1", "x"), ("unknown", "x")))
    assert triage.checks([crash, cluster("b2"), cluster("c3"), cluster("d4")], issues, REPO) == [
        {"issue": "MEMTEMDIS-9", "pr": 812, "versions": ["5.7.1"]}]
    assert triage.checks([cluster("a1")], [issue("MEMTEMDIS-1", prs=())], REPO) == [
        {"issue": "MEMTEMDIS-1", "pr": None, "versions": ["5.7.1"]}]


@pytest.mark.parametrize("bad", [None, "not a list", [1, 2]])
def test_malformed_input_files_nothing(bad):
    with pytest.raises(ValueError):
        triage.triage(bad, [], [], AS_OF)
    with pytest.raises(ValueError):
        triage.triage([cluster()], bad, [], AS_OF)
    with pytest.raises(ValueError):
        triage.checks([cluster()], bad, REPO)


def test_as_of_must_carry_a_zone():
    with pytest.raises(ValueError):
        triage.triage([cluster()], [], [], "2026-11-09T13:30:00")


def test_the_description_is_a_template_of_the_crash_data():
    [action] = decide([cluster("a1")])["actions"]
    text = action["description"]
    assert "Crashlytics issue a1 crashed the app for 40 users (80 crashes)" in text
    assert "-[Tree a1]\nTree.m:88 in Tree" in text and "5.7.1: 40 users, last seen" in text
    assert text.endswith("Label crashlytics-a1.")


def test_the_module_registers_both_steps_as_read_only_tools():
    registry = ToolRegistry()
    triage.register(registry)
    found = asyncio.run(registry.dispatch("crash_watch_triage", {
        "clusters": [cluster("a1")], "issues": [], "shipped": [], "as_of": AS_OF}))
    assert decisions(found) == [("a1", "new")]
    checks = asyncio.run(registry.dispatch("crash_watch_checks", {"clusters": [], "issues": [], "repo": REPO}))
    assert checks == {"checks": []}


def test_the_runner_imports_the_triage_module_without_the_server():
    code = ("import sys, armature.server.crash_triage; "
            "print(sorted(m for m in ('sqlalchemy', 'armature.server.broker', 'armature.server.migrate_ops') "
            "if m in sys.modules))")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert done.stdout.strip() == "[]"

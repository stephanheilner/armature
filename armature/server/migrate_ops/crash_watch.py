"""crash-watch: each morning, the Tree iOS app's worst new or regressed Crashlytics crashes become MEMTEMDIS Defects.

At most two a day, assigned to the Jira connection's account in the board's active sprint, so ops' jira-watch hands
them to development. A crash with an open issue is left alone. One whose issue was resolved Fixed is filed again,
linked to the old issue, only when a release tag holding the fix crashed after the tag. The decisions are
armature.server.crash_triage's pure functions and the description is a template, so no model sees crash data.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from armature.server.migrate_ops import MigrateError, Poll, Rows

REPO = "fs-eng/mobile-ios"
TAG_PREFIX = "familytree_"
CRON = "30 6 * * *"
TIMEZONE = "America/Denver"
DAYS = 7
MIN_USERS = 10
TOP = 20
SETTINGS = {"jira.components": ["Mobile iOS"], "jira.board": 3887}


def _file(n: int, after: str) -> str:
    action = f"triage.actions[{n - 1}]"
    return f"""
  - id: file_{n}
    adapter: jira/create
    skip_if: "{{{{ triage.count < {n} }}}}"
    with:
      type: Defect
      summary: "{{{{ {action}.summary }}}}"
      description: "{{{{ {action}.description }}}}"
      labels: "{{{{ {action}.labels }}}}"
      link: "{{{{ {action}.link }}}}"
      assign_to_me: true
    depends_on: [{after}]
"""


BODY = f"""\
name: crash-watch
version: "1.0"
description: "File the worst new or regressed fatal Crashlytics crashes of the last week as Jira Defects, at most two a day."
tools:
  - module: armature.server.crash_triage
contracts:
  timeout_hours: 0.5
stages:
  - id: clusters
    adapter: crashlytics/clusters
    with: {{days: {DAYS}, min_users: {MIN_USERS}, top: {TOP}}}
    depends_on: []

  - id: issues
    adapter: jira/search
    with:
      labels: "{{{{ clusters.labels }}}}"
    depends_on: [clusters]

  - id: fixed
    tool_call:
      name: crash_watch_checks
      args:
        clusters: "{{{{ clusters.clusters }}}}"
        issues: "{{{{ issues.issues }}}}"
        repo: {REPO}
    depends_on: [issues]

  - id: shipped
    adapter: github/release-contains
    fan_out: 5
    fan_in: list
    partition_source: "{{{{ fixed.checks }}}}"
    partition_key: check
    with:
      repo: {REPO}
      pr: "{{{{ check.pr }}}}"
      issue: "{{{{ check.issue }}}}"
      versions: "{{{{ check.versions }}}}"
      tag_prefix: {TAG_PREFIX}
    depends_on: [fixed]

  - id: triage
    tool_call:
      name: crash_watch_triage
      args:
        clusters: "{{{{ clusters.clusters }}}}"
        issues: "{{{{ issues.issues }}}}"
        shipped: "{{{{ shipped }}}}"
        as_of: "{{{{ clusters.as_of }}}}"
        since: "{{{{ clusters.since }}}}"
    depends_on: [shipped]
{_file(1, "triage")}{_file(2, "file_1")}"""

NOTES = (
    ("Before enabling the schedule, add a GCP connection on this project with the crash-watch service account's "
     "JSON key and the Crashlytics export's project_id, dataset (firebase_crashlytics), and daily table "
     "(org_familysearch_tree_IOS), then Verify it."),
    ("The jira.project setting (MEMTEMDIS) must name the Jira project, on the team or this project. This import sets "
     "jira.components to Mobile iOS and jira.board to the MEMTEMDIS scrum board, whose one active sprint gets each "
     "crash issue. Both apply only to a jira.create with a crashlytics- label, so other workflows' issues in this "
     "project go out as before."),
    "Issues are assigned to the Jira connection's account, so ops' jira-watch picks them up for that engineer.",
    "The schedule starts disabled. Enable it on the Triggers page after a dry run.",
)


def rows(setting: Callable[[str], Any], project: Any = None) -> Rows:
    repo = getattr(project, "repo", None) or ""
    if repo.lower() != REPO:
        raise MigrateError(f"crash-watch runs only on {REPO}, not {repo or 'this project'}.")
    config = {"name": f"Daily crash watch of {REPO}", "cron": CRON, "timezone": TIMEZONE,
              "project_id": project.id}
    return Rows(name="crash-watch", kind="other", body=BODY, polls=(Poll("schedule", config),),
                settings=dict(SETTINGS), notes=NOTES)

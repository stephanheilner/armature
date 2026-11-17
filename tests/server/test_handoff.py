"""workflow.handoff: a step queues a run of another stored workflow in its own project, through the broker."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select, update

from armature.server import connections, defs, runners, worker
from armature.server.db import new_id
from armature.server.db.schema import audit, jobs, projects, runs, tool_calls, triggers
from armature.server.tools import NONE, registry
from tests.server.tool_support import (  # noqa: F401
    BODY,
    app,
    ask_grant,
    clock,
    dns,
    pem,
    start_run,
    use,
    vendor,
    world,
)

CALLER = BODY + """  - id: handoff
    adapter: workflow/handoff
    with: {workflow: approve}
  - id: loop
    adapter: workflow/handoff
    with: {workflow: tools}
  - id: templated
    adapter: workflow/handoff
    with: {workflow: "{{ target }}"}
"""
APPROVE = """name: approve
requires: [host]
contracts:
  inputs:
    - {name: issue, required: true}
    - {name: verdict}
    - {name: repos, type: array}
stages:
  - id: approve
    gate: human
"""
ISSUE = {"issue": "MEMTEMDIS-12", "verdict": "bug", "repos": ["android-tree"]}


def _workflow(engine, clock, scope, name, body):
    workflow_id = new_id()
    with engine.begin() as conn:
        conn.execute(insert(defs.WORKFLOW.table).values(id=workflow_id, name=name, current_version=1,
                                                        created_at=clock(), updated_at=clock(), **scope.columns()))
        defs._add_version(conn, defs.WORKFLOW, workflow_id, 1, body, "seed", None, clock())
    return workflow_id


@pytest.fixture
def run(sqlite_engine, keyring, clock, world):
    with sqlite_engine.begin() as conn:
        defs._add_version(conn, defs.WORKFLOW, world.workflow, 2, CALLER, "handoff", None, clock())
    found = start_run(sqlite_engine, keyring, clock, world, version=2)
    with sqlite_engine.begin() as conn:
        conn.execute(update(runs).where(runs.c.id == found.id).values(actor="ana", triggered_by="user-1",
                                                                      thread="jira:MEMTEMDIS-12"))
    return found


@pytest.fixture
def approve(sqlite_engine, clock, world):
    return _workflow(sqlite_engine, clock, defs.DefScope("team", world.team), "approve", APPROVE)


def _handoff(app, run, step="handoff", **params):
    granted = ask_grant(app, run, step, tools=["workflow.handoff"])
    assert granted.status_code == 200, granted.text
    return use(app, granted.json()["token"], "workflow.handoff", params)


def _ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def _refused(response, status, error):
    assert response.status_code == status, response.text
    assert response.json()["error"].startswith(error)


def _jobs(engine, workflow_id=None):
    query = select(jobs).order_by(jobs.c.created_at)
    if workflow_id is not None:
        query = query.where(jobs.c.workflow_id == workflow_id)
    with engine.connect() as conn:
        return conn.execute(query).all()


def test_handoff_is_a_database_only_write():
    spec = registry()["workflow.handoff"]
    assert spec.kind == NONE and spec.reads is False


def test_a_handoff_queues_the_target_like_a_trigger(app, sqlite_engine, run, approve, world, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("workflow.handoff touched a connection")
    monkeypatch.setattr(connections, "resolve", refuse)

    out = _ok(_handoff(app, run, workflow="approve", inputs=ISSUE))
    [job] = _jobs(sqlite_engine, approve)
    assert out == {"queued": True, "run_id": None, "job_id": job.id, "workflow": "approve"}
    assert (job.team_id, job.project_id, job.status, job.workflow_version) == (world.team, world.project, "queued", 1)
    assert job.inputs == ISSUE and job.requires == ["host"]
    assert job.concurrency_key == f"handoff:{run.id}:handoff"
    assert (job.requested_by, job.thread) == ("user-1", "jira:MEMTEMDIS-12")
    with sqlite_engine.connect() as conn:
        [call] = conn.execute(select(tool_calls).where(tool_calls.c.tool == "workflow.handoff")).all()
        [audited] = conn.execute(select(audit).where(audit.c.action == "run.handoff")).all()
    assert (call.run_id, call.step_id, call.mode, call.outcome, call.target) == (run.id, "handoff", "live", "ok",
                                                                                 "approve")
    assert (audited.actor_id, audited.target_id) == ("user-1", run.id)
    assert audited.detail == {"step": "handoff", "workflow": "approve", "job_id": job.id, "actor": "ana"}


def test_the_handed_off_run_records_the_caller_as_its_trigger(app, sqlite_engine, keyring, clock, run, approve):
    _ok(_handoff(app, run, workflow="approve", inputs=ISSUE))
    with sqlite_engine.begin() as conn:
        job = conn.execute(select(jobs).where(jobs.c.workflow_id == approve)).one()
        bundle = worker.build_bundle(conn, keyring, job, now=clock(), lease=worker.LEASE, events="x")
        opened = conn.execute(select(runs).where(runs.c.id == bundle.run_id)).one()
    assert (opened.trigger_kind, opened.trigger_ref) == ("handoff", {"run_id": run.id, "step": "handoff"})
    assert (opened.actor, opened.triggered_by, opened.thread) == ("ana", "user-1", "jira:MEMTEMDIS-12")
    assert bundle.inputs == ISSUE
    again = _ok(_handoff(app, run, workflow="approve", inputs=ISSUE))
    assert again == {"queued": True, "run_id": bundle.run_id, "job_id": job.id, "workflow": "approve"}


def test_a_retried_step_gets_the_job_its_first_call_queued(app, sqlite_engine, run, approve):
    first = _ok(_handoff(app, run, workflow="approve", inputs=ISSUE))
    second = _ok(_handoff(app, run, workflow="approve", inputs={**ISSUE, "verdict": "not a bug"}))
    assert second == first
    assert len(_jobs(sqlite_engine, approve)) == 1


def test_the_project_workflow_wins_over_the_teams(app, sqlite_engine, clock, run, approve, world):
    mine = _workflow(sqlite_engine, clock, defs.DefScope("project", world.team, world.project), "approve", APPROVE)
    _ok(_handoff(app, run, workflow="approve", inputs=ISSUE))
    assert [j.workflow_id for j in _jobs(sqlite_engine) if j.concurrency_key] == [mine]


def test_an_instance_workflow_is_reachable(app, sqlite_engine, clock, run):
    shared = _workflow(sqlite_engine, clock, defs.INSTANCE, "approve", APPROVE)
    assert _ok(_handoff(app, run, workflow="approve", inputs=ISSUE))["queued"] is True
    assert len(_jobs(sqlite_engine, shared)) == 1


@pytest.mark.parametrize("where", ["other_team", "other_project"])
def test_another_teams_or_projects_workflow_is_refused(app, sqlite_engine, clock, run, world, where):
    if where == "other_team":
        scope = defs.DefScope("team", world.other)
    else:
        project = new_id()
        with sqlite_engine.begin() as conn:
            conn.execute(insert(projects).values(id=project, team_id=world.team, name="web", repo="Acme/Web",
                                                 created_at=clock()))
        scope = defs.DefScope("project", world.team, project)
    _workflow(sqlite_engine, clock, scope, "approve", APPROVE)
    _refused(_handoff(app, run, workflow="approve", inputs=ISSUE), 404,
             "No workflow named approve in this project, its team, or the instance.")
    assert [j.id for j in _jobs(sqlite_engine) if j.concurrency_key] == []
    with sqlite_engine.connect() as conn:
        [call] = conn.execute(select(tool_calls).where(tool_calls.c.tool == "workflow.handoff")).all()
    assert (call.outcome, call.target) == ("refused", "approve")


def test_a_workflow_cant_hand_off_to_itself(app, sqlite_engine, run):
    _refused(_handoff(app, run, "loop", workflow="tools"), 422, "A workflow can't hand off to itself.")
    assert len(_jobs(sqlite_engine)) == 1


def test_a_step_hands_off_only_to_the_literal_its_workflow_names(app, sqlite_engine, clock, run, approve, world):
    _workflow(sqlite_engine, clock, defs.DefScope("team", world.team), "other", APPROVE)
    _refused(_handoff(app, run, workflow="other", inputs=ISSUE), 422,
             "Step handoff hands off only to the workflow its with.workflow names.")
    _refused(_handoff(app, run, "templated", workflow="other", inputs=ISSUE), 422,
             "Step templated must name its target as a literal with.workflow, never a template.")
    _refused(_handoff(app, run, "templated", workflow="{{ target }}", inputs=ISSUE), 422,
             "Step templated must name its target as a literal with.workflow, never a template.")
    assert [j.id for j in _jobs(sqlite_engine) if j.concurrency_key] == []


def test_a_triggers_job_with_a_handoff_key_is_not_a_handoff(app, sqlite_engine, keyring, clock, run, approve,
                                                            world):
    forged = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(jobs).values(id=forged, team_id=world.team, project_id=world.project,
                                         workflow_id=approve, workflow_version=1, inputs=ISSUE, status="queued",
                                         attempts=0, created_at=clock(), delivery_id=new_id(),
                                         concurrency_key=f"handoff:{run.id}:handoff"))
    out = _ok(_handoff(app, run, workflow="approve", inputs=ISSUE))
    assert out["job_id"] != forged and len(_jobs(sqlite_engine, approve)) == 2
    with sqlite_engine.begin() as conn:
        job = conn.execute(select(jobs).where(jobs.c.id == forged)).one()
        bundle = worker.build_bundle(conn, keyring, job, now=clock(), lease=worker.LEASE, events="x")
        opened = conn.execute(select(runs).where(runs.c.id == bundle.run_id)).one()
    assert opened.trigger_kind == "webhook" and opened.actor is None


def test_an_experiment_arm_copied_from_a_handoff_is_not_a_handoff(sqlite_engine, keyring, clock, run, approve,
                                                                   world):
    arm = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(jobs).values(id=arm, team_id=world.team, project_id=world.project, workflow_id=approve,
                                         workflow_version=1, inputs=ISSUE, status="queued", attempts=0,
                                         created_at=clock(), arm_id=new_id(),
                                         concurrency_key=f"handoff:{run.id}:handoff#arm:{new_id()}"))
        job = conn.execute(select(jobs).where(jobs.c.id == arm)).one()
        bundle = worker.build_bundle(conn, keyring, job, now=clock(), lease=worker.LEASE, events="x")
        opened = conn.execute(select(runs).where(runs.c.id == bundle.run_id)).one()
    assert opened.trigger_kind == "webhook" and opened.actor is None
    assert "run_id" not in opened.trigger_ref


def test_a_reruns_copied_key_never_answers_for_the_step(app, sqlite_engine, clock, run, approve, world):
    with sqlite_engine.begin() as conn:
        conn.execute(insert(jobs).values(id=new_id(), team_id=world.team, project_id=world.project,
                                         workflow_id=approve, workflow_version=1, inputs=ISSUE, status="queued",
                                         attempts=0, created_at=clock(), rerun_of=new_id(),
                                         concurrency_key=f"handoff:{run.id}:handoff"))
    out = _ok(_handoff(app, run, workflow="approve", inputs=ISSUE))
    assert [j.id for j in _jobs(sqlite_engine, approve) if j.rerun_of is None] == [out["job_id"]]


@pytest.mark.parametrize("inputs, error", [
    ({"verdict": "bug"}, "approve: Required input 'issue' missing from context"),
    ({**ISSUE, "url": "https://x"}, "approve declares no input url in contracts.inputs."),
    ({**ISSUE, "repos": "android-tree"}, "approve: Input 'repos' must be an array"),
])
def test_inputs_that_dont_fit_the_targets_contract_are_refused(app, sqlite_engine, run, approve, inputs, error):
    _refused(_handoff(app, run, workflow="approve", inputs=inputs), 422, error)
    assert _jobs(sqlite_engine, approve) == []


def test_inputs_outside_the_adapters_schema_are_a_bad_request(app, sqlite_engine, run, approve):
    assert _handoff(app, run, workflow="approve", inputs={"issue": {"key": "X-1"}}).status_code == 400
    assert _handoff(app, run, workflow="approve", inputs={"bad-name": "x"}).status_code == 400
    assert _handoff(app, run, inputs=ISSUE).status_code == 400
    assert _jobs(sqlite_engine, approve) == []


def test_a_target_whose_callee_doesnt_resolve_is_refused(app, sqlite_engine, clock, run, world):
    body = APPROVE + "  - id: nested\n    call: {workflow: missing}\n"
    _workflow(sqlite_engine, clock, defs.DefScope("team", world.team), "approve", body)
    _refused(_handoff(app, run, workflow="approve", inputs=ISSUE), 422, "A workflow approve calls doesn't resolve")


def test_a_dry_run_queues_nothing(app, sqlite_engine, keyring, clock, world, approve):
    with sqlite_engine.begin() as conn:
        defs._add_version(conn, defs.WORKFLOW, world.workflow, 2, CALLER, "handoff", None, clock())
    run = start_run(sqlite_engine, keyring, clock, world, version=2, trigger_ref={"dry_run": True})
    assert _ok(_handoff(app, run, workflow="approve", inputs=ISSUE))["dry_run"] is True
    assert _jobs(sqlite_engine, approve) == []


def test_a_target_with_no_saved_version_is_refused(app, sqlite_engine, clock, run, world):
    with sqlite_engine.begin() as conn:
        conn.execute(insert(defs.WORKFLOW.table).values(id=new_id(), name="approve", current_version=0,
                                                        created_at=clock(), updated_at=clock(),
                                                        **defs.DefScope("team", world.team).columns()))
    _refused(_handoff(app, run, workflow="approve", inputs=ISSUE), 422, "approve has no saved version.")
    assert [j.id for j in _jobs(sqlite_engine) if j.concurrency_key] == []


def test_a_poll_triggers_job_with_the_steps_key_never_answers_for_it(app, sqlite_engine, clock, run, approve, world):
    rule = new_id()
    with sqlite_engine.begin() as conn:
        conn.execute(insert(triggers).values(id=rule, team_id=world.team, workflow_id=approve, kind="jira_poll",
                                             config={}, created_at=clock()))
        conn.execute(insert(jobs).values(id=new_id(), team_id=world.team, project_id=world.project,
                                         workflow_id=approve, workflow_version=1, inputs=ISSUE, status="queued",
                                         attempts=0, created_at=clock(), trigger_id=rule,
                                         concurrency_key=f"handoff:{run.id}:handoff"))
    out = _ok(_handoff(app, run, workflow="approve", inputs=ISSUE))
    assert [j.id for j in _jobs(sqlite_engine, approve) if j.trigger_id is None] == [out["job_id"]]


INTAKE = """name: intake
requires: [remote:foundry]
stages:
  - id: handoff
    adapter: workflow/handoff
    with: {workflow: followup, inputs: {issue: "{{ issue }}"}}
"""
FOLLOWUP = """name: followup
contracts:
  inputs:
    - {name: issue, required: true}
stages:
  - id: approve
    gate: human
"""


def test_a_runner_that_cant_pause_hands_off_to_a_gated_workflow_a_pausing_runner_claims(app, sqlite_engine, keyring,
                                                                                         clock, world):
    from armature.runner.process import BUNDLE_VERSION

    intake = _workflow(sqlite_engine, clock, defs.DefScope("team", world.team), "intake", INTAKE)
    followup = _workflow(sqlite_engine, clock, defs.DefScope("team", world.team), "followup", FOLLOWUP)
    with sqlite_engine.begin() as conn:
        _, key = runners.create_key(conn, keyring, team_id=world.team, name="pool", actor_id=None, now=clock(),
                                         ip=None)
        caller_job = worker.queue_job(conn, team_id=world.team, project_id=world.project, workflow_id=intake,
                                      inputs={"issue": "MEMTEMDIS-12"}, now=clock(), requested_by="user-1")
    auth = {"Authorization": f"Bearer {key}"}

    def hello(name, capabilities, pauses):
        answer = app.post("/api/runner/hello", json={"name": name, "capabilities": capabilities, "pauses": pauses},
                          headers=auth)
        assert answer.status_code == 200, answer.text
        return answer.json()["runner_id"]

    def claim(runner_id):
        answer = app.post("/api/runner/claim", json={"runner_id": runner_id, "bundle_version": BUNDLE_VERSION},
                          headers=auth)
        assert answer.status_code in (200, 204), answer.text
        return None if answer.status_code == 204 else answer.json()["job"]

    local = hello("local", ["git", "host"], True)
    foundry = hello("foundry", ["remote:foundry"], False)
    stuck = hello("stuck", ["git", "host"], False)
    assert claim(local) is None and claim(stuck) is None
    bundle = claim(foundry)
    assert bundle["job_id"] == caller_job and bundle["checkpoint"] is None

    caller = SimpleNamespace(id=bundle["run_id"], token=bundle["run_token"], key=runners.engine_key(keyring, bundle["run_id"]),
                             clock=clock)
    out = _ok(_handoff(app, caller, workflow="followup", inputs={"issue": "MEMTEMDIS-12"}))
    [target] = _jobs(sqlite_engine, followup)
    assert out["job_id"] == target.id and target.requires is None
    with sqlite_engine.connect() as conn:
        assert worker.pauses(conn, target.id)

    assert claim(foundry) is None and claim(stuck) is None
    assert _jobs(sqlite_engine, followup)[0].status == "queued"
    handed = claim(local)
    assert handed["job_id"] == target.id and handed["checkpoint"] == {}
    with sqlite_engine.connect() as conn:
        opened = conn.execute(select(runs).where(runs.c.id == handed["run_id"])).one()
        claimed = {j.id: j.runner_id for j in conn.execute(select(jobs)).all()}
    assert (opened.trigger_kind, opened.trigger_ref) == ("handoff", {"run_id": caller.id, "step": "handoff"})
    assert (claimed[caller_job], claimed[target.id]) == (foundry, local)

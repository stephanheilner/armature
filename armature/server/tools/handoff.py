"""workflow.handoff: queue a run of another stored workflow in the caller's own project, as a trigger would.

The target resolves by name the way a run in the caller's project resolves it, so a step never reaches another
team's workflow. Each step queues at most one job, so a retried step gets the job its first call queued.

workflow.again: queue the caller's own workflow once more, at its current version, under the calling job's
concurrency key, so the new run waits for this one to end and anything that supersedes the key supersedes it too. A
dry run never reaches it, since the broker answers every write tool's dry run itself.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select, update

from armature.packs.params import is_template
from armature.server import audit, broker, defs, worker
from armature.server.db.schema import audit as audit_rows
from armature.server.db.schema import jobs, runs, workflows
from armature.server.tools import Call, ToolError
from armature.spec.contract_io import InputError, resolve_inputs

ADAPTER = "workflow/handoff"
AGAIN_ADAPTER = "workflow/again"
KEY_MAX = 191


def _named(conn: Any, job: Any, step: str, adapter: str = ADAPTER) -> list[Any]:
    """The with.workflow of each ``step`` stage that runs ``adapter``, in the bodies broker.step_tools reads."""
    found = []
    for data in map(worker._load, broker.step_bodies(conn, job)):
        stages = data.get("stages") if isinstance(data, dict) else None
        adapters = data.get("adapters") if isinstance(data, dict) else None
        for stage in stages if isinstance(stages, list) else []:
            if isinstance(stage, dict) and stage.get("id") == step and stage.get("adapter") == adapter \
                    and not (isinstance(adapters, dict) and adapter in adapters):
                given = stage.get("with")
                found.append(given.get("workflow") if isinstance(given, dict) else None)
    return found


def _check_target(conn: Any, run_id: str, step: str, name: str) -> None:
    """The step's workflow names its target as a literal, so a step can only hand off where its author said."""
    job = conn.execute(select(jobs).where(jobs.c.run_id == run_id)).first()
    named = _named(conn, job, step) if job is not None else []
    if any(not isinstance(v, str) or is_template(v) for v in named):
        raise ToolError(422, f"Step {step} must name its target as a literal with.workflow, never a template.")
    if name not in named:
        raise ToolError(422, f"Step {step} hands off only to the workflow its with.workflow names.")


def _caller(call: Call, conn: Any) -> Any:
    # A write first, so a retried call waits for the first one and finds its job.
    conn.execute(update(runs).where(runs.c.id == call.run_id).values(status=runs.c.status))
    caller = conn.execute(select(runs).where(runs.c.id == call.run_id)).first()
    if caller is None:
        raise ToolError(404, "The calling run is gone.")
    return caller


def _queued_again(conn: Any, run_id: str, step: str) -> Any:
    """The job an earlier call of this step queued, by the audit row it left, since its key is the caller's own."""
    for detail in conn.execute(select(audit_rows.c.detail).where(audit_rows.c.action == "run.again",
                                                                 audit_rows.c.target_id == run_id)).scalars():
        if isinstance(detail, dict) and detail.get("step") == step:
            return conn.execute(select(jobs.c.id, jobs.c.run_id).where(jobs.c.id == detail.get("job_id"))).first()
    return None


def again(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    if call.engine is None:
        raise ToolError(500, "workflow.again needs the server's database.")
    inputs = params.get("inputs") or {}
    with call.engine.begin() as conn:
        caller = _caller(call, conn)
        target = conn.execute(select(workflows).where(workflows.c.id == caller.workflow_id)).first()
        if target is None:
            raise ToolError(404, "The calling run's workflow is gone.")
        job = conn.execute(select(jobs).where(jobs.c.run_id == call.run_id)).first()
        if job is None or not _named(conn, job, call.step_id, AGAIN_ADAPTER):
            raise ToolError(422, f"Step {call.step_id} runs its workflow again only when its stage names "
                                 f"{AGAIN_ADAPTER}.")
        first = _queued_again(conn, call.run_id, call.step_id)
        if first is not None:
            return {"queued": True, "run_id": first.run_id, "job_id": first.id, "workflow": target.name}
        # The run's newest job holds it, so its key is the one the next run waits behind.
        own = conn.execute(select(jobs.c.concurrency_key).where(jobs.c.run_id == call.run_id)
                           .order_by(jobs.c.created_at.desc(), jobs.c.id.desc())).first()
        if own is None or not own.concurrency_key:
            raise ToolError(422, "The calling job has no concurrency key, so a run of it again could overlap it.")
        if not target.current_version:
            raise ToolError(422, f"{target.name} has no saved version.")
        _check_inputs(target.name, defs.body_of(conn, defs.WORKFLOW, target.id, target.current_version), inputs)
        try:
            job_id = worker.queue_job(conn, team_id=caller.team_id, project_id=caller.project_id,
                                      workflow_id=target.id, inputs=inputs, now=call.now, thread=caller.thread,
                                      key=own.concurrency_key, rerun_of=call.run_id,
                                      requested_by=caller.triggered_by)
        except ValueError as exc:
            raise ToolError(422, f"A workflow {target.name} calls doesn't resolve: {exc}") from exc
        audit.record(conn, action="run.again", actor_id=caller.triggered_by, team_id=caller.team_id,
                     target_kind="run", target_id=call.run_id,
                     detail={"step": call.step_id, "workflow": target.name, "job_id": job_id, "actor": caller.actor})
    return {"queued": True, "run_id": None, "job_id": job_id, "workflow": target.name}


def _check_inputs(name: str, body: str | None, inputs: dict[str, Any]) -> None:
    """``inputs`` fit the target's contracts.inputs, by the engine's own rules."""
    data = worker._load(body)
    contracts = data.get("contracts") if isinstance(data, dict) else None
    listed = contracts.get("inputs") if isinstance(contracts, dict) else None
    decls = [d for d in listed if isinstance(d, dict)] if isinstance(listed, list) else []
    undeclared = sorted(set(inputs) - {d.get("name") for d in decls})
    if undeclared:
        raise ToolError(422, f"{name} declares no input {', '.join(undeclared)} in contracts.inputs.")
    try:
        resolve_inputs(decls, inputs)
    except InputError as exc:
        raise ToolError(422, f"{name}: {exc}") from exc


def handoff(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    if call.engine is None:
        raise ToolError(500, "workflow.handoff needs the server's database.")
    key = worker.handoff_key(call.run_id, call.step_id)
    if len(key) > KEY_MAX:
        raise ToolError(400, "The step id is too long to hand off from.")
    name, inputs = params["workflow"], params.get("inputs") or {}
    with call.engine.begin() as conn:
        caller = _caller(call, conn)
        _check_target(conn, call.run_id, call.step_id, name)
        # Only a job this tool queued answers for the step. A rerun or a trigger's job may carry the same key.
        first = conn.execute(select(jobs.c.id, jobs.c.run_id, workflows.c.name).join(
            workflows, workflows.c.id == jobs.c.workflow_id).where(
            jobs.c.concurrency_key == key, jobs.c.team_id == caller.team_id, jobs.c.rerun_of.is_(None),
            jobs.c.trigger_id.is_(None), jobs.c.delivery_id.is_(None))).first()
        if first is not None:
            return {"queued": True, "run_id": first.run_id, "job_id": first.id, "workflow": first.name}
        target = defs.resolve_workflow(conn, name, caller.team_id, caller.project_id)
        # MySQL compares names without case, so the name is matched again here.
        if target is None or target.name != name:
            raise ToolError(404, f"No workflow named {name} in this project, its team, or the instance.")
        if target.id == caller.workflow_id or target.name == caller.workflow_name:
            raise ToolError(422, "A workflow can't hand off to itself.")
        if not target.current_version:
            raise ToolError(422, f"{name} has no saved version.")
        _check_inputs(name, defs.body_of(conn, defs.WORKFLOW, target.id, target.current_version), inputs)
        try:
            job_id = worker.queue_job(conn, team_id=caller.team_id, project_id=caller.project_id,
                                      workflow_id=target.id, inputs=inputs, now=call.now, thread=caller.thread,
                                      key=key, requested_by=caller.triggered_by)
        except ValueError as exc:
            raise ToolError(422, f"A workflow {name} calls doesn't resolve: {exc}") from exc
        # actor_id holds a user id. The run's free-text actor can run to 191 characters, so it goes in the detail.
        audit.record(conn, action="run.handoff", actor_id=caller.triggered_by, team_id=caller.team_id,
                     target_kind="run", target_id=call.run_id,
                     detail={"step": call.step_id, "workflow": name, "job_id": job_id, "actor": caller.actor})
    return {"queued": True, "run_id": None, "job_id": job_id, "workflow": name}

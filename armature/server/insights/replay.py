"""Replays of Claude's code review rounds on another model, and the report that compares the two.

kimi-evaluate starts a replay experiment: one eval_experiments row whose specialist is the panel, and one variant arm
per replayable round of the project's code-review runs. Each arm's job repeats its round's run at the same workflow
version, callees, and inputs as a partial stub: every agent stage runs live on the experiment's alias, and every other
stage answers with the round's recorded output, so nothing reaches GitHub, Jira, or findings.

When every arm has ended, the Arms daemon calls compare. It pairs each round's team lead decisions with the findings
Claude's round recorded, has a blinded judge rate the findings only one side raised, and stores the report on the
experiment and as an HTML file in the reports folder. A replay experiment is never sampled, judged, scored, or
recommended as a specialist experiment is.
"""
from __future__ import annotations

import functools
import json
import logging
import random
from collections import Counter
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Connection, Engine, and_, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from armature.nodes import stub
from armature.server import (
    audit,
    calls,
    defs,
    environments,
    ingest,
    llm,
    risk,
    rounds,
    worker,
)
from armature.server.db import new_id
from armature.server.db.schema import (
    INSTANCE_SCOPE,
    eval_arms,
    eval_experiments,
    findings,
    gh_pull_requests,
    jobs,
    projects,
    run_steps,
    runs,
    settings,
    teams,
    users,
    workflows,
)
from armature.server.insights import (
    arm_matching,
    arms,
    experiments,
    judge,
    labeling,
    replay_html,
    report_file,
)
from armature.server.tools import Call, ToolError, marks, review
from armature.spec.models import AGENT_HARNESSES, Stage

log = logging.getLogger(__name__)

WORKFLOW = "kimi-evaluate"
SOURCE = "code-review"
KEY = "kimi-evaluate:"
CONTEXT, RATE, RISK, LEAD = "context", "rate", "risk", "team_lead"
LABEL = "variant"
KEPT = ("block", "suggest")
# What a replay's job still requires of the source's: what its bundle needs to run, never where the source ran.
RUNS_WITH = ("git", worker.CALLS_CAPABILITY, worker.ROLES_CAPABILITY, *AGENT_HARNESSES)
DEFAULTS = {"alias": "kimi", "include_merged": True, "budget_usd": 50, "max_in_flight": 2, "max_rounds": 20}
LIMITS = {"budget_usd": (1, 1000), "max_in_flight": (1, 20), "max_rounds": (1, 500)}
NOT_ADMIN = "Only a kimi-evaluate run an admin started can replay reviews."
RUNNER = "A runner registered this round itself, so there is no server job to repeat."
NO_CONTEXT = "The round has no recorded context step."
UNFINISHED = "The replay didn't finish."
# Only a runner says which bundle version it reads, when it claims, so one that runs replays lists stubs:v5 itself.
NO_RUNNER = (f"No runner online offers {worker.EVAL_ARM} and {worker.STUBS_CAPABILITY} with the review's other "
             "requirements. Upgrade a runner to bundle version 5 and add both to its capabilities.")
NO_LEAD = "The replay's team lead output is missing or truncated."
SEVERITIES = ("high", "medium", "low")
SKIPPED = {"_skipped": True}
# An arm whose replay a restart or cancel ended. A rerun queues it again, never one that finished.
RETRIED = ("failed", "skipped")


class ReplayError(ToolError):
    pass


# What a replay serves and runs

def _stages(text: str | None) -> list[dict[str, Any]]:
    data = worker._load(text)
    stages = data.get("stages") if isinstance(data, dict) else None
    return [s for s in stages if isinstance(s, dict) and isinstance(s.get("id"), str)] \
        if isinstance(stages, list) else []


def model_stages(texts: list[str]) -> tuple[list[str], list[str]]:
    """The agent stages a replay runs on its alias, and the model stages it can't run live, by id."""
    return _model_stages(tuple(texts))


@functools.lru_cache(maxsize=32)
def _model_stages(texts: tuple[str, ...]) -> tuple[list[str], list[str]]:
    found, refused = set(), set()
    for raw in (stage for text in texts for stage in _stages(text)):
        if raw.get("agent") is None and raw.get("role") is None:
            continue
        try:
            runs_live = stub.live(Stage.model_validate(raw))
        except ValidationError:
            runs_live = False
        (found if runs_live else refused).add(raw["id"])
    return sorted(found), sorted(refused)


def _cut(output: Any) -> bool:
    return isinstance(output, dict) and bool(output.get(ingest.TRUNCATED))


def served(conn: Connection, run_id: str) -> dict[str, Any]:
    """Every recorded output of the run by step key, and a skipped step's skip, so a live answer that flips a later
    skip_if still gets the recorded one."""
    rows = conn.execute(select(run_steps.c.step_key, run_steps.c.status, run_steps.c.output).where(
        run_steps.c.run_id == run_id)).all()
    found = {r.step_key: r.output for r in rows if r.output is not None}
    found.update({r.step_key: dict(SKIPPED) for r in rows if r.output is None and r.status == "skipped"})
    return found


def stubs_for(conn: Connection, arm_id: str) -> dict[str, Any] | None:
    """A replay arm's stub outputs, from the round it repeats. None for an arm of any other experiment."""
    row = conn.execute(select(eval_arms.c.primary_run_id, eval_experiments.c.specialist)
                       .join(eval_experiments, eval_experiments.c.id == eval_arms.c.experiment_id)
                       .where(eval_arms.c.id == arm_id)).first()
    if row is None or row.specialist != arms.PANEL:
        return None
    stubs = served(conn, row.primary_run_id)
    if not stubs:
        raise ReplayError(409, "The round this replay repeats has no recorded outputs.")
    cut = sorted(key for key, output in stubs.items() if _cut(output))
    if cut:
        raise ReplayError(409, f"The round this replay repeats has truncated outputs: {', '.join(cut)}.")
    return stubs


def _bodies(conn: Connection, source: Any) -> list[str]:
    """The source job's workflow body at its version, and its callees' at its pins. Raises ReplayError."""
    workflow = conn.execute(select(workflows.c.id, workflows.c.name, workflows.c.scope)
                            .where(workflows.c.id == source.workflow_id)).first()
    body = defs.body_of(conn, defs.WORKFLOW, workflow.id, source.workflow_version) if workflow else None
    if body is None:
        raise ReplayError(409, "The round's workflow version is gone.")
    try:
        files = worker.bundle_calls(conn, source, workflow, body)
    except worker.JobError as exc:
        raise ReplayError(409, str(exc)) from exc
    return [body, *(text for name, text in files.items() if name != calls.MANIFEST)]


# Which rounds a replay repeats

def _why_not(conn: Connection, source: Any, parsed: dict[tuple[Any, ...], Any]) -> tuple[str | None, list[str]]:
    """Why the round can't be replayed, or None, with its workflow bodies when it can. ``parsed`` caches each
    workflow version's bodies, and stored outputs are read only once every cheaper check passed."""
    if source.job_id is None or source.workflow_version is None:
        return RUNNER, []
    if conn.execute(select(run_steps.c.step_id).where(run_steps.c.run_id == source.id, run_steps.c.step_id == CONTEXT,
                                                      run_steps.c.status == "succeeded").limit(1)).first() is None:
        return NO_CONTEXT, []
    key = (source.workflow_id, source.workflow_version, json.dumps(source.call_pins or {}, sort_keys=True))
    if key not in parsed:
        try:
            texts = _bodies(conn, source)
            refused = model_stages(texts)[1]
            parsed[key] = (texts, f"Its workflow has a model step a replay can't run on another model: "
                                  f"{', '.join(refused)}." if refused else None)
        except ReplayError as exc:
            parsed[key] = ([], exc.message)
    texts, reason = parsed[key]
    if reason:
        return reason, []
    cut = sorted(r.step_key for r in conn.execute(select(run_steps.c.step_key, run_steps.c.output).where(
        run_steps.c.run_id == source.id, run_steps.c.output.is_not(None))) if _cut(r.output))
    if cut:
        return f"A recorded output is truncated: {', '.join(cut)}.", []
    return None, texts


def _number(subject: str) -> int:
    return int(subject.rpartition("#")[2]) if subject.rpartition("#")[2].isdigit() else 0


def select_rounds(conn: Connection, team_id: str, project_id: str, *, include_merged: bool, prs: list[int] | None,
                  max_rounds: int) -> tuple[list[tuple[Any, list[str]]], list[dict[str, Any]], list[dict[str, Any]]]:
    """The rounds to replay with their workflow bodies, the rounds skipped with why, and the pull requests
    max_rounds left out.

    Each pull request that is open, or merged when ``include_merged``, and had two or more rounds counts, with the
    first run of each round in start order. Whole pull requests are taken, most replayable rounds first, then by
    number, while their rounds keep the total at or under ``max_rounds``. One that doesn't fit is left out, and a
    smaller one after it may still fit.
    """
    name = func.coalesce(workflows.c.name, runs.c.workflow_name)
    rows = conn.execute(
        select(runs.c.id, runs.c.subject, runs.c.review_round, runs.c.trigger_ref, runs.c.cost_usd,
               jobs.c.id.label("job_id"), jobs.c.workflow_id, jobs.c.workflow_version, jobs.c.inputs,
               jobs.c.call_pins, jobs.c.dry_run)
        .select_from(runs.outerjoin(workflows, workflows.c.id == runs.c.workflow_id)
                     .outerjoin(jobs, and_(jobs.c.run_id == runs.c.id, jobs.c.resume_of_gate.is_(None))))
        .where(runs.c.team_id == team_id, runs.c.project_id == project_id, runs.c.status == "succeeded",
               name == SOURCE, runs.c.review_round.is_not(None), runs.c.subject.is_not(None),
               arms.not_arm(runs.c.id))
        .order_by(runs.c.started_at, runs.c.id)).all()
    by_pr: dict[str, dict[int, Any]] = {}
    for row in rows:
        if row.dry_run or (row.trigger_ref or {}).get("dry_run") is True:
            continue
        by_pr.setdefault(row.subject.lower(), {}).setdefault(row.review_round, row)
    states = ("open", "merged") if include_merged else ("open",)
    wanted = set(prs or ())
    parsed: dict[tuple[Any, ...], Any] = {}
    found: list[tuple[str, list[Any], list[dict[str, Any]]]] = []
    for subject, by_round in by_pr.items():
        if max(by_round) < 2 or (wanted and _number(subject) not in wanted):
            continue
        first = next(iter(by_round.values()))
        pr_id = rounds._pull_request(conn, first.subject, team_id, project_id)
        state = conn.execute(select(gh_pull_requests.c.state).where(gh_pull_requests.c.id == pr_id)).scalar() \
            if pr_id else None
        if state not in states:
            continue
        chosen, skipped = [], []
        for number in sorted(by_round):
            source = by_round[number]
            reason, texts = _why_not(conn, source, parsed)
            if reason:
                skipped.append({"subject": source.subject, "round": number, "run_id": source.id, "reason": reason})
            else:
                chosen.append((source, texts))
        found.append((first.subject, chosen, skipped))
    found.sort(key=lambda f: (-len(f[1]), _number(f[0]), f[0]))
    picked: list[tuple[Any, list[str]]] = []
    skipped: list[dict[str, Any]] = []
    left_out: list[dict[str, Any]] = []
    for subject, chosen, skipped_here in found:
        if len(picked) + len(chosen) > max_rounds:
            left_out.append({"subject": subject, "rounds": len(chosen)})
            continue
        picked += chosen
        skipped += skipped_here
    return picked, skipped, left_out


# Starting a replay

def _engine(call: Call) -> Engine:
    if call.engine is None:
        raise ReplayError(500, "The replay tools need the server's database.")
    return call.engine


def _authorized(conn: Connection, call: Call) -> Any:
    """The calling run's job, refused unless it is a run of the built-in kimi-evaluate an admin started."""
    job = conn.execute(select(jobs).where(jobs.c.run_id == call.run_id, jobs.c.team_id == call.team_id)).first()
    builtin = job is not None and conn.execute(select(workflows.c.id).where(
        workflows.c.id == job.workflow_id, workflows.c.name == WORKFLOW, workflows.c.built_in.is_(True))).first()
    admin = job is not None and job.requested_by is not None and conn.execute(select(users.c.id).where(
        users.c.id == job.requested_by, users.c.is_admin.is_(True), users.c.disabled.is_(False))).first()
    if not builtin or not admin:
        raise ReplayError(403, NOT_ADMIN)
    if job.project_id is None:
        raise ReplayError(400, "Start kimi-evaluate in the project whose reviews it replays.")
    # Only queue() keys a job so, which keeps a handoff out and one start per project at a time.
    if job.concurrency_key != f"{KEY}{job.project_id}":
        raise ReplayError(403, NOT_ADMIN)
    return job


def _variant(conn: Connection, team_id: str, project_id: str, alias: str) -> tuple[Any, str]:
    """The project's own provider for ``alias`` and the model it names, refused unless an admin saved it."""
    if not defs.ALIAS.fullmatch(alias):
        raise ReplayError(400, "alias must be a model alias, such as kimi.")
    found = defs.resolve_alias(conn, alias, team_id, project_id)
    if found is None or found[0].scope != project_id:
        raise ReplayError(400, f"No provider of this project defines the alias {alias}.")
    row, model = found
    if not llm.saved_by_admin(conn, row):
        raise ReplayError(403, f"An admin must save the provider for {alias} before reviews replay on it.")
    config = row.config or {}
    if config.get("api_base") and not (config.get("prices") or {}).get(alias):
        raise ReplayError(400, f"Set a price for {alias} on its provider first, so its cost can be compared.")
    return row, model


def _inputs(params: dict[str, Any]) -> dict[str, Any]:
    given = {**DEFAULTS, **{k: v for k, v in params.items() if v is not None}}
    for name, (low, high) in LIMITS.items():
        value = given[name]
        if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high \
                or (name != "budget_usd" and not isinstance(value, int)):
            raise ReplayError(400, f"{name} must be a number from {low} to {high}.")
    prs = given.get("prs") or []
    if not isinstance(prs, list) or not all(isinstance(n, int) and not isinstance(n, bool) for n in prs):
        raise ReplayError(400, "prs is a list of pull request numbers.")
    return {**given, "include_merged": given["include_merged"] is not False, "prs": prs}


def find(conn: Connection, team_id: str, project_id: str, alias: str) -> Any:
    """The project's replay experiment on ``alias``, or None."""
    rows = conn.execute(select(eval_experiments).where(
        eval_experiments.c.team_id == team_id, eval_experiments.c.project_id == project_id,
        eval_experiments.c.specialist == arms.PANEL).order_by(eval_experiments.c.created_at, eval_experiments.c.id))
    return next((r for r in rows if (r.variant or {}).get("alias") == alias), None)


def _experiment(conn: Connection, job: Any, alias: str, model: str, given: dict[str, Any], now: datetime) -> Any:
    found = find(conn, job.team_id, job.project_id, alias)
    limits = {"daily_budget_usd": given["budget_usd"], "max_concurrent_arms": given["max_in_flight"]}
    if found is not None:
        conn.execute(update(eval_experiments).where(eval_experiments.c.id == found.id).values(
            variant={"model": model, "alias": alias}, **limits))
        return conn.execute(select(eval_experiments).where(eval_experiments.c.id == found.id)).one()
    try:
        baseline = experiments.current_setting(conn, job.team_id, job.project_id, LEAD)[0]
    except experiments.ExperimentError:
        baseline = {"alias": None, "model": None, "effort": None}
    experiment_id = new_id()
    # Finished from the start, so nothing samples, judges, or scores it as a specialist experiment.
    conn.execute(insert(eval_experiments).values(
        id=experiment_id, team_id=job.team_id, project_id=job.project_id, specialist=arms.PANEL, variable="model",
        baseline=baseline, variant={"model": model, "alias": alias}, sample_rate=1.0, status="finished",
        created_at=now, created_by=job.requested_by, **limits))
    return conn.execute(select(eval_experiments).where(eval_experiments.c.id == experiment_id)).one()


def _spent(conn: Connection, experiment_id: str) -> Decimal:
    """What every replay run of the experiment cost, a failed attempt's included."""
    total = conn.execute(select(func.coalesce(func.sum(runs.c.cost_usd), 0))
                         .select_from(jobs.join(runs, runs.c.id == jobs.c.run_id)
                                      .join(eval_arms, eval_arms.c.id == jobs.c.arm_id))
                         .where(eval_arms.c.experiment_id == experiment_id)).scalar_one()
    return Decimal(str(total))


def _judge_spent(stored: Any) -> Decimal:
    """What the judge cost over every earlier compare of the experiment, as its stored report carries it."""
    found = (stored or {}).get("judge") if isinstance(stored, dict) else None
    found = found if isinstance(found, dict) else {}
    value = found.get("total_cost_usd", found.get("cost_usd"))
    return Decimal(str(value)) if isinstance(value, int | float) and not isinstance(value, bool) else Decimal(0)


def _arm_costs(conn: Connection, experiment_id: str) -> list[Any]:
    return conn.execute(select(eval_arms.c.status, eval_arms.c.cost_usd).where(
        eval_arms.c.experiment_id == experiment_id)).all()


def _estimate(conn: Connection, experiment: Any, cost: Any) -> Decimal:
    """What one replay is expected to cost: the mean of the experiment's finished replays, else the round's own
    ``cost``, else the arm estimate."""
    done = [Decimal(str(r.cost_usd)) for r in _arm_costs(conn, experiment.id)
            if r.status == "done" and r.cost_usd is not None]
    if done:
        return sum(done) / len(done)
    if cost:
        return Decimal(str(cost))
    return arms.expected_cost(conn, experiment)


def _requires(texts: list[str]) -> list[str]:
    return list(_requirements(tuple(texts)))


@functools.lru_cache(maxsize=32)
def _requirements(texts: tuple[str, ...]) -> tuple[str, ...]:
    found = worker.requirements_with_calls(texts[0], texts[1:]) or []
    return tuple(sorted({*(r for r in found if r in RUNS_WITH), worker.EVAL_ARM, worker.STUBS_CAPABILITY}))


def _queue(conn: Connection, experiment: Any, source: Any, texts: list[str], alias: str, slot: int,
           now: datetime, retry: str | None = None) -> bool:
    """Arm the round, or reset its ``retry`` arm whose replay failed, and queue its replay. False when another start
    armed or reset it first."""
    overrides = {stage: {"alias": alias} for stage in model_stages(texts)[0]}
    if retry is not None:
        arm_id = retry
        if conn.execute(update(eval_arms).where(eval_arms.c.id == retry, eval_arms.c.status.in_(RETRIED)).values(
                status="queued", arm_run_id=None, reason=None, cost_usd=None, latency_s=None,
                overrides=overrides)).rowcount != 1:
            return False
    else:
        arm_id = new_id()
        head = str((source.inputs or {}).get("ref") or "")[:40]
        try:
            with conn.begin_nested():
                conn.execute(insert(eval_arms).values(
                    id=arm_id, team_id=experiment.team_id, experiment_id=experiment.id, primary_run_id=source.id,
                    head_sha=head, label=LABEL, overrides=overrides, status="queued", created_at=now))
        except IntegrityError:
            return False
    # The replay's own key, never the review's, with one key per slot so at most max_in_flight run at once.
    worker.queue_job(conn, team_id=experiment.team_id, project_id=experiment.project_id,
                     workflow_id=source.workflow_id, inputs=source.inputs, now=now,
                     key=f"{KEY}{experiment.id}#arm:{slot}", arm_id=arm_id,
                     pinned={"workflow_version": source.workflow_version, "call_pins": source.call_pins,
                             "requires": _requires(texts), "dry_run": True})
    return True


def _takers(conn: Connection, now: datetime) -> list[Any]:
    """environments.takers, with the server's own worker, which takers lists last, offering only what it claims
    with: never an agent harness."""
    found = environments.takers(conn, now)
    enabled = conn.execute(select(settings.c.value).where(settings.c.scope == INSTANCE_SCOPE,
                                                          settings.c.key == worker.ENABLED_KEY)).scalar()
    if enabled is not False and found:
        embedded = found[-1]
        found[-1] = environments.Taker(embedded.team_id, [c for c in embedded.capabilities or []
                                                          if c not in AGENT_HARNESSES])
    return found


def start(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """eval.replay-start: arm and queue every replayable round not armed yet, and again each whose replay failed,
    within the budget. Safe to rerun."""
    given = _inputs(params)
    alias = given["alias"]
    engine = _engine(call)
    # Selecting reads many runs, so it runs before the transaction that locks the experiment's row.
    with engine.connect() as conn:
        job = _authorized(conn, call)
        _, model = _variant(conn, job.team_id, job.project_id, alias)
        chosen, skipped, left_out = select_rounds(conn, job.team_id, job.project_id,
                                                  include_merged=given["include_merged"], prs=given["prs"],
                                                  max_rounds=given["max_rounds"])
    with engine.begin() as conn:
        job = _authorized(conn, call)
        experiment = _experiment(conn, job, alias, model, given, call.now)
        armed = dict(conn.execute(select(eval_arms.c.primary_run_id, eval_arms.c.id).where(
            eval_arms.c.experiment_id == experiment.id, eval_arms.c.status.not_in(RETRIED))).all())
        retry = dict(conn.execute(select(eval_arms.c.primary_run_id, eval_arms.c.id).where(
            eval_arms.c.experiment_id == experiment.id, eval_arms.c.status.in_(RETRIED))).all())
        new = [(source, texts) for source, texts in chosen if source.id not in armed]
        if new and not environments.taken(SimpleNamespace(team_id=job.team_id, requires=_requires(new[0][1])),
                                          _takers(conn, call.now)):
            raise ReplayError(409, NO_RUNNER)
        active = sum(1 for r in _arm_costs(conn, experiment.id) if r.status in arms.ACTIVE)
        stored = conn.execute(select(eval_experiments.c.report).where(eval_experiments.c.id == experiment.id)
                              .with_for_update()).scalar()
        committed = _spent(conn, experiment.id) + _judge_spent(stored)
        if active:
            committed += active * _estimate(conn, experiment, None)
        budget = Decimal(str(given["budget_usd"]))
        queued, retried, over = 0, 0, []
        for i, (source, texts) in enumerate(new):
            cost = _estimate(conn, experiment, source.cost_usd)
            if committed + cost > budget:
                over = [{"subject": s.subject, "round": s.review_round, "run_id": s.id} for s, _ in new[i:]]
                break
            if _queue(conn, experiment, source, texts, alias, (active + queued) % given["max_in_flight"], call.now,
                      retry.get(source.id)):
                committed += cost
                queued += 1
                retried += source.id in retry
        selection = {"skipped": skipped, "left_out": left_out, "over_budget": over, "max_rounds": given["max_rounds"],
                     "include_merged": given["include_merged"], "prs": given["prs"]}
        conn.execute(update(eval_experiments).where(eval_experiments.c.id == experiment.id).values(
            report={**(stored or {}), "selection": selection}))
        audit.record(conn, action="insights.replay.start", actor_id=job.requested_by, team_id=job.team_id,
                     target_kind="experiment", target_id=experiment.id,
                     detail={"alias": alias, "queued": queued, "retried": retried, "rounds": len(chosen),
                             "over_budget": len(over),
                             **{k: given[k] for k in ("budget_usd", "max_in_flight", "max_rounds")}})
    return {"experiment": experiment.id, "queued": queued, "retried": retried, "already": len(chosen) - len(new),
            "skipped": len(skipped), "over_budget": len(over), "left_out": len(left_out),
            "pending": active + queued}


def queue(conn: Connection, *, team_id: str, project_id: str, admin_id: str, params: dict[str, Any],
          now: datetime) -> str:
    """Queue a kimi-evaluate run of the project for an admin. Returns its job id. Raises ReplayError."""
    from armature.server import conversations

    given = _inputs(params)
    try:
        workflow_id = conversations.builtin_workflow(conn, WORKFLOW, now)
    except conversations.ConversationError as exc:
        raise ReplayError(exc.status, exc.message) from exc
    job_id = worker.queue_job(conn, team_id=team_id, project_id=project_id, workflow_id=workflow_id, inputs=given,
                              now=now, key=f"{KEY}{project_id}", requested_by=admin_id)
    audit.record(conn, action="insights.replay.queue", actor_id=admin_id, team_id=team_id, target_kind="job",
                 target_id=job_id, detail={"project_id": project_id, **given})
    return job_id


def report(call: Call, params: dict[str, Any]) -> dict[str, Any]:
    """eval.replay-report: the experiment's report as it stands, and how many replays are still to finish."""
    alias = params.get("alias") or DEFAULTS["alias"]
    with _engine(call).connect() as conn:
        job = _authorized(conn, call)
        experiment = find(conn, job.team_id, job.project_id, alias)
        if experiment is None:
            return {"experiment": None, "pending": 0, "report": None}
        statuses = Counter(r.status for r in _arm_costs(conn, experiment.id))
    stored = experiment.report or {}
    shown = {k: stored.get(k) for k in ("compared_at", "totals", "judge", "file")}
    shown["prs"] = [{"subject": p["subject"], "totals": p["totals"]} for p in stored.get("prs") or []]
    shown["skipped"] = len(stored.get("skipped") or (stored.get("selection") or {}).get("skipped") or [])
    return {"experiment": experiment.id, "pending": sum(statuses[s] for s in arms.ACTIVE), "arms": dict(statuses),
            "report": shown if stored.get("compared_at") else None}


# What admins read

def listed(conn: Connection) -> list[dict[str, Any]]:
    """Every replay experiment, newest first, with how far it got."""
    rows = conn.execute(select(eval_experiments.c.id, eval_experiments.c.variant, eval_experiments.c.report,
                               eval_experiments.c.created_at, teams.c.name.label("team"),
                               projects.c.name.label("project"))
                        .join(teams, teams.c.id == eval_experiments.c.team_id)
                        .outerjoin(projects, projects.c.id == eval_experiments.c.project_id)
                        .where(eval_experiments.c.specialist == arms.PANEL)
                        .order_by(eval_experiments.c.created_at.desc(), eval_experiments.c.id)).all()
    statuses: dict[str, Counter] = {}
    for arm in conn.execute(select(eval_arms.c.experiment_id, eval_arms.c.status).where(
            eval_arms.c.experiment_id.in_([r.id for r in rows]))):
        statuses.setdefault(arm.experiment_id, Counter())[arm.status] += 1
    return [{"id": r.id, "team": r.team, "project": r.project, "alias": (r.variant or {}).get("alias"),
             "model": (r.variant or {}).get("model"), "created_at": r.created_at.isoformat(),
             "compared_at": (r.report or {}).get("compared_at"),
             "rounds": ((r.report or {}).get("totals") or {}).get("rounds"),
             "arms": dict(statuses.get(r.id, {}))} for r in rows]


def shown(conn: Connection, experiment_id: str) -> dict[str, Any] | None:
    """A replay experiment's report as the admin view draws it, without the judge's raw answers."""
    found = next((r for r in listed(conn) if r["id"] == experiment_id), None)
    if found is None:
        return None
    stored = conn.execute(select(eval_experiments.c.report).where(eval_experiments.c.id == experiment_id)).scalar()
    report_data = {k: v for k, v in (stored or {}).items() if k != "judgments"}
    return {**found, "report": report_data if report_data.get("compared_at") else None}


# Comparing

def due(conn: Connection, experiment: Any, changed: bool) -> bool:
    """Whether every arm of the replay experiment ended and the report doesn't cover them yet."""
    statuses = [r.status for r in _arm_costs(conn, experiment.id)]
    if not statuses or any(s in arms.ACTIVE for s in statuses):
        return False
    stored = experiment.report or {}
    return changed or not stored.get("compared_at") or bool(stored.get("error")) \
        or bool((stored.get("judge") or {}).get("unavailable"))


def failed(engine: Engine, experiment_id: str, error: str) -> None:
    """Record that a compare failed, so the next tick tries again."""
    with engine.begin() as conn:
        found = conn.execute(select(eval_experiments.c.report).where(eval_experiments.c.id == experiment_id)
                             .with_for_update()).scalar()
        conn.execute(update(eval_experiments).where(eval_experiments.c.id == experiment_id).values(
            report={**(found or {}), "error": error}))


def _finding(row: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(finding_key=row.get("finding_key"), path=row.get("path"), line=row.get("line"),
                           claim=row.get("title") or row.get("body"), body=row.get("body"), suggestion=None,
                           severity=None, blocking=None, decision=row.get("decision"), reason=row.get("reason"),
                           lineage_key=row.get("lineage_key") or row.get("finding_key"))


def _lead_findings(output: dict[str, Any]) -> list[SimpleNamespace]:
    """The lead's decisions as findings rows record them, keyed as review.decisions keys them."""
    given = [d for d in output.get("decisions") or [] if isinstance(d, dict)]
    return [_finding({**ingest.finding_row(None, None, datetime.min, payload), "title": payload.get("claim")})
            for payload in marks.decisions(None, {"decisions": given})["findings"]]


def _graded(found: list[SimpleNamespace], lead: Any) -> list[SimpleNamespace]:
    """The findings with the severity and blocking of the comment their lead posted for each, so both sides are
    graded from the same source. One the lead posted nothing for stays unknown."""
    comments = [c for c in (lead or {}).get("comments") or [] if isinstance(c, dict)] if isinstance(lead, dict) \
        else []
    for f in found:
        comment = next((c for c in comments if (c.get("finding_id") and c.get("finding_id") == f.finding_key)
                        or review.finding_key(c) == f.finding_key), {})
        f.severity = comment.get("severity") if comment.get("severity") in SEVERITIES else None
        f.blocking = comment.get("blocking") if isinstance(comment.get("blocking"), bool) else None
        f.body = comment["body"] if isinstance(comment.get("body"), str) and comment["body"] else f.body
    return found


def _raised(steps: list[Any]) -> dict[str, int]:
    found: dict[str, int] = {}
    for s in steps:
        items = s.output.get("findings") if isinstance(s.output, dict) and not _cut(s.output) else None
        if s.step_id in risk.SPECIALISTS and isinstance(items, list):
            found[s.step_id] = found.get(s.step_id, 0) + len(items)
    return found


def _output(steps: list[Any], step_id: str) -> Any:
    return next((s.output for s in sorted(steps, key=lambda s: s.step_key) if s.step_id == step_id), None)


def _run(conn: Connection, run_id: str) -> dict[str, Any]:
    row = conn.execute(select(runs.c.cost_usd, runs.c.input_tokens, runs.c.output_tokens, runs.c.started_at,
                              runs.c.finished_at).where(runs.c.id == run_id)).first()
    if row is None:
        return {}
    wall = (row.finished_at - row.started_at).total_seconds() if row.finished_at and row.started_at else None
    return {"cost_usd": float(row.cost_usd) if row.cost_usd is not None else None,
            "input_tokens": row.input_tokens, "output_tokens": row.output_tokens, "wall_s": wall,
            "minutes": round(wall / 60, 1) if wall is not None else None}


def _later_lineages(conn: Connection, experiment: Any, subject: str) -> dict[int, set[str]]:
    """Each Claude round of the pull request with the lineages its findings carry, none for a round with none."""
    found: dict[int, set[str]] = {}
    for row in conn.execute(select(runs.c.review_round, findings.c.lineage_key, findings.c.finding_key)
                            .select_from(runs.outerjoin(findings, findings.c.run_id == runs.c.id))
                            .where(runs.c.team_id == experiment.team_id, runs.c.project_id == experiment.project_id,
                                   func.lower(runs.c.subject) == subject.lower(), runs.c.review_round.is_not(None),
                                   arms.not_arm(runs.c.id))):
        lineages = found.setdefault(row.review_round, set())
        if row.lineage_key or row.finding_key:
            lineages.add(row.lineage_key or row.finding_key)
    return found


def _rounds(conn: Connection, experiment: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Each replay that finished with its two sides paired, and each that didn't with why."""
    rows = conn.execute(select(eval_arms.c.primary_run_id, eval_arms.c.arm_run_id, eval_arms.c.status,
                               eval_arms.c.reason, eval_arms.c.head_sha, runs.c.subject, runs.c.review_round)
                        .join(runs, runs.c.id == eval_arms.c.primary_run_id)
                        .where(eval_arms.c.experiment_id == experiment.id)
                        .order_by(runs.c.subject, runs.c.review_round, runs.c.id)).all()
    done, skipped = [], []
    lineages: dict[str, dict[int, set[str]]] = {}
    for arm in rows:
        where = {"subject": arm.subject, "round": arm.review_round, "run_id": arm.primary_run_id,
                 "replay_run_id": arm.arm_run_id}
        if arm.status != "done" or arm.arm_run_id is None:
            skipped.append({**where, "reason": arm.reason or UNFINISHED})
            continue
        replayed = conn.execute(select(run_steps.c.step_key, run_steps.c.step_id, run_steps.c.output).where(
            run_steps.c.run_id == arm.arm_run_id)).all()
        lead = _output(replayed, LEAD)
        if not isinstance(lead, dict) or _cut(lead):
            skipped.append({**where, "reason": NO_LEAD})
            continue
        source = conn.execute(select(run_steps.c.step_key, run_steps.c.step_id, run_steps.c.output).where(
            run_steps.c.run_id == arm.primary_run_id)).all()
        claude = _graded([_finding(dict(r._mapping)) for r in conn.execute(
            select(findings.c.finding_key, findings.c.path, findings.c.line, findings.c.decision,
                   findings.c.reason, findings.c.lineage_key, findings.c.title, findings.c.body)
            .where(findings.c.run_id == arm.primary_run_id).order_by(findings.c.created_at, findings.c.id))],
            _output(source, LEAD))
        by_round = lineages.setdefault(arm.subject.lower(), _later_lineages(conn, experiment, arm.subject))
        later_rounds = [found for number, found in by_round.items() if number > arm.review_round]
        later = {lin for found in later_rounds for lin in found}
        for f in claude:
            f.fixed = bool(later_rounds) and f.decision in KEPT and f.lineage_key not in later
        rate, claude_rate, claude_risk = (_output(replayed, RATE), _output(source, RATE), _output(source, RISK))
        claude_lead = _output(source, LEAD)
        done.append({**where, "claude": claude, "kimi": _graded(_lead_findings(lead), lead), "head": arm.head_sha,
                     "verdict": {"claude": claude_lead.get("verdict") if isinstance(claude_lead, dict) else None,
                                 "kimi": lead.get("verdict")},
                     "raised": {"claude": _raised(source), "kimi": _raised(replayed)},
                     "rate": {"kimi": (rate or {}).get("level"), "claude": (claude_rate or {}).get("level"),
                              "risk": (claude_risk or {}).get("level"), "panel": (claude_risk or {}).get("panel")},
                     "runs": {"claude": _run(conn, arm.primary_run_id), "kimi": _run(conn, arm.arm_run_id)}})
    return done, skipped


def pair(done: list[dict[str, Any]]) -> None:
    """Mark each finding either side raised that the other raised too, and give each its judgment key."""
    for entry in done:
        for side in ("claude", "kimi"):
            for i, f in enumerate(entry[side]):
                f.matched, f.key = False, f"{entry['run_id']}|{side}|{f.finding_key or i}"
        entry["pairs"] = arm_matching.assign(entry["claude"], entry["kimi"])
        for left, right, _, _ in entry["pairs"]:
            if right is not None:
                left.matched = right.matched = True


def _judge(engine: Engine, keyring: Any, experiment: Any, done: list[dict[str, Any]], known: dict[str, bool],
           now: datetime) -> dict[str, Any]:
    """Rate every finding only one side raised as valid or not, blinded and shuffled per round, within the budget.
    Judgments an earlier report made are kept, and so is the running total of what judging cost."""
    earlier = _judge_spent(experiment.report)
    work = []
    for entry in done:
        items = [(f.key, f) for side in ("claude", "kimi") for f in entry[side] if not f.matched]
        random.Random(entry["run_id"]).shuffle(items)
        work.append([(key, f) for key, f in items if key not in known])
    totals = labeling.Totals()
    info: dict[str, Any] = {}
    if not any(work):
        return {"judgments": known, "alias": None, "total_cost_usd": float(earlier), **info}
    with engine.connect() as conn:
        try:
            alias = judge.judge_alias(conn, experiment.team_id)[0]
            model = llm.prepare(conn, keyring, experiment.team_id, alias)
        except llm.LLMError as exc:
            return {"judgments": known, "alias": None, "unavailable": exc.message,
                    "total_cost_usd": float(earlier)}
        spent = _spent(conn, experiment.id) + earlier
    budget = Decimal(str(experiment.daily_budget_usd))
    judged = dict(known)
    chunks = [items[i:i + judge.BATCH] for items in work for i in range(0, len(items), judge.BATCH)]
    try:
        for chunk in chunks:
            if spent + Decimal(str(totals.cost_usd)) >= budget:
                info["stopped"] = "The budget ran out before every finding was judged."
                break
            answer = model.complete(judge._system(), [{"role": "user", "content": json.dumps(
                {"items": [judge._item(i, f) for i, (_, f) in enumerate(chunk)]})}], judge.SCHEMA)
            totals.spent(alias, answer)
            given = answer.data.get("judgments") if isinstance(answer.data, dict) else None
            for item in given if isinstance(given, list) else []:
                if isinstance(item, dict) and str(item.get("id")).isdecimal() and int(item["id"]) < len(chunk) \
                        and isinstance(item.get("valid"), bool):
                    judged.setdefault(chunk[int(item["id"])][0], item["valid"])
    except llm.LLMError as exc:
        info["stopped"] = f"The judge stopped: {exc.message}"
    return {"judgments": judged, "alias": totals.labeler or alias, "calls": totals.calls,
            "cost_usd": round(totals.cost_usd, 6), "input_tokens": totals.input_tokens,
            "output_tokens": totals.output_tokens,
            "total_cost_usd": round(float(earlier) + totals.cost_usd, 6), **info}


def _split(items: list[Any]) -> dict[str, Any]:
    return {"total": len(items),
            "severity": {s: sum(1 for f in items if (f.severity if f.severity in SEVERITIES else "unknown") == s)
                         for s in (*SEVERITIES, "unknown")},
            "blocking": {"blocking": sum(1 for f in items if f.blocking is True),
                         "non_blocking": sum(1 for f in items if f.blocking is False),
                         "unknown": sum(1 for f in items if f.blocking is None)}}


TEXT_CAP = 2000


def _card(f: Any, judged: dict[str, bool] | None = None, side: str = "kimi") -> dict[str, Any]:
    """One finding as the report shows it. Its text is review content, so every renderer escapes it."""
    card = {"path": f.path, "line": f.line, "severity": f.severity, "blocking": f.blocking, "decision": f.decision,
            "reason": (f.reason or "")[:TEXT_CAP] or None, "claim": (f.claim or "")[:TEXT_CAP],
            "body": (f.body or "")[:TEXT_CAP], "finding_key": f.finding_key}
    if side == "claude":
        card["fixed"] = bool(f.fixed)
    if judged is not None:
        card["verdict"] = "valid" if judged.get(f.key) is True else "not_valid" if f.key in judged else "not_judged"
    return card


def _round_result(entry: dict[str, Any], judged: dict[str, bool]) -> dict[str, Any]:
    both = [(left, right) for left, right, _, _ in entry["pairs"] if right is not None]
    claude_only = [f for f in entry["claude"] if not f.matched]
    kimi_only = [f for f in entry["kimi"] if not f.matched]

    def valid(items: list[Any]) -> tuple[int, int]:
        return sum(1 for f in items if judged.get(f.key) is True), sum(1 for f in items if f.key not in judged)
    claude_valid, claude_open = valid(claude_only)
    kimi_valid, kimi_open = valid(kimi_only)
    keeps = [(left.decision in KEPT, right.decision in KEPT) for left, right in both]
    return {"subject": entry["subject"], "round": entry["round"], "run_id": entry["run_id"],
            "replay_run_id": entry["replay_run_id"],
            "both": _split([left for left, _ in both]), "claude_only": _split(claude_only),
            "kimi_only": _split(kimi_only),
            "fixed": sum(1 for f in entry["claude"] if f.fixed),
            "fixed_missed": sum(1 for f in claude_only if f.fixed),
            "claude_only_valid": claude_valid, "kimi_only_valid": kimi_valid, "unjudged": claude_open + kimi_open,
            "lead": {"agree": sum(1 for c, k in keeps if c == k),
                     "claude_keep_kimi_drop": sum(1 for c, k in keeps if c and not k),
                     "claude_drop_kimi_keep": sum(1 for c, k in keeps if k and not c)},
            "raised": entry["raised"], "rate": entry["rate"],
            "claude_run": entry["runs"]["claude"], "kimi_run": entry["runs"]["kimi"],
            "head": entry["head"], "verdict": entry["verdict"],
            "findings": {"pairs": [{"claude": _card(left, side="claude"), "kimi": _card(right)}
                                   for left, right in both],
                         "claude_only": [_card(f, judged, "claude") for f in claude_only],
                         "kimi_only": [_card(f, judged) for f in kimi_only]}}


def _add(total: dict[str, Any], part: dict[str, Any]) -> dict[str, Any]:
    for key, value in part.items():
        if isinstance(value, dict):
            _add(total.setdefault(key, {}), value)
        elif isinstance(value, int | float) and not isinstance(value, bool):
            total[key] = (total.get(key) or 0) + value
    return total


TOTALLED = ("both", "claude_only", "kimi_only", "fixed", "fixed_missed", "claude_only_valid", "kimi_only_valid",
            "unjudged", "lead", "claude_run", "kimi_run")


def _totals(results: list[dict[str, Any]]) -> dict[str, Any]:
    total: dict[str, Any] = {"rounds": len(results),
                             "rate_agrees": sum(1 for r in results if r["rate"]["kimi"] == r["rate"]["claude"]),
                             "rate_matches_risk": sum(1 for r in results if r["rate"]["kimi"] == r["rate"]["risk"]),
                             "verdict_agrees": sum(1 for r in results if (r.get("verdict") or {}).get("kimi")
                                                   and (r.get("verdict") or {}).get("kimi")
                                                   == (r.get("verdict") or {}).get("claude"))}
    for result in results:
        _add(total, {k: result[k] for k in TOTALLED})
    return total


def build(done: list[dict[str, Any]], skipped: list[dict[str, Any]], judged: dict[str, Any], experiment: Any,
          now: datetime, selection: dict[str, Any]) -> dict[str, Any]:
    """The report of rounds ``pair`` marked: per round, per pull request, and in total, with every skipped round
    and why."""
    results = [_round_result(entry, judged.get("judgments") or {}) for entry in done]
    prs: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        prs.setdefault(result["subject"], []).append(result)
    return {"version": 2, "experiment": experiment.id, "alias": (experiment.variant or {}).get("alias"),
            "model": (experiment.variant or {}).get("model"), "compared_at": now.isoformat(),
            "judge": {k: v for k, v in judged.items() if k != "judgments"},
            "totals": _totals(results),
            "prs": [{"subject": subject, "totals": _totals(rows), "rounds": rows} for subject, rows in prs.items()],
            "skipped": [*(selection.get("skipped") or []), *skipped],
            "selection": selection, "judgments": judged.get("judgments") or {}}


def compare(engine: Engine, keyring: Any, experiment: Any, now: datetime, reports_dir: str | None = None) -> dict:
    """Compare every finished replay of the experiment with its Claude round, and store the report."""
    with engine.connect() as conn:
        done, skipped = _rounds(conn, experiment)
    pair(done)
    known = {k: v for k, v in ((experiment.report or {}).get("judgments") or {}).items() if isinstance(v, bool)}
    judged = _judge(engine, keyring, experiment, done, known, now)
    name = f"kimi-evaluate-{experiment.id}.html"
    with engine.begin() as conn:
        # A start may have stored a newer selection since this compare read the experiment.
        stored = conn.execute(select(eval_experiments.c.report).where(eval_experiments.c.id == experiment.id)
                              .with_for_update()).scalar()
        built = build(done, skipped, judged, experiment, now, (stored or {}).get("selection") or {})
        if reports_dir is not None:
            try:
                report_file.write_file(reports_dir, name, replay_html.render(built))
                built["file"] = name
            except (OSError, report_file.Unsafe) as exc:
                log.error("the replay report of %s is stored but its file wasn't written: %s", experiment.id, exc)
                built["error"] = f"The report file wasn't written: {exc}"
        conn.execute(update(eval_experiments).where(eval_experiments.c.id == experiment.id).values(report=built))
        if judged.get("calls"):
            audit.record(conn, action="insights.replay.judge", actor_id=None, team_id=experiment.team_id,
                         target_kind="experiment", target_id=experiment.id,
                         detail={k: judged.get(k) for k in ("alias", "calls", "cost_usd", "input_tokens",
                                                            "output_tokens")})
    return built

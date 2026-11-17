import asyncio
import json
import subprocess
import time

import pytest

from armature.runtime.engine import Harness
from armature.spec.models import (
    Adapter,
    HarnessSpec,
    ModelTierConfig,
    ModelTiers,
    Role,
    RoleType,
    SandboxConfig,
    SandboxMode,
    Stage,
)
from tests.events.support import DatagramReader


def _spec(**sandbox) -> HarnessSpec:
    return HarnessSpec(
        name="stub-test",
        stages=[
            Stage(id="resolve", adapter="resolve"),
            Stage(id="gate", gate="human", depends_on=["resolve"]),
            Stage(id="review", adapter="review", depends_on=["gate"], fan_out=2,
                  partition_source="{{ resolve.files }}", partition_key="file"),
            Stage(id="lead", role=Role(name="Lead", type=RoleType.WORKER, description="decide", model_tier="small"),
                  depends_on=["review"]),
            Stage(id="post", adapter="post", depends_on=["lead"]),
        ],
        adapters={
            "resolve": Adapter(name="resolve", type="script", cmd="echo resolve"),
            "review": Adapter(name="review", type="script", cmd="echo review"),
            "post": Adapter(name="post", type="script", cmd="echo post"),
        },
        model_tiers=ModelTiers(small=ModelTierConfig(provider="openai", model="gpt-4o-mini")),
        **sandbox,
    )


@pytest.fixture
def no_subprocess(monkeypatch):
    started = []

    def refuse(*args, **kwargs):
        started.append(args)
        raise AssertionError(f"stub mode started a subprocess: {args!r}")

    async def refuse_async(*args, **kwargs):
        refuse(*args)

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", refuse_async)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse_async)
    return started


def _stub(monkeypatch, tmp_path, outputs, default=None):
    path = tmp_path / "stubs.json"
    path.write_text(json.dumps(outputs))
    monkeypatch.setenv("ARMATURE_STUB_OUTPUTS", str(path))
    if default is not None:
        monkeypatch.setenv("ARMATURE_STUB_DEFAULT", default)
    else:
        monkeypatch.delenv("ARMATURE_STUB_DEFAULT", raising=False)


def _no_model(monkeypatch):
    async def refuse(**kwargs):
        raise AssertionError("stub mode called a model")
    monkeypatch.setattr("armature.nodes.llm.litellm_completion", refuse)


async def test_stub_outputs_flow_through_depends_on_and_fan_out_without_a_subprocess(
        monkeypatch, tmp_path, no_subprocess):
    _no_model(monkeypatch)
    _stub(monkeypatch, tmp_path, {
        "resolve": {"files": ["a.py", "b.py"]},
        "review": [{"finding": "a"}, {"finding": "b"}],
        "post": {"posted": False},
    })
    harness = Harness(spec=_spec(sandbox=SandboxConfig(mode=SandboxMode.DOCKER)), session_dir=tmp_path,
                      traces_db=tmp_path / "t.db")
    results = await harness.run({})
    assert results["resolve"] == {"files": ["a.py", "b.py"]}
    assert results["gate"]["approved"] is True
    assert results["review"] == [{"finding": "a"}, {"finding": "b"}]
    assert results["lead"] == {"stub": True}
    assert results["post"] == {"posted": False}
    assert no_subprocess == []


async def test_a_recorded_llm_output_is_used(monkeypatch, tmp_path, no_subprocess):
    _no_model(monkeypatch)
    _stub(monkeypatch, tmp_path, {"resolve": {"files": []}, "review": [], "lead": {"verdict": "approve"},
                                  "post": {}})
    results = await Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db").run({})
    assert results["lead"] == {"verdict": "approve"}


async def test_a_missing_stage_fails_with_stub_missing(monkeypatch, tmp_path, no_subprocess):
    _no_model(monkeypatch)
    _stub(monkeypatch, tmp_path, {"resolve": {"files": []}, "review": []})
    with pytest.raises(ValueError, match="STUB_MISSING: stage 'post'"):
        await Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db").run({})


async def test_default_empty_fills_missing_stages(monkeypatch, tmp_path, no_subprocess):
    _no_model(monkeypatch)
    _stub(monkeypatch, tmp_path, {"resolve": {"files": ["a.py"]}}, default="empty")
    results = await Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db").run({})
    assert results["review"] == [{}]
    assert results["post"] == {}


async def test_stub_mode_emits_events(monkeypatch, tmp_path, no_subprocess):
    _no_model(monkeypatch)
    _stub(monkeypatch, tmp_path, {"resolve": {"files": ["a.py"]}, "review": [{}], "post": {}})
    reader = DatagramReader()
    monkeypatch.setenv("ARMATURE_EVENTS", reader.endpoint)
    monkeypatch.setenv("ARMATURE_EVENTS_TOKEN", "run-token")
    try:
        harness = Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db")
        await harness.run({})
        harness._events.flush(5)
        deadline = time.monotonic() + 5
        while not any(e["kind"] == "run_end" for e in reader.events) and time.monotonic() < deadline:
            time.sleep(0.01)
        kinds = [(e["kind"], e["step_id"]) for e in reader.events]
    finally:
        reader.close()
    assert kinds[0] == ("run_start", None)
    for step in ("resolve", "gate", "review", "lead", "post"):
        assert ("step_end", step) in kinds
    assert reader.events[-1]["payload"]["status"] == "succeeded"


async def test_stub_mode_loads_no_tool_modules_and_reads_no_files(monkeypatch, tmp_path, no_subprocess):
    import pathlib

    from armature.spec.models import ToolModule
    _no_model(monkeypatch)
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read")
    _stub(monkeypatch, tmp_path, {"resolve": {"files": [str(secret)]}, "review": [{}], "post": {}})
    spec = _spec()
    spec.tools = [ToolModule(module="armature_no_such_tool_module")]
    review = next(s for s in spec.stages if s.id == "review")
    review.inject_file_as = "content"
    harness = Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db")
    read = []
    original = pathlib.Path.read_text

    def recording(self, *args, **kwargs):
        read.append(str(self))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(pathlib.Path, "read_text", recording)
    await harness.run({})
    assert str(secret) not in read


async def test_a_partial_stub_serves_every_stage_but_a_session_even_an_llm_stage(monkeypatch, tmp_path,
                                                                                  no_subprocess):
    _no_model(monkeypatch)
    _stub(monkeypatch, tmp_path, {"resolve": {"files": ["a.py"]}, "gate": {"approved": True}, "review": [{"x": 1}],
                                  "lead": {"verdict": "recorded"}, "post": {"posted": False}})
    monkeypatch.setenv("ARMATURE_STUB_LIVE", "agents")
    results = await Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db").run({})
    assert results["lead"] == {"verdict": "recorded"} and results["review"] == [{"x": 1}]
    assert results["post"] == {"posted": False} and no_subprocess == []


async def test_a_partial_stub_never_runs_a_stage_with_no_recorded_output(monkeypatch, tmp_path, no_subprocess):
    _no_model(monkeypatch)
    _stub(monkeypatch, tmp_path, {"resolve": {"files": ["a.py"]}, "gate": {"approved": True}, "review": [{}],
                                  "lead": {}}, default="empty")
    monkeypatch.setenv("ARMATURE_STUB_LIVE", "agents")
    with pytest.raises(ValueError, match="STUB_MISSING: stage 'post'"):
        await Harness(spec=_spec(), session_dir=tmp_path, traces_db=tmp_path / "t.db").run({})
    assert no_subprocess == []


def test_a_partial_stub_runs_only_calls_and_read_only_sessions_with_the_default_tools():
    from armature.nodes.stub import StubMissing, StubOutputs
    from armature.spec.models import AgentStep, CallRef

    partial, whole = StubOutputs({"first/context": {"pr": "x"}}, live_agents=True), StubOutputs({})
    read = Stage(id="rate", agent=AgentStep(model="review", prompt="rate"))
    write = Stage(id="fix", agent=AgentStep(model="review", prompt="fix", access="write"))
    tooled = Stage(id="probe", agent=AgentStep(model="review", prompt="probe", allowed_tools=["Bash"]))
    role = Stage(id="lead", role=Role(name="Lead", type=RoleType.WORKER, description="decide", tools=["shell"]))
    plain_role = Stage(id="judge", role=Role(name="Judge", type=RoleType.WORKER, description="judge"))
    call = Stage(id="first", call=CallRef(workflow="review-pass"))
    adapter, gate = Stage(id="post", adapter="post"), Stage(id="gate", gate="human")
    stages = (read, write, tooled, role, plain_role, call, adapter, gate)
    assert [partial.runs_live(s) for s in stages] == [True, False, False, False, False, True, False, False]
    assert not any(whole.runs_live(s) for s in stages)
    assert partial.output_for(Stage(id="context", adapter="context"), None, "first") == {"pr": "x"}
    with pytest.raises(StubMissing, match="first/context"):
        StubOutputs({"context": {}}, live_agents=True).output_for(Stage(id="context", adapter="c"), None, "first")
    with pytest.raises(StubMissing):
        partial.output_for(gate, None)


def test_a_partial_stub_reads_a_long_step_key_as_the_server_records_it():
    from armature.events.steps import step_key
    from armature.nodes.stub import StubOutputs

    parent = "p" * 200
    stubs = StubOutputs({step_key("context", parent): {"pr": "x"}}, live_agents=True)
    assert len(step_key("context", parent)) == 191
    assert stubs.output_for(Stage(id="context", adapter="context"), None, parent) == {"pr": "x"}


CALLEE = """name: pass
adapters:
  context: {name: context, type: script, cmd: "echo context"}
  post: {name: post, type: script, cmd: "echo post"}
stages:
  - id: context
    adapter: context
    depends_on: []
  - id: post
    adapter: post
    depends_on: [context]
"""


def _calling():
    from armature.spec.calls import MappingCallResolver
    from armature.spec.models import CallRef

    spec = HarnessSpec(name="root", stages=[Stage(id="first", call=CallRef(workflow="pass", version=1))])
    return spec, MappingCallResolver({("pass", 1): CALLEE})


async def test_a_partial_stub_runs_the_call_and_serves_its_stages_by_parent_key(monkeypatch, tmp_path,
                                                                                 no_subprocess):
    _stub(monkeypatch, tmp_path, {"first/context": {"pr": "x"}, "first/post": {"_skipped": True}})
    monkeypatch.setenv("ARMATURE_STUB_LIVE", "agents")
    spec, resolver = _calling()
    results = await Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db",
                            call_resolver=resolver).run({})
    assert results["first"] == {"_skipped": True} and no_subprocess == []


async def test_a_partial_stub_never_serves_a_callees_stage_by_its_bare_id(monkeypatch, tmp_path, no_subprocess):
    _stub(monkeypatch, tmp_path, {"context": {"pr": "x"}, "post": {}})
    monkeypatch.setenv("ARMATURE_STUB_LIVE", "agents")
    spec, resolver = _calling()
    with pytest.raises(Exception, match="STUB_MISSING: stage 'first/context'"):
        await Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db", call_resolver=resolver).run({})
    assert no_subprocess == []


async def test_a_comparison_run_refuses_an_installed_pack_adapter_that_writes(monkeypatch, tmp_path, no_subprocess):
    from armature.spec.models import PACK_ADAPTER_CONTEXT

    monkeypatch.delenv("ARMATURE_STUB_OUTPUTS", raising=False)
    monkeypatch.setenv("ARMATURE_ARM_RUN", "1")
    post = Adapter.model_validate({"name": "review-gh/post", "type": "script", "cmd": "echo posted", "parse": "json",
                                   "pack_dir": str(tmp_path / "review-gh"), "writes": True},
                                  context={PACK_ADAPTER_CONTEXT: True})
    spec = HarnessSpec(name="arm", stages=[Stage(id="post", adapter="review-gh/post")],
                       adapters={"review-gh/post": post})
    with pytest.raises(PermissionError, match="never runs review-gh/post, which writes"):
        await Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db").run({})
    assert no_subprocess == []
    with pytest.raises(ValueError, match="RESERVED_FIELD"):
        Adapter.model_validate({"name": "x", "type": "script", "cmd": "echo", "writes": True})


async def test_a_comparison_run_still_runs_a_built_in_pack_tool_that_writes(monkeypatch, tmp_path):
    from armature.packs import BUILTIN_DIR
    from armature.spec.models import PACK_ADAPTER_CONTEXT

    monkeypatch.delenv("ARMATURE_STUB_OUTPUTS", raising=False)
    monkeypatch.setenv("ARMATURE_ARM_RUN", "1")
    decisions = Adapter.model_validate({"name": "review/decisions", "type": "script",
                                        "cmd": "echo '{\"recorded\": 0, \"dry_run\": true}'", "parse": "json",
                                        "pack_dir": str(BUILTIN_DIR / "review"), "writes": True},
                                       context={PACK_ADAPTER_CONTEXT: True})
    spec = HarnessSpec(name="arm", stages=[Stage(id="decisions", adapter="review/decisions")],
                       adapters={"review/decisions": decisions})
    results = await Harness(spec=spec, session_dir=tmp_path, traces_db=tmp_path / "t.db").run({})
    assert results["decisions"] == {"recorded": 0, "dry_run": True}

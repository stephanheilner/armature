"""Stub mode: every stage returns a recorded output instead of running, so a workflow can be tested for free.

``ARMATURE_STUB_OUTPUTS`` names a JSON file mapping stage id to output. A list
stands for a fan-out stage's branches, one item per branch. A stage with no
entry fails with STUB_MISSING, unless ``ARMATURE_STUB_DEFAULT=empty``, and an
LLM stage with no entry returns a placeholder without calling a model. Gates
approve themselves.

With ``ARMATURE_STUB_LIVE=agents`` the stub is partial, as a replay of a recorded
run needs: calls and read-only agent sessions with the default tools run for real,
and every other stage returns its recorded output, keyed as the server's step key
``<parent>/<stage>`` inside a called workflow. One with no entry fails with
STUB_MISSING, so nothing else ever runs live.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from armature.events.steps import step_key
from armature.nodes.base import BaseNode
from armature.spec.models import AgentStep, Stage

OUTPUTS_ENV = "ARMATURE_STUB_OUTPUTS"
DEFAULT_ENV = "ARMATURE_STUB_DEFAULT"
LIVE_ENV = "ARMATURE_STUB_LIVE"
LIVE_AGENTS = "agents"
PLACEHOLDER = {"stub": True}


class StubMissing(ValueError):
    def __init__(self, stage_id: str, detail: str = "no recorded output"):
        super().__init__(f"STUB_MISSING: stage '{stage_id}' has {detail} in stub mode")


def live(stage: Stage) -> bool:
    """Whether a partial stub would run the stage for real. Nothing with tools of its own ever does."""
    if stage.call is not None:
        return True
    agent = stage.agent
    return isinstance(agent, AgentStep) and agent.access == "read" and agent.allowed_tools is None


class StubOutputs:
    def __init__(self, outputs: dict[str, Any], *, missing_empty: bool = False, live_agents: bool = False):
        self.outputs = outputs
        self.missing_empty = missing_empty and not live_agents
        self.live_agents = live_agents

    @classmethod
    def from_env(cls) -> StubOutputs | None:
        path = os.environ.get(OUTPUTS_ENV, "").strip()
        if not path:
            return None
        outputs = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(outputs, dict):
            raise ValueError(f"{OUTPUTS_ENV} must name a JSON object of stage id to output")
        return cls(outputs, missing_empty=os.environ.get(DEFAULT_ENV, "").strip() == "empty",
                   live_agents=os.environ.get(LIVE_ENV, "").strip() == LIVE_AGENTS)

    def runs_live(self, stage: Stage) -> bool:
        """Whether a partial stub runs the stage for real: a call, or an agent session that only reads with the
        default tools."""
        return self.live_agents and live(stage)

    def output_for(self, stage: Stage, branch: int | None, parent: str | None = None) -> Any:
        key = step_key(stage.id, parent) if self.live_agents else stage.id
        if key not in self.outputs:
            if self.live_agents:
                raise StubMissing(key)
            if stage.gate == "human":
                return {"approved": True, "feedback": None, **PLACEHOLDER}
            if stage.role is not None and not (stage.adapter or stage.tool_call or stage.subagent_spec):
                return dict(PLACEHOLDER)
            if self.missing_empty:
                return {}
            raise StubMissing(stage.id)
        value = self.outputs[key]
        if branch is not None and isinstance(value, list):
            if branch < len(value):
                return copy.deepcopy(value[branch])
            if self.missing_empty:
                return {}
            raise StubMissing(key, f"no recorded output for branch {branch}")
        return copy.deepcopy(value)


class StubNode(BaseNode):
    def __init__(self, stage: Stage, outputs: StubOutputs, branch: int | None = None, parent: str | None = None):
        self._stage = stage
        self._outputs = outputs
        self._branch = branch
        self._parent = parent

    async def execute(self, context: dict[str, Any]) -> Any:
        return self._outputs.output_for(self._stage, self._branch, self._parent)

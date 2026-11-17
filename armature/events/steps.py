"""The server's key for a run's step, which a partial stub reads recorded outputs by too."""
from __future__ import annotations

import hashlib

STEP_KEY_MAX = 191


def step_key(step_id: str, parent_step: str | None) -> str:
    """The run_steps key: a child's step is filed under its parent, so a reused stage id never merges."""
    key = f"{parent_step}/{step_id}" if parent_step else step_id
    if len(key) <= STEP_KEY_MAX:
        return key
    return key[:STEP_KEY_MAX - 41] + "#" + hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()

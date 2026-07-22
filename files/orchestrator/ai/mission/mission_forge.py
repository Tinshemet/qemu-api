"""
mission_forge.py — the mission-authoring wizard.

The tasking twin of forge.py (which authors AGENTS). It reuses the forge
elicitation engine — the same FieldType registry and `elicit_spec` walk a
declarative schema (mission_fields.json) — so authoring a mission and authoring an
agent share one input core. What's mission-specific lives here: prune blank fields
so they inherit the agent's defaults, validate the required title+goal, render a
summary, and seal (encrypt + persist under the agent).

    contracts create agents · agents consume missions
"""
import json
import os
from typing import Callable, Optional

from ..agent import forge as _forge
from . import mission as _mission

_AI = os.path.dirname(__file__)


def _schema() -> dict:
    return json.load(open(os.path.join(_AI, "mission_fields.json")))


def forge_mission_interactive(ask: Callable[[str], str], out: Callable[[str], None],
                              agent: Optional[str] = None) -> Optional[str]:
    """Author a mission through a dialogue: elicit → prune → validate → seal.

    `ask(prompt) -> str` supplies answers (console.input in the CLI; scriptable in
    tests); `out(text)` prints. Returns the sealed .mission path, or None if the
    mission is incomplete or the operator cancels. Nothing is written until sealed.
    """
    schema = _schema()
    out(schema.get("header", "═ Author a mission ═"))
    # Reuse the agent-forge elicitation engine (same FieldType registry + per-field
    # validation) over the MISSION schema — one input core for both.
    spec = _forge.elicit_spec(ask, schema=schema, out=out)
    spec = _mission.prune(spec)

    issues = _mission.validate(spec)
    if issues:
        out("✗ The mission is incomplete:")
        for i in issues:
            out(f"    - {i}")
        return None

    m = _mission.Mission(spec, agent=agent)
    out(_mission.render(m))
    seal = ask(schema.get("seal_prompt", "Seal this mission? (blank to cancel)"))
    if not seal:
        out("  Cancelled — not sealed.")
        return None

    path = _mission.save(spec, agent=agent)
    out(f"  ✔ Mission sealed → {path}")
    out(f"    run it with  gorgon mission run {_mission.slug(m.title)}")
    return path

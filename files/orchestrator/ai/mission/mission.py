"""
mission.py — a Mission: what you TASK an agent to do.

The split: a **contract** (.grgn) creates the AGENT — who it is and its default
parameters. A **mission** is a tasking that agent consumes. Only ``title`` and
``goal`` are required; every other field is optional and, when omitted, INHERITS
the agent's default (contract.default_*). So a mission carries just what makes it
different from the agent's baseline.

    contracts create agents · agents consume missions

A signed, long-form mission is authored in the mission wizard and persisted; a
quick one-off task is an *ephemeral* mission (``Mission.ephemeral(goal)``) that
sets only the goal and inherits everything else. Either way, the reward-cost engine
and the goal verifier read the RESOLVED values here, so they never need to know
whether a value came from the mission or the agent.
"""
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from ..agent import contract as _contract

_DIR = os.path.expanduser("~/.gorgon/missions")

# The mission fields, and which are required. Data-driven so the wizard, the
# validator, and this model agree on one list (mirrors the forge field schema).
REQUIRED_FIELDS = ("title", "goal")
OPTIONAL_FIELDS = ("sub_goals", "reward", "importance", "weight",
                   "tool_whitelist", "tool_blacklist", "scrutiny",
                   "success_predicate", "success_criteria", "reward_cost")


class Mission:
    """A tasking for the active agent. Unset fields resolve to the agent's defaults."""

    def __init__(self, spec: Dict[str, Any], agent: Optional[str] = None):
        self._s = dict(spec or {})
        # The owning agent — a mission is a product of an agent's existence, so it's
        # scoped to (and disabled with) that agent. Defaults to the active one.
        self.agent = agent or self._s.get("agent") or _contract.active_agent_key()

    # ── identity ──────────────────────────────────────────────────────────────
    @property
    def title(self) -> str:
        return self._s.get("title") or "(untitled mission)"

    @property
    def goal(self) -> str:
        return self._s.get("goal") or ""

    @property
    def sub_goals(self) -> List[str]:
        return list(self._s.get("sub_goals") or [])

    # ── resolved parameters (mission value → else agent default) ────────────────
    def reward(self) -> float:
        """R for closing this mission — importance SCALES it (an important mission is
        worth more), so a 2× importance on a reward-1 mission books reward 2."""
        base = self._s.get("reward")
        base = float(base) if base is not None else _contract.default_reward()
        return base * self.importance()

    def importance(self) -> float:
        v = self._s.get("importance")
        return float(v) if v is not None else _contract.default_importance()

    def weight(self) -> float:
        v = self._s.get("weight")
        return float(v) if v is not None else _contract.default_weight()

    def scrutiny(self):
        return self._s.get("scrutiny") if self._s.get("scrutiny") is not None \
            else _contract.default_scrutiny()

    def whitelist(self) -> List[str]:
        """Tools this mission may use — its own whitelist, else the agent's toolkit."""
        return list(self._s.get("tool_whitelist") or _contract.default_toolkit())

    def blacklist(self) -> List[str]:
        """Red lines for this mission: the agent's blacklist UNION the mission's own —
        a mission can add limits, never remove the agent's (the agent bounds every
        mission it runs)."""
        return sorted(set(_contract.default_blacklist()) | set(self._s.get("tool_blacklist") or []))

    # Reward-cost knobs a mission MAY tune — reward SHAPING and learning only. The
    # worth-it bar (theta) and risk-aversion (lambda) are deliberately EXCLUDED: those are
    # the agent's standing safety policy (set in the contract), not something a per-tasking
    # mission may relax. So a mission can dial partial-credit or holding cost, never its
    # own bar for acting.
    _TUNABLE_REWARD_COST = ("alpha", "H", "kappa", "p_world_k")

    def reward_cost_overrides(self) -> Dict[str, float]:
        """Per-tasking reward-cost tweaks that LAYER over the contract — restricted to
        reward-shaping / learning knobs (e.g. a deep-recon mission dialing `alpha` up so
        long plans bank more partial credit as sub-goals close). Safety knobs stay
        contract-level; unknown or forbidden keys are dropped."""
        rc = self._s.get("reward_cost") or {}
        return {k: float(v) for k, v in rc.items()
                if k in self._TUNABLE_REWARD_COST and isinstance(v, (int, float))
                and not isinstance(v, bool)}

    def predicate(self) -> Optional[list]:
        """The mission's structured acceptance clauses (the checkable 'done when'), or
        None — in which case acceptance falls to the Library (state) + findings
        grounding, no faked gate over prose."""
        return self._s.get("success_predicate") or None

    def filter_tools(self, tools: List[Dict]) -> List[Dict]:
        """Apply the mission's whitelist/blacklist to a tool list (OpenAI tool dicts):
        keep only whitelisted names (if a whitelist is set), then drop blacklisted."""
        wl, bl = set(self.whitelist()), set(self.blacklist())

        def _name(t):
            return (t.get("function") or {}).get("name") if "function" in t else t.get("name")
        out = tools
        if wl:
            out = [t for t in out if _name(t) in wl]
        if bl:
            out = [t for t in out if _name(t) not in bl]
        return out

    def to_spec(self) -> Dict[str, Any]:
        """The raw spec dict (for signing/persisting)."""
        d = dict(self._s)
        d["agent"] = self.agent
        return d

    # ── constructors ────────────────────────────────────────────────────────────
    @classmethod
    def ephemeral(cls, goal: str, title: Optional[str] = None,
                  agent: Optional[str] = None) -> "Mission":
        """A quick one-off task: only the goal is set, everything else inherits the
        agent's defaults. This is what `gorgon mission "<goal>"` runs (unsigned)."""
        return cls({"title": title or "(ad-hoc task)", "goal": goal}, agent=agent)


def validate(spec: Dict[str, Any]) -> List[str]:
    """Structural problems with a mission spec — the required fields, mainly. Returns
    a list of human-readable issues ([] when the spec is well-formed)."""
    issues: List[str] = []
    for f in REQUIRED_FIELDS:
        if not (spec or {}).get(f):
            issues.append(f"missing required field: {f}")
    return issues


def prune(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Drop blank/empty optional fields so they INHERIT the agent's default (an
    empty answer must mean 'inherit', not 'set to empty'). Required fields stay."""
    return {k: v for k, v in (spec or {}).items()
            if k in REQUIRED_FIELDS or v not in (None, "", [], {})}


# ── persistence: a signed .mission scoped to its agent ────────────────────────
# Missions are a product of an agent's existence, so they live UNDER the agent
# (~/.gorgon/missions/<agent>/<slug>.mission) and are disabled when it's voided.
# Encrypted like a .grgn, so goals/rewards never sit in cleartext.

def _safe(name: Optional[str]) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", name or "default") or "default"


def slug(title: str) -> str:
    return _safe((title or "mission").strip().lower().replace(" ", "-"))


def missions_dir(agent: Optional[str] = None) -> str:
    return os.path.join(_DIR, _safe(agent or _contract.active_agent_key()))


def mission_path(name: str, agent: Optional[str] = None) -> str:
    return os.path.join(missions_dir(agent), f"{_safe(name)}.mission")


def save(spec: Dict[str, Any], agent: Optional[str] = None) -> str:
    """Seal a mission: encrypt its spec to ~/.gorgon/missions/<agent>/<slug>.mission.
    Returns the path. Falls back to plaintext only if the crypto layer is absent."""
    agent = agent or _contract.active_agent_key()
    spec = dict(spec)
    spec["agent"] = agent
    os.makedirs(missions_dir(agent), exist_ok=True)
    path = mission_path(slug(spec.get("title", "mission")), agent)
    try:
        from shared.grgn_sign import write_encrypted
        return write_encrypted(spec, path)
    except Exception:
        import json
        with open(path, "w") as f:
            json.dump(spec, f, indent=2, ensure_ascii=False)
        return path


def load(name: str, agent: Optional[str] = None) -> Tuple[Optional["Mission"], str]:
    """Load a sealed mission by name → (Mission, status). status is the integrity
    verdict from grgn_sign (encrypted|signed|unsigned|tampered|missing); a tampered
    or missing mission returns (None, status) — fail-closed, like a bad .grgn."""
    agent = agent or _contract.active_agent_key()
    try:
        from ..agent import revocation as _revocation
        if _revocation.is_voided(agent):
            return None, "voided"          # the owning agent is disabled → so is this mission
    except Exception:
        pass
    path = mission_path(name, agent)
    if not os.path.isfile(path):
        return None, "missing"
    try:
        from shared.grgn_sign import read as _read
        spec, status = _read(path)
    except Exception:
        import json
        try:
            spec, status = json.load(open(path)), "unsigned"
        except Exception:
            return None, "tampered"
    if spec is None or status in ("tampered", "missing"):
        return None, status
    return Mission(spec, agent=agent), status


def delete(name: str, agent: Optional[str] = None) -> bool:
    """Delete a sealed mission by name. Returns False if it doesn't exist."""
    agent = agent or _contract.active_agent_key()
    path = mission_path(name, agent)
    if not os.path.isfile(path):
        return False
    os.remove(path)
    return True


def list_missions(agent: Optional[str] = None) -> List[Dict[str, Any]]:
    """The agent's sealed missions as [{name, title, goal, status}], sorted by name."""
    agent = agent or _contract.active_agent_key()
    try:
        from ..agent import revocation as _revocation
        if _revocation.is_voided(agent):
            return []                      # voided agent → its missions are disabled
    except Exception:
        pass
    d = missions_dir(agent)
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".mission"):
            continue
        name = fn[:-len(".mission")]
        m, status = load(name, agent)
        out.append({
            "name": name,
            "title": m.title if m else "(unreadable)",
            "goal": m.goal if m else "",
            "status": status,
        })
    return out


def render(m: "Mission") -> str:
    """A human-readable summary of a mission (for `gorgon mission show`)."""
    L = [f"  MISSION — {m.title}",
         f"    agent:   {m.agent}",
         f"    goal:    {m.goal}"]
    if m.sub_goals:
        L.append(f"    steps:   {', '.join(m.sub_goals)}")
    L.append(f"    reward:  {m.reward()}   (importance ×{m.importance()}, weight {m.weight()})")
    wl, bl = m.whitelist(), m.blacklist()
    L.append(f"    tools:   {', '.join(wl) if wl else '(agent toolkit)'}")
    if bl:
        L.append(f"    redline: {', '.join(bl)}")
    pred = m.predicate()
    if pred:
        clauses = ", ".join(f"{c.get('criterion')}:{c.get('target')}" for c in pred)
        L.append(f"    done-when: {clauses}")
    return "\n".join(L)

"""
contract.py — the engine that loads a .grgn agent and applies its contract.

A ``.grgn`` file is a COMPLETE, portable gorgon agent: persona + prompts (system/
innate) + contract (tiers, formula weights, per-tool risk, verbs, pins) — everything
that makes an agent THIS agent. Drag-and-drop one file to swap the Doorman for a
Conductor; "the only difference is role" is literal at the file level (same schema,
different contents). This module is the SUBSTRATE side: it loads the active agent
file and answers "how should this tool call be gated?" — it is not itself part of
any agent, and neither are the executor, the tools (``command_catalog.json``), or
the CLI, which every agent consumes by reference after the fact.

CONFIRMATION TIER — computed, never authored. Each tool the agent has assessed
carries risk facts (reversible? how destructive? blast radius? commitment); the
weighted formula turns those into a tier. A ``pin`` overrides the formula for a
judgment call. Tools the agent hasn't assessed resolve to ``none``. The tool
UNIVERSE and signatures come from ``command_catalog.TOOL_SPECS`` (the registry);
the contract only references tools by name, so the two can't duplicate each other.

The tier ladder, ascending friction::

    none        run it silently (read-only / trivial)
    acknowledge run it, but surface "heads-up, I did X" (catchable)
    normal      y/n confirm
    name        type the exact target name once (proof of intent)
    double      type YES, then the exact name (irreversible + destructive)
"""
import json
import os
from typing import Any, Dict, List, Optional

# The tool registry — the FACTS source of truth (what tools exist + signatures).
# Guarded like score.py's import so this module still loads in orchestrator-only
# checkouts without the executor package (tools resolve to none then).
try:
    from executor.command_catalog import TOOL_SPECS as _TOOL_SPECS, TOOL_NAME_ARG as _TOOL_NAME_ARG
except ImportError:                                                    # pragma: no cover
    _TOOL_SPECS: Dict[str, Any] = {}
    _TOOL_NAME_ARG: Dict[str, str] = {}

# The active agent file, resolved at import. agent_select owns the resolution
# order (GORGON_AGENT env var > persisted `gorgon agent` selection > doorman
# default), so this reader never re-encodes it.
try:
    from shared.agent_select import resolve as _resolve_agent
except Exception:
    _resolve_agent = lambda: "doorman.grgn"                                    # type: ignore[assignment]
_AGENT_FILE = _resolve_agent()
_AGENT_PATH = (_AGENT_FILE if os.path.isabs(_AGENT_FILE)
               else os.path.join(os.path.dirname(__file__), _AGENT_FILE))
_DOORMAN_PATH = os.path.join(os.path.dirname(__file__), "doorman.grgn")
if not os.path.isfile(_AGENT_PATH):
    # A stale selection (deleted file) must not brick startup — fall back safely.
    _AGENT_PATH = _DOORMAN_PATH

# A VOIDED agent is disabled (its contract was revoked): refuse it and run doorman
# instead. Because an agent's missions are only reachable while it's the active agent,
# voiding the agent disables every mission it owns — the void cascade.
try:
    from . import revocation as _revocation
    _VOIDED_SELECTION = _revocation.is_voided(os.path.splitext(os.path.basename(_AGENT_PATH))[0])
except Exception:
    _VOIDED_SELECTION = False
if _VOIDED_SELECTION and os.path.abspath(_AGENT_PATH) != os.path.abspath(_DOORMAN_PATH):
    _AGENT_PATH = _DOORMAN_PATH

# Integrity gate: a TAMPERED agent file (bad Fernet token or bad sidecar) is
# refused (fail-closed) and we fall back to doorman — a hand-edited/forged-under-a-
# foreign-key contract must not run. Forged files are encrypted; the built-in
# templates are plaintext (trust-on-first-use). Status is surfaced, not printed
# here, since contract.py is imported in many contexts.
try:
    from shared.grgn_sign import read as _read_grgn
except Exception:
    _read_grgn = lambda p: (json.load(open(p)), "unsigned")                   # type: ignore[assignment]
def _is_expired(contract: Dict[str, Any]) -> bool:
    """True if the contract carries an expiry date that is already past."""
    con = (contract or {}).get("contract", {}) or {}
    exp = con.get("expiry") or (con.get("campaign") or {}).get("expiry")   # agent-level, legacy fallback
    if not exp:
        return False
    try:
        from datetime import date
        return date.fromisoformat(str(exp)) < date.today()
    except Exception:
        return False                               # unparseable → don't brick startup


_C_LOADED, _AGENT_STATUS = _read_grgn(_AGENT_PATH)
_refuse = (_AGENT_STATUS in ("tampered", "missing") or _C_LOADED is None
           or _is_expired(_C_LOADED))
if _refuse and os.path.abspath(_AGENT_PATH) != os.path.abspath(_DOORMAN_PATH):
    _bad_status = "expired" if (_C_LOADED is not None and _is_expired(_C_LOADED)) else "tampered"
    _AGENT_PATH = _DOORMAN_PATH                    # refuse; run the default agent
    _C_LOADED, _ = _read_grgn(_AGENT_PATH)
    _AGENT_STATUS = _bad_status                    # remember why the selected file was refused
if _VOIDED_SELECTION:
    _AGENT_STATUS = "voided"                        # the selected agent was revoked → doorman runs
_C: Dict[str, Any] = _C_LOADED if _C_LOADED is not None else json.load(open(_DOORMAN_PATH))


def agent_signature_status() -> str:
    """Status of the SELECTED agent file: encrypted | signed | unsigned | tampered
    | expired | missing. 'tampered'/'expired' mean it was refused and doorman is
    running instead."""
    return _AGENT_STATUS


def active_agent_key() -> str:
    """The per-agent scope key (agent file basename, no extension) — used to scope
    the claim store so a doorman run's claims never mix with a barenboim run's
    (doorman.grgn -> 'doorman')."""
    return os.path.splitext(os.path.basename(_AGENT_PATH))[0]

PERSONA = _C.get("persona", {})
_PROMPTS = _C.get("prompts", {})
_CONTRACT = _C["contract"]

# Ordered least→most friction. Index = ordinal rank, used for monotonicity checks
# and for "take the stricter of two tiers" when layers combine later.
TIERS = _CONTRACT["tiers"]
_TIER_RANK = {t: i for i, t in enumerate(TIERS)}

_FORMULA = _CONTRACT["formula"]
_WEIGHTS = _FORMULA["weights"]
_BLAST_SCALE = _FORMULA["blast_scale"]
_THRESHOLDS = _FORMULA["thresholds"]        # {tier: min_risk} for the non-"none" tiers
_TOOLS = _CONTRACT["tools"]                 # per-tool contract data: {risk, verb, pin}
_FLEET_ACTIONS = _CONTRACT.get("fleet_actions", {})   # action -> tier (action-conditional)

# The fleet actions that require confirmation (tier != none). Derived from the
# single source so the CLI and HTTP paths can't drift — the property the fleet test
# guards.
FLEET_CONFIRM_ACTIONS = frozenset(_FLEET_ACTIONS)


def system_prompt_template() -> str:
    """The agent's system/innate prompt TEMPLATE (from the .grgn).

    Stored as a list of lines for readability; joined here. Contains the runtime
    tokens ``{custom_note} {ovmf_status} {profiles} {state_section}`` which the
    caller fills from the live substrate.
    """
    return "\n".join(_PROMPTS.get("system", []))


def tier_rank(tier: str) -> int:
    """Ordinal position of ``tier`` on the friction ladder (none=0 … double=N)."""
    return _TIER_RANK[tier]


def stricter(a: str, b: str) -> str:
    """Return whichever of two tiers demands MORE friction.

    The combine rule for stacking layers (innate + campaign): a higher layer may
    raise a tool's tier but the effective gate is the strictest applicable one.

    Example::

        stricter("normal", "double")  # -> "double"
    """
    return a if _TIER_RANK[a] >= _TIER_RANK[b] else b


def tool_risk(tool: str) -> Optional[Dict[str, Any]]:
    """The tool's risk facts as assessed by the active contract, or None.

    None = the contract hasn't assessed this tool (→ tier none). Risk is a
    contract JUDGMENT (lives in the .grgn), not a registry fact.
    """
    return (_TOOLS.get(tool) or {}).get("risk")


def _risk_score(risk: Dict[str, Any]) -> float:
    """Weighted risk score in [0, 1] from a tool's risk facts.

    Factors: destructiveness (damage if wrong), irreversibility (can't be undone),
    blast radius (how far the effect spreads), and commitment (resources/side
    effects it locks in even when reversible — why creating a VM warrants a y/n
    though it's undoable).

    Example::

        _risk_score({"reversible": False, "destructiveness": 1.0,
                     "blast": "entity", "commitment": 0.3})  # -> ~0.77
    """
    dest = float(risk.get("destructiveness", 0.0))
    irr = 0.0 if risk.get("reversible", True) else 1.0
    blast = float(_BLAST_SCALE.get(risk.get("blast", "none"), 0.0))
    commit = float(risk.get("commitment", 0.0))
    return (_WEIGHTS["destructiveness"] * dest
            + _WEIGHTS["irreversibility"] * irr
            + _WEIGHTS["blast"] * blast
            + _WEIGHTS["commitment"] * commit)


def _risk_to_tier(risk_val: float) -> str:
    """Map a risk score to a tier by walking thresholds high → low."""
    if risk_val >= _THRESHOLDS["double"]:
        return "double"
    if risk_val >= _THRESHOLDS["name"]:
        return "name"
    if risk_val >= _THRESHOLDS["normal"]:
        return "normal"
    if risk_val >= _THRESHOLDS["acknowledge"]:
        return "acknowledge"
    return "none"


def formula_tier(tool: str) -> Optional[str]:
    """The tier the FORMULA computes for a tool from its risk, ignoring any pin.

    "none" for a tool the contract assessed as risk-free / didn't assess; None for
    a tool absent from the registry. Comparing this to ``resolve_tier`` is the
    tweak worklist — where a pin overrides the formula.
    """
    if tool not in _TOOL_SPECS:
        return None
    risk = tool_risk(tool)
    return "none" if not risk else _risk_to_tier(_risk_score(risk))


def resolve_tier(tool: str, args: Optional[Dict[str, Any]] = None) -> str:
    """The LIVE confirmation tier for a proposed tool call — the gate's answer.

    Resolution order: ``fleet`` is action-conditional; then a ``pin`` wins if set;
    otherwise the tier is COMPUTED from the contract's risk facts. A tool absent
    from the registry defaults to ``none`` (matching "unknown tool is silent").

    Example::

        resolve_tier("delete_vm")                      # -> "double"  (formula)
        resolve_tier("snapshot_create")                # -> "none"    (unassessed)
        resolve_tier("fleet", {"action": "exec"})      # -> "normal"
        resolve_tier("fleet", {"action": "ping"})      # -> "none"
    """
    if tool == "fleet":
        action = ((args or {}).get("action") or "").strip().lower()
        return _FLEET_ACTIONS.get(action, "none")
    if tool not in _TOOL_SPECS:
        return "none"
    pin = (_TOOLS.get(tool) or {}).get("pin")
    if pin is not None:
        return pin
    risk = tool_risk(tool)
    return "none" if not risk else _risk_to_tier(_risk_score(risk))


# ── DISPOSITION: how a tier is HANDLED, per the agent's role ─────────────────────
# resolve_tier() gives the risk TIER (role-independent). The DISPOSITION then maps
# that tier to a handling ACTION. Same risk, different response: a supervised agent
# asks the human; an autonomous agent resolves it itself — log the low-risk ones,
# CHECKPOINT a destructive-but-authorized one so it stays revertible, and HALT +
# escalate at the top (corrigibility, not unilateral action). The human-confirm
# actions are implemented by the CLI/web gates; the autonomous actions (log/
# checkpoint/halt) by the Conductor harness.
DISPOSITION = PERSONA.get("disposition", "human-confirm")

_HANDLING = {
    "human-confirm": {"none": "proceed", "acknowledge": "notify",
                      "normal": "ask_yn", "name": "ask_name", "double": "ask_double"},
    "autonomous":    {"none": "proceed", "acknowledge": "log",
                      "normal": "log", "name": "checkpoint", "double": "halt"},
}
# Most-conservative fallback per disposition if a tier is ever unmapped.
_HANDLING_FALLBACK = {"human-confirm": "ask_double", "autonomous": "halt"}


def disposition() -> str:
    """The active agent's disposition (e.g. 'human-confirm' | 'autonomous')."""
    return DISPOSITION


def gate_action(tool: str, args: Optional[Dict[str, Any]] = None) -> str:
    """How a proposed call is HANDLED under the active agent's disposition.

    Two stages: resolve the risk tier, then map it through the disposition. The
    Doorman (human-confirm) yields proceed/notify/ask_yn/ask_name/ask_double; a
    Conductor (autonomous) yields proceed/log/checkpoint/halt — same tiers, no human.

    Example (delete_vm is tier 'double')::

        # doorman.grgn:    gate_action("delete_vm") -> "ask_double"
        # conductor.grgn:  gate_action("delete_vm") -> "halt"
    """
    tier = resolve_tier(tool, args)
    table = _HANDLING.get(DISPOSITION, _HANDLING["human-confirm"])
    return table.get(tier, _HANDLING_FALLBACK.get(DISPOSITION, "ask_double"))


def safeword() -> Optional[str]:
    """The active contract's safeword (the operator's kill-switch), or None if the
    agent isn't a signed campaign. The harness arms its KillSwitch with this."""
    return _CONTRACT.get("safeword") or (_CONTRACT.get("campaign") or {}).get("safeword")


def goal_predicate() -> Optional[list]:
    """The campaign's structured ROOT predicate — what the WHOLE goal must satisfy,
    as a list of ``{criterion, target}`` clauses (the checkable twin of the prose
    ``success_criteria``). This is "the root test comes from the CONTRACT": a plan is
    accepted only if every clause holds against ground truth, so a clean-but-WRONG
    decomposition books no reward.

    None when the agent has no campaign (the Doorman) or the campaign declares only
    free-text success_criteria — we won't fake a deterministic gate over prose (the
    design's honest boundary). Each clause reuses the leaf criteria vocabulary
    (present / absent / running / stopped / restored) over a named target.
    """
    return (_CONTRACT.get("campaign") or {}).get("success_predicate") or None


def _defaults() -> Dict[str, Any]:
    """The agent's DEFAULT mission parameters — what a mission inherits for any field
    it doesn't set. Sourced from an explicit ``defaults`` block, falling back to the
    legacy ``campaign`` values so existing .grgn files keep working during the split
    (contract=identity → mission=tasking)."""
    d = dict(_CONTRACT.get("defaults") or {})
    camp = _CONTRACT.get("campaign") or {}
    d.setdefault("reward", camp.get("reward", 1.0))
    d.setdefault("scrutiny", camp.get("scrutiny"))
    return d


def default_reward() -> float:
    """The agent's default payoff R for closing a goal — a mission's `reward` overrides
    it; missions that don't set one inherit this. 1.0 when unspecified."""
    return float(_defaults().get("reward", 1.0))


def default_importance() -> float:
    """The agent's default mission importance (a reward multiplier); 1.0 unspecified."""
    return float(_defaults().get("importance", 1.0))


def default_weight() -> float:
    """The agent's default mission weight (planning/scoring weight); 1.0 unspecified."""
    return float(_defaults().get("weight", 1.0))


def default_scrutiny():
    """The agent's default scrutiny level (a mission may raise/lower it)."""
    return _defaults().get("scrutiny")


def default_toolkit() -> list:
    """The agent's default tool WHITELIST — the toolkit it may use unless a mission
    narrows it. Empty means 'no explicit whitelist' (all registered tools allowed,
    subject to the blacklist)."""
    camp = _CONTRACT.get("campaign") or {}
    return list(_CONTRACT.get("toolkit") or camp.get("toolkit") or [])


def default_blacklist() -> list:
    """The agent's default tool BLACKLIST (red lines) — a mission may add to it but
    never remove from it (the agent's hard limits bound every mission)."""
    return list(_CONTRACT.get("forbidden") or [])


def campaign_reward() -> float:
    """Back-compat alias for :func:`default_reward` (the agent's default payoff R).
    Kept so callers written against the pre-split name keep working."""
    return default_reward()


def reward_cost_cfg() -> Dict[str, Any]:
    """The reward-cost constants (θ, λ, H, κ, weights…) declared by the active
    contract's formula block. Empty → the reward_cost engine DEFAULTS. Keeps ALL the
    tunable policy in the .grgn (the contract holds the policy)."""
    return dict(_FORMULA.get("reward_cost", {}))


def risk_breakdown(tool: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The weighted risk-score breakdown for a tool — each factor's raw value, its
    weight, and weighted contribution, plus the total score, the formula tier, the
    resolved tier (after pins), and the gate action. Pure/read-only — for the verbose
    debug panel (surfaces WHY a call got the scrutiny it did)."""
    risk = tool_risk(tool) or {}
    dest = float(risk.get("destructiveness", 0.0))
    irr = 0.0 if risk.get("reversible", True) else 1.0
    blast_name = risk.get("blast", "none")
    blast = float(_BLAST_SCALE.get(blast_name, 0.0))
    commit = float(risk.get("commitment", 0.0))
    factors = [("destructiveness", dest, _WEIGHTS["destructiveness"]),
               ("irreversibility", irr, _WEIGHTS["irreversibility"]),
               ("blast", blast, _WEIGHTS["blast"]),
               ("commitment", commit, _WEIGHTS["commitment"])]
    score = sum(v * w for _, v, w in factors)
    return {
        "tool": tool,
        "assessed": bool(tool_risk(tool)),
        "factors": [{"name": n, "value": round(v, 3), "weight": w,
                     "contribution": round(v * w, 3)} for n, v, w in factors],
        "blast_label": blast_name,
        "score": round(score, 3),
        "formula_tier": _risk_to_tier(score),
        "resolved_tier": resolve_tier(tool, args),
        "action": gate_action(tool, args),
    }


def is_forbidden(tool: str, args: Optional[Dict[str, Any]] = None) -> bool:
    """LEGAL FILTER (gauntlet step A): a hard, categorical red line — a weight-0
    rule the tree may NEVER cross. Forbidden calls are dropped up front, never
    costed, never surfaced for consent. Distinct from destructiveness (which is the
    priceable/consent axis): this is legality/authorization. Contract-declared via
    the .grgn ``forbidden`` list (tool names for now; target-scope patterns later).
    """
    return tool in set(_CONTRACT.get("forbidden", []))


def consent_verb(tool: str) -> str:
    """A human-readable consequence to SURFACE in a consent referendum (the design
    requires the referendum show the consequence, not just the action)."""
    meta = confirm_meta(tool)
    return meta[1] if meta else tool.replace("_", " ")


def success_criterion(tool: str) -> Optional[str]:
    """The contract's post-condition for a tool — what "done" means — or None.

    A declarative criterion the tree VERIFIES against ground truth (the Active
    Library) before marking a node done, instead of trusting the tool's own
    "success" return. E.g. create_vm -> "present" (the VM now exists), delete_vm ->
    "absent", launch_vm -> "running". The contract declares the criterion (what);
    the caller supplies the reality-check (how). None = trust the execute result.
    """
    return (_TOOLS.get(tool) or {}).get("verify")


def confirm_meta(tool: str):
    """(field, verb) for a confirmable tool, or None.

    ``field`` = which arg names the target of the confirm prompt; ``verb`` = the
    human-readable action ("delete VM"). The field is DERIVED from the registry
    (``TOOL_NAME_ARG`` override, else the tool's first required arg) so it stays in
    sync with the tool's real signature; the contract supplies the display verb,
    falling back to a humanized tool name. Returns a usable pair for any tool in
    the contract or the registry; only a tool in neither returns None.

    Example::

        confirm_meta("delete_vm")   # -> ("name", "delete VM")
        confirm_meta("no_such_xyz") # -> None
    """
    if tool not in _TOOLS and tool not in _TOOL_SPECS:
        return None
    attr = _TOOLS.get(tool) or {}
    field = attr.get("field") or _registry_target_field(tool)
    return field, attr.get("verb") or tool.replace("_", " ")


def _registry_target_field(tool: str) -> str:
    """Which arg names the tool's target, from the registry (default 'name')."""
    if tool in _TOOL_NAME_ARG:
        return _TOOL_NAME_ARG[tool]
    req = (_TOOL_SPECS.get(tool) or {}).get("req") or []
    return req[0] if req else "name"


def is_critical(tool: str, args: Optional[Dict[str, Any]] = None) -> bool:
    """True when the tool double-confirms (tier == 'double').

    The successor to the old ``critical_tools`` set — now derived from the tier,
    so it tracks the contract instead of a separate hand-maintained list.
    """
    return resolve_tier(tool, args) == "double"


def confirms_by_name(tool: str, args: Optional[Dict[str, Any]] = None) -> bool:
    """True when the tool requires typing the target name ('name' or 'double').

    The successor to ``tool in _CONFIRM_NAME`` — used by the pre-flight gate to
    skip its ask_user path for name-confirmed destructive ops.
    """
    return resolve_tier(tool, args) in ("name", "double")


def registry_tools() -> frozenset:
    """The canonical tool universe (``KNOWN_TOOLS``), or empty if unavailable."""
    return frozenset(_TOOL_SPECS)


def orphan_entries() -> set:
    """Contract tool entries that name a tool absent from the registry — drift.

    Empty is the healthy state; anything here means the .grgn assessed a tool that
    was renamed or removed from the registry under it.
    """
    if not _TOOL_SPECS:
        return set()
    return set(_TOOLS) - set(_TOOL_SPECS)


def agent_tool_issues(allowed_remote_tools: Optional[set] = None) -> List[str]:
    """Advisory warnings about the ACTIVE agent's tool references vs. the executor
    SSOT — surfaced at load time (e.g. after `gorgon agent load` restarts the
    server). Two kinds:

      - a referenced tool absent from the registry (a stale/renamed reference),
      - a whitelisted tool the executor won't run remotely (not in
        allowed_remote_tools).

    These are ADVISORY, not blocking: a forged .grgn deliberately carries its own
    (copied) tool baseline for portability, and the executor is the real gate —
    an unknown or non-allowed call is rejected there regardless. This just tells
    the operator the file has drifted from the registry. allowed_remote_tools
    None/empty ⇒ skip the second check (an empty allow-list means all permitted).
    """
    issues: List[str] = []
    known      = set(_TOOL_SPECS)
    camp       = _CONTRACT.get("campaign", {}) or {}
    toolkit    = set(_CONTRACT.get("toolkit") or camp.get("toolkit") or [])
    forbidden  = set(_CONTRACT.get("forbidden", []) or [])
    referenced = toolkit | forbidden | set(_TOOLS)
    if known:
        for t in sorted(referenced - known):
            issues.append(f"missing tool reference: '{t}' is not in the executor registry")
    allowed = set(allowed_remote_tools or [])
    if allowed:
        for t in sorted((toolkit & known) - allowed):
            issues.append(f"tool '{t}' forbidden by executor (not in allowed_remote_tools)")
    return issues


def pinned_disagreements() -> Dict[str, Dict[str, str]]:
    """Every pin that overrides the computed tier → {tool: {pin, formula}}.

    The reconciliation worklist: either the pin encodes a real judgment the
    formula should learn (adjust the tool's risk / the weights until they agree,
    then drop the pin), or the formula is surfacing a gap worth adopting.
    """
    out: Dict[str, Dict[str, str]] = {}
    for tool, attr in _TOOLS.items():
        pin = attr.get("pin")
        if pin is None:
            continue
        f = formula_tier(tool)
        if f is not None and f != pin:
            out[tool] = {"pin": pin, "formula": f}
    return out

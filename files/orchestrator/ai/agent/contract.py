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

The contract is modelled as objects: a :class:`Contract` holds the loaded .grgn and
composes a :class:`RiskFormula` (the weighted risk→tier scorer) and a
:class:`ToolPolicy` (the per-tool risk map + pins + fleet actions). Genuinely
cross-field logic (tier resolution, disposition/gate) lives on ``Contract``. A
module-level default instance ``ACTIVE`` is loaded once at import, and the historical
``contract.gate_action(...)`` module functions are thin shims over it, so callers
need not hold an instance.

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

from .fields import build_fields, ExpiryField
from shared.bundle import Bundle, resolve_grgn as _resolve_grgn

# The tool registry — the FACTS source of truth (what tools exist + signatures).
# Guarded like score.py's import so this module still loads in orchestrator-only
# checkouts without the executor package (tools resolve to none then).
try:
    from executor.command_catalog import TOOL_SPECS as _TOOL_SPECS, TOOL_NAME_ARG as _TOOL_NAME_ARG
except ImportError:                                                    # pragma: no cover
    _TOOL_SPECS: Dict[str, Any] = {}
    _TOOL_NAME_ARG: Dict[str, str] = {}

_HANDLING = {
    "human-confirm": {"none": "proceed", "acknowledge": "notify",
                      "normal": "ask_yn", "name": "ask_name", "double": "ask_double"},
    "autonomous":    {"none": "proceed", "acknowledge": "log",
                      "normal": "log", "name": "checkpoint", "double": "halt"},
}
# Most-conservative fallback per disposition if a tier is ever unmapped.
_HANDLING_FALLBACK = {"human-confirm": "ask_double", "autonomous": "halt"}


def _is_expired(grgn: Dict[str, Any]) -> bool:
    """True if the agent's contract carries an expiry date that is already past.
    Delegates to ExpiryField (the field owns the read + legacy fallback)."""
    return ExpiryField().is_expired((grgn or {}).get("contract", {}) or {})


def agent_grgn_path(agent_file: str, code_dir: str) -> str:
    """Resolve an agent selection to its .grgn path — bundle-first, code-dir fallback.
    Thin alias over the shared authority ``shared.bundle.resolve_grgn`` so the loader
    and the contract commands resolve identically."""
    return _resolve_grgn(agent_file, code_dir)


def _load_active() -> "tuple":
    """Resolve + integrity-gate the active .grgn, returning (grgn, agent_path, status).

    agent_select owns the resolution order (GORGON_AGENT env var > persisted
    `gorgon agent` selection > doorman default). A VOIDED, TAMPERED, MISSING, or
    EXPIRED selection is refused (fail-closed) and doorman runs instead; the reason
    is remembered in the status so it can be surfaced (not printed here, since this
    module loads in many contexts).
    """
    try:
        from shared.agent_select import resolve as _resolve_agent
    except Exception:
        _resolve_agent = lambda: "doorman.grgn"                                # type: ignore[assignment]
    agent_file = _resolve_agent()
    here       = os.path.dirname(__file__)
    doorman    = os.path.join(here, "doorman.grgn")
    agent_path = agent_grgn_path(agent_file, here)
    if not os.path.isfile(agent_path):
        # A stale selection (deleted file) must not brick startup — fall back safely.
        agent_path = doorman

    # A VOIDED agent is disabled (its contract was revoked): refuse it and run doorman
    # instead. Because an agent's missions are only reachable while it's the active
    # agent, voiding the agent disables every mission it owns — the void cascade.
    try:
        from . import revocation as _revocation
        voided = _revocation.is_voided(os.path.splitext(os.path.basename(agent_path))[0])
    except Exception:
        voided = False
    if voided and os.path.abspath(agent_path) != os.path.abspath(doorman):
        agent_path = doorman

    # Integrity gate: a TAMPERED agent file (bad Fernet token or bad sidecar) is
    # refused (fail-closed) and we fall back to doorman — a hand-edited/forged-under-a-
    # foreign-key contract must not run. Forged files are encrypted; the built-in
    # templates are plaintext (trust-on-first-use).
    try:
        from shared.grgn_sign import read as _read_grgn
    except Exception:
        _read_grgn = lambda p: (json.load(open(p)), "unsigned")               # type: ignore[assignment]
    loaded, status = _read_grgn(agent_path)
    refuse = (status in ("tampered", "missing") or loaded is None or _is_expired(loaded))
    if refuse and os.path.abspath(agent_path) != os.path.abspath(doorman):
        bad_status = "expired" if (loaded is not None and _is_expired(loaded)) else "tampered"
        agent_path = doorman                       # refuse; run the default agent
        loaded, _  = _read_grgn(agent_path)
        status     = bad_status                    # remember why the selected file was refused
    if voided:
        status = "voided"                          # the selected agent was revoked → doorman runs
    grgn = loaded if loaded is not None else json.load(open(doorman))
    return grgn, agent_path, status


class RiskFormula:
    """The weighted risk→tier scorer: turns a tool's risk facts into a [0,1] score
    and then a tier, using the contract's ``formula`` block (weights / blast_scale /
    thresholds). Pure cross-field scoring config — never authored, copied from the
    innate baseline — so it lives here, not as an authored Field.
    """

    def __init__(self, formula: Dict[str, Any], tiers: List[str]):
        self.weights     = formula["weights"]
        self.blast_scale = formula["blast_scale"]
        self.thresholds  = formula["thresholds"]        # {tier: min_risk} for non-"none" tiers
        self.reward_cost = dict(formula.get("reward_cost", {}))
        self._tiers      = tiers

    def score(self, risk: Dict[str, Any]) -> float:
        """Weighted risk score in [0, 1] from a tool's risk facts.

        Factors: destructiveness (damage if wrong), irreversibility (can't be undone),
        blast radius (how far the effect spreads), and commitment (resources/side
        effects it locks in even when reversible — why creating a VM warrants a y/n
        though it's undoable).
        """
        dest   = float(risk.get("destructiveness", 0.0))
        irr    = 0.0 if risk.get("reversible", True) else 1.0
        blast  = float(self.blast_scale.get(risk.get("blast", "none"), 0.0))
        commit = float(risk.get("commitment", 0.0))
        return (self.weights["destructiveness"] * dest
                + self.weights["irreversibility"] * irr
                + self.weights["blast"] * blast
                + self.weights["commitment"] * commit)

    def to_tier(self, risk_val: float) -> str:
        """Map a risk score to a tier by walking thresholds high → low."""
        if risk_val >= self.thresholds["double"]:
            return "double"
        if risk_val >= self.thresholds["name"]:
            return "name"
        if risk_val >= self.thresholds["normal"]:
            return "normal"
        if risk_val >= self.thresholds["acknowledge"]:
            return "acknowledge"
        return "none"

    def factors(self, risk: Dict[str, Any]) -> "tuple":
        """(factor rows, blast_label) for the debug breakdown — each factor's raw
        value + weight, so risk_breakdown can show its weighted contribution."""
        dest       = float(risk.get("destructiveness", 0.0))
        irr        = 0.0 if risk.get("reversible", True) else 1.0
        blast_name = risk.get("blast", "none")
        blast      = float(self.blast_scale.get(blast_name, 0.0))
        commit     = float(risk.get("commitment", 0.0))
        rows = [("destructiveness", dest, self.weights["destructiveness"]),
                ("irreversibility", irr, self.weights["irreversibility"]),
                ("blast", blast, self.weights["blast"]),
                ("commitment", commit, self.weights["commitment"])]
        return rows, blast_name


class ToolPolicy:
    """The per-tool contract data: the ``tools`` map ({risk, verb, verify, pin, field}),
    the fleet action→tier map, and the tier resolution over them (pin > formula), plus
    the registry-cross-checks (orphans, pinned disagreements). Composes a RiskFormula.
    """

    def __init__(self, tools: Dict[str, Any], fleet_actions: Dict[str, str],
                 formula: RiskFormula):
        self.tools         = tools
        self.fleet_actions = fleet_actions
        self.formula       = formula

    def tool_risk(self, tool: str) -> Optional[Dict[str, Any]]:
        """The tool's risk facts as assessed by the active contract, or None (→ tier
        none). Risk is a contract JUDGMENT (lives in the .grgn), not a registry fact."""
        return (self.tools.get(tool) or {}).get("risk")

    def formula_tier(self, tool: str) -> Optional[str]:
        """The tier the FORMULA computes for a tool from its risk, ignoring any pin.
        'none' for an assessed-risk-free / unassessed tool; None for a tool absent
        from the registry."""
        if tool not in _TOOL_SPECS:
            return None
        risk = self.tool_risk(tool)
        return "none" if not risk else self.formula.to_tier(self.formula.score(risk))

    def resolve_tier(self, tool: str, args: Optional[Dict[str, Any]] = None) -> str:
        """The LIVE confirmation tier for a proposed tool call — the gate's answer.

        Resolution order: ``fleet`` is action-conditional; then a ``pin`` wins if set;
        otherwise the tier is COMPUTED from the contract's risk facts. A tool absent
        from the registry defaults to ``none``.
        """
        if tool == "fleet":
            action = ((args or {}).get("action") or "").strip().lower()
            return self.fleet_actions.get(action, "none")
        if tool not in _TOOL_SPECS:
            return "none"
        pin = (self.tools.get(tool) or {}).get("pin")
        if pin is not None:
            return pin
        risk = self.tool_risk(tool)
        return "none" if not risk else self.formula.to_tier(self.formula.score(risk))

    def success_criterion(self, tool: str) -> Optional[str]:
        """The contract's post-condition for a tool — what "done" means — or None."""
        return (self.tools.get(tool) or {}).get("verify")

    def confirm_meta(self, tool: str):
        """(field, verb) for a confirmable tool, or None. ``field`` names the target
        arg (registry-derived so it tracks the tool signature); ``verb`` is the
        contract's display verb, falling back to a humanized tool name."""
        if tool not in self.tools and tool not in _TOOL_SPECS:
            return None
        attr  = self.tools.get(tool) or {}
        field = attr.get("field") or self._registry_target_field(tool)
        return field, attr.get("verb") or tool.replace("_", " ")

    def _registry_target_field(self, tool: str) -> str:
        """Which arg names the tool's target, from the registry (default 'name')."""
        if tool in _TOOL_NAME_ARG:
            return _TOOL_NAME_ARG[tool]
        req = (_TOOL_SPECS.get(tool) or {}).get("req") or []
        return req[0] if req else "name"

    def orphan_entries(self) -> set:
        """Contract tool entries that name a tool absent from the registry — drift."""
        if not _TOOL_SPECS:
            return set()
        return set(self.tools) - set(_TOOL_SPECS)

    def pinned_disagreements(self) -> Dict[str, Dict[str, str]]:
        """Every pin that overrides the computed tier → {tool: {pin, formula}}."""
        out: Dict[str, Dict[str, str]] = {}
        for tool, attr in self.tools.items():
            pin = attr.get("pin")
            if pin is None:
                continue
            f = self.formula_tier(tool)
            if f is not None and f != pin:
                out[tool] = {"pin": pin, "formula": f}
        return out


class Contract:
    """A loaded .grgn agent contract. Holds the persona/prompts/contract data and
    composes a :class:`RiskFormula` and :class:`ToolPolicy`; the genuinely cross-field
    logic (tier ladder, disposition/gate, safeword, defaults) lives here as methods.
    """

    def __init__(self, grgn: Dict[str, Any], agent_path: str, status: str):
        self.raw        = grgn
        self.agent_path = agent_path
        self.status     = status
        self.persona    = grgn.get("persona", {})
        self.prompts    = grgn.get("prompts", {})
        self.contract   = grgn["contract"]

        # Ordered least→most friction. Index = ordinal rank (monotonicity + stricter()).
        self.tiers      = self.contract["tiers"]
        self._tier_rank = {t: i for i, t in enumerate(self.tiers)}

        self.formula = RiskFormula(self.contract["formula"], self.tiers)
        self.tool_policy = ToolPolicy(
            self.contract["tools"], self.contract.get("fleet_actions", {}), self.formula)
        # The fleet actions that require confirmation (tier != none), from the single
        # source so the CLI and HTTP paths can't drift.
        self.fleet_confirm_actions = frozenset(self.contract.get("fleet_actions", {}))
        self.disposition_name = self.persona.get("disposition", "human-confirm")

        # The single-key, authorable fields (toolkit, red-lines, safeword, expiry,
        # mission defaults) as objects — each owns its read + legacy fallback.
        self.fields = build_fields()

    @classmethod
    def load(cls) -> "Contract":
        """Load the active agent (resolution + integrity gate) into a Contract."""
        grgn, path, status = _load_active()
        return cls(grgn, path, status)

    # ── identity / signature ─────────────────────────────────────────────────────
    def agent_signature_status(self) -> str:
        """Status of the SELECTED file: encrypted|signed|unsigned|tampered|expired|
        missing|voided. tampered/expired/voided mean it was refused, doorman runs."""
        return self.status

    def active_agent_key(self) -> str:
        """The per-agent scope key (file basename, no extension) — scopes the claim
        store so a doorman run's claims never mix with a barenboim run's."""
        return os.path.splitext(os.path.basename(self.agent_path))[0]

    # ── prompt ───────────────────────────────────────────────────────────────────
    def system_prompt_template(self) -> str:
        """The agent's system/innate prompt TEMPLATE (joined from the .grgn line list),
        with the runtime tokens {custom_note}{ovmf_status}{profiles}{state_section}."""
        return "\n".join(self.prompts.get("system", []))

    # ── tier ladder ──────────────────────────────────────────────────────────────
    def tier_rank(self, tier: str) -> int:
        """Ordinal position of ``tier`` on the friction ladder (none=0 … double=N)."""
        return self._tier_rank[tier]

    def stricter(self, a: str, b: str) -> str:
        """Whichever of two tiers demands MORE friction (the layer-combine rule)."""
        return a if self._tier_rank[a] >= self._tier_rank[b] else b

    # ── risk / tier (delegated to the sub-objects) ───────────────────────────────
    def tool_risk(self, tool: str) -> Optional[Dict[str, Any]]:
        return self.tool_policy.tool_risk(tool)

    def formula_tier(self, tool: str) -> Optional[str]:
        return self.tool_policy.formula_tier(tool)

    def resolve_tier(self, tool: str, args: Optional[Dict[str, Any]] = None) -> str:
        return self.tool_policy.resolve_tier(tool, args)

    def risk_breakdown(self, tool: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """The weighted risk-score breakdown for a tool — each factor's raw value,
        weight, and weighted contribution, plus the score, formula tier, resolved tier
        (after pins), and gate action. Pure/read-only — for the verbose debug panel."""
        risk = self.tool_risk(tool) or {}
        rows, blast_name = self.formula.factors(risk)
        score = sum(v * w for _, v, w in rows)
        return {
            "tool": tool,
            "assessed": bool(self.tool_risk(tool)),
            "factors": [{"name": n, "value": round(v, 3), "weight": w,
                         "contribution": round(v * w, 3)} for n, v, w in rows],
            "blast_label": blast_name,
            "score": round(score, 3),
            "formula_tier": self.formula.to_tier(score),
            "resolved_tier": self.resolve_tier(tool, args),
            "action": self.gate_action(tool, args),
        }

    # ── disposition / gate ───────────────────────────────────────────────────────
    def disposition(self) -> str:
        """The active agent's disposition (e.g. 'human-confirm' | 'autonomous')."""
        return self.disposition_name

    def gate_action(self, tool: str, args: Optional[Dict[str, Any]] = None) -> str:
        """How a proposed call is HANDLED: resolve the risk tier, then map it through
        the disposition. human-confirm → proceed/notify/ask_yn/ask_name/ask_double;
        autonomous → proceed/log/checkpoint/halt (same tiers, no human)."""
        tier  = self.resolve_tier(tool, args)
        table = _HANDLING.get(self.disposition_name, _HANDLING["human-confirm"])
        return table.get(tier, _HANDLING_FALLBACK.get(self.disposition_name, "ask_double"))

    # ── safeword / goal / mission-defaults ───────────────────────────────────────
    def safeword(self) -> Optional[str]:
        """The active contract's safeword (kill-switch), or None. The harness arms
        its KillSwitch with this."""
        return self.fields["safeword"].read(self.contract)

    def goal_predicate(self) -> Optional[list]:
        """The campaign's structured ROOT predicate — the checkable twin of the prose
        success_criteria, as {criterion, target} clauses. None for an agent with no
        campaign or only free-text criteria (we won't fake a gate over prose)."""
        return (self.contract.get("campaign") or {}).get("success_predicate") or None

    def _defaults(self) -> Dict[str, Any]:
        """The agent's DEFAULT mission parameters (a mission inherits any it doesn't
        set), from an explicit ``defaults`` block with the legacy ``campaign`` fallback.
        Kept as the assembled-dict view; the per-field reads go through the Fields."""
        d = dict(self.contract.get("defaults") or {})
        camp = self.contract.get("campaign") or {}
        d.setdefault("reward", camp.get("reward", 1.0))
        d.setdefault("scrutiny", camp.get("scrutiny"))
        return d

    def default_reward(self) -> float:
        """The agent's default payoff R for closing a goal (a mission's `reward`
        overrides it). 1.0 when unspecified."""
        return self.fields["reward"].read(self.contract)

    def default_importance(self) -> float:
        """The agent's default mission importance (a reward multiplier); 1.0 unspecified."""
        return self.fields["importance"].read(self.contract)

    def default_weight(self) -> float:
        """The agent's default mission weight (planning/scoring weight); 1.0 unspecified."""
        return self.fields["weight"].read(self.contract)

    def default_scrutiny(self):
        """The agent's default scrutiny level (a mission may raise/lower it)."""
        return self.fields["scrutiny"].read(self.contract)

    def campaign_reward(self) -> float:
        """Back-compat alias for :meth:`default_reward` (the agent's default payoff R)."""
        return self.default_reward()

    def reward_cost_cfg(self) -> Dict[str, Any]:
        """The reward-cost constants (θ, λ, H, κ, weights…) from the formula block;
        empty → the reward_cost engine DEFAULTS. Keeps ALL tunable policy in the .grgn."""
        return dict(self.formula.reward_cost)

    # ── toolkit / red-lines / verbs / criteria ───────────────────────────────────
    def default_toolkit(self) -> list:
        """The agent's default tool WHITELIST (a mission may narrow it). Empty means
        'no explicit whitelist' (all registered tools allowed, subject to blacklist)."""
        return self.fields["toolkit"].read(self.contract)

    def default_blacklist(self) -> list:
        """The agent's default tool BLACKLIST (red lines) — a mission may add to it but
        never remove from it."""
        return self.fields["forbidden"].read(self.contract)

    def is_forbidden(self, tool: str, args: Optional[Dict[str, Any]] = None) -> bool:
        """LEGAL FILTER (gauntlet step A): a hard, categorical red line the tree may
        NEVER cross — dropped up front, never costed. Contract-declared via the .grgn
        ``forbidden`` list."""
        return self.fields["forbidden"].contains(self.contract, tool)

    def consent_verb(self, tool: str) -> str:
        """A human-readable consequence to SURFACE in a consent referendum."""
        meta = self.confirm_meta(tool)
        return meta[1] if meta else tool.replace("_", " ")

    def success_criterion(self, tool: str) -> Optional[str]:
        """The contract's post-condition for a tool — what "done" means — or None."""
        return self.tool_policy.success_criterion(tool)

    def confirm_meta(self, tool: str):
        """(field, verb) for a confirmable tool, or None."""
        return self.tool_policy.confirm_meta(tool)

    def is_critical(self, tool: str, args: Optional[Dict[str, Any]] = None) -> bool:
        """True when the tool double-confirms (tier == 'double')."""
        return self.resolve_tier(tool, args) == "double"

    def confirms_by_name(self, tool: str, args: Optional[Dict[str, Any]] = None) -> bool:
        """True when the tool requires typing the target name ('name' or 'double')."""
        return self.resolve_tier(tool, args) in ("name", "double")

    def registry_tools(self) -> frozenset:
        """The canonical tool universe (``KNOWN_TOOLS``), or empty if unavailable."""
        return frozenset(_TOOL_SPECS)

    def orphan_entries(self) -> set:
        """Contract tool entries naming a tool absent from the registry — drift."""
        return self.tool_policy.orphan_entries()

    def agent_tool_issues(self, allowed_remote_tools: Optional[set] = None) -> List[str]:
        """Advisory warnings about the ACTIVE agent's tool references vs. the executor
        SSOT: a referenced tool absent from the registry, or a whitelisted tool the
        executor won't run remotely. Advisory, not blocking (the executor is the real
        gate). allowed_remote_tools None/empty ⇒ skip the second check."""
        issues: List[str] = []
        known      = set(_TOOL_SPECS)
        toolkit    = set(self.fields["toolkit"].read(self.contract))
        forbidden  = set(self.fields["forbidden"].read(self.contract))
        referenced = toolkit | forbidden | set(self.tool_policy.tools)
        if known:
            for t in sorted(referenced - known):
                issues.append(f"missing tool reference: '{t}' is not in the executor registry")
        allowed = set(allowed_remote_tools or [])
        if allowed:
            for t in sorted((toolkit & known) - allowed):
                issues.append(f"tool '{t}' forbidden by executor (not in allowed_remote_tools)")
        return issues

    def pinned_disagreements(self) -> Dict[str, Dict[str, str]]:
        """Every pin that overrides the computed tier → {tool: {pin, formula}}."""
        return self.tool_policy.pinned_disagreements()


# ── the module-level default instance + back-compat shims ────────────────────────
# ACTIVE is the one contract loaded at import (as this module always was a singleton
# wrapper around one .grgn). The functions below delegate to it so every historical
# `contract.foo()` call site keeps working; new code can also construct a Contract.
ACTIVE = Contract.load()

# Module-level constants preserved for callers that read them as attributes.
PERSONA               = ACTIVE.persona
_PROMPTS              = ACTIVE.prompts
_CONTRACT             = ACTIVE.contract
TIERS                 = ACTIVE.tiers
_TIER_RANK            = ACTIVE._tier_rank
_FORMULA              = ACTIVE.contract["formula"]
_WEIGHTS              = ACTIVE.formula.weights
_BLAST_SCALE          = ACTIVE.formula.blast_scale
_THRESHOLDS           = ACTIVE.formula.thresholds
_TOOLS                = ACTIVE.tool_policy.tools
_FLEET_ACTIONS        = ACTIVE.tool_policy.fleet_actions
FLEET_CONFIRM_ACTIONS = ACTIVE.fleet_confirm_actions
DISPOSITION           = ACTIVE.disposition_name
_AGENT_PATH           = ACTIVE.agent_path
_AGENT_STATUS         = ACTIVE.status
_C                    = ACTIVE.raw


def _risk_score(risk: Dict[str, Any]) -> float:
    """Weighted risk score in [0, 1] from a tool's risk facts (see RiskFormula.score)."""
    return ACTIVE.formula.score(risk)


def _risk_to_tier(risk_val: float) -> str:
    """Map a risk score to a tier (see RiskFormula.to_tier)."""
    return ACTIVE.formula.to_tier(risk_val)


def agent_signature_status() -> str: return ACTIVE.agent_signature_status()
def active_agent_key() -> str: return ACTIVE.active_agent_key()
def system_prompt_template() -> str: return ACTIVE.system_prompt_template()
def tier_rank(tier: str) -> int: return ACTIVE.tier_rank(tier)
def stricter(a: str, b: str) -> str: return ACTIVE.stricter(a, b)
def tool_risk(tool: str) -> Optional[Dict[str, Any]]: return ACTIVE.tool_risk(tool)
def formula_tier(tool: str) -> Optional[str]: return ACTIVE.formula_tier(tool)


def resolve_tier(tool: str, args: Optional[Dict[str, Any]] = None) -> str:
    return ACTIVE.resolve_tier(tool, args)


def risk_breakdown(tool: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return ACTIVE.risk_breakdown(tool, args)


def disposition() -> str: return ACTIVE.disposition()


def gate_action(tool: str, args: Optional[Dict[str, Any]] = None) -> str:
    return ACTIVE.gate_action(tool, args)


def safeword() -> Optional[str]: return ACTIVE.safeword()
def goal_predicate() -> Optional[list]: return ACTIVE.goal_predicate()
def _defaults() -> Dict[str, Any]: return ACTIVE._defaults()
def default_reward() -> float: return ACTIVE.default_reward()
def default_importance() -> float: return ACTIVE.default_importance()
def default_weight() -> float: return ACTIVE.default_weight()
def default_scrutiny(): return ACTIVE.default_scrutiny()
def default_toolkit() -> list: return ACTIVE.default_toolkit()
def default_blacklist() -> list: return ACTIVE.default_blacklist()
def campaign_reward() -> float: return ACTIVE.campaign_reward()
def reward_cost_cfg() -> Dict[str, Any]: return ACTIVE.reward_cost_cfg()


def is_forbidden(tool: str, args: Optional[Dict[str, Any]] = None) -> bool:
    return ACTIVE.is_forbidden(tool, args)


def consent_verb(tool: str) -> str: return ACTIVE.consent_verb(tool)
def success_criterion(tool: str) -> Optional[str]: return ACTIVE.success_criterion(tool)
def confirm_meta(tool: str): return ACTIVE.confirm_meta(tool)


def _registry_target_field(tool: str) -> str:
    return ACTIVE.tool_policy._registry_target_field(tool)


def is_critical(tool: str, args: Optional[Dict[str, Any]] = None) -> bool:
    return ACTIVE.is_critical(tool, args)


def confirms_by_name(tool: str, args: Optional[Dict[str, Any]] = None) -> bool:
    return ACTIVE.confirms_by_name(tool, args)


def registry_tools() -> frozenset: return ACTIVE.registry_tools()
def orphan_entries() -> set: return ACTIVE.orphan_entries()


def agent_tool_issues(allowed_remote_tools: Optional[set] = None) -> List[str]:
    return ACTIVE.agent_tool_issues(allowed_remote_tools)


def pinned_disagreements() -> Dict[str, Dict[str, str]]:
    return ACTIVE.pinned_disagreements()

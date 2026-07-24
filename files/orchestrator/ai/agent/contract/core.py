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

from ..fields import build_fields
from .registry import _TOOL_SPECS, _TOOL_NAME_ARG
from .risk_formula import RiskFormula
from .tool_policy import ToolPolicy
from .loader import _is_expired, agent_grgn_path, _load_active

_HANDLING = {
    "human-confirm": {"none": "proceed", "acknowledge": "notify",
                      "normal": "ask_yn", "name": "ask_name", "double": "ask_double"},
    "autonomous":    {"none": "proceed", "acknowledge": "log",
                      "normal": "log", "name": "checkpoint", "double": "halt"},
}
# Most-conservative fallback per disposition if a tier is ever unmapped.
_HANDLING_FALLBACK = {"human-confirm": "ask_double", "autonomous": "halt"}


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

    def deadman_timeout(self) -> Optional[float]:
        """The UNATTENDED dead-man's timeout (seconds): the longest the run may go without
        a sign of life before it auto-aborts. None (default) = off — the safeword is the
        attended stop; this is the unattended backstop. Read from campaign.deadman; the
        harness arms a DeadMansSwitch with it. A non-positive / unparseable value = off."""
        v = (self.contract.get("campaign") or {}).get("deadman")
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f > 0 else None

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
def deadman_timeout() -> Optional[float]: return ACTIVE.deadman_timeout()
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

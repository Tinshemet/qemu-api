"""
assemble.py — turn a negotiated spec into a .grgn, and review/sign/render/write it.

forge() assembles the .grgn (inheriting the innate risk baseline from doorman and
adding the negotiated policy + toolkit/red-lines); review() is the coherence gate;
sign() locks a coherent contract + sets its safeword; render() is the human view;
write_grgn() persists it (encrypted).
"""

import json
import os
from typing import Any, Dict, List

from .schema import _reward_render

# The code-resident agent dir (agent/), one level up — doorman.grgn lives there.
_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _base_innate() -> Dict[str, Any]:
    """The shared innate layer (tiers, formula, per-tool risk baseline) from doorman.grgn."""
    return json.load(open(os.path.join(_AGENT_DIR, "doorman.grgn")))["contract"]


def _build_prompt(spec: Dict[str, Any]) -> List[str]:
    p = spec.get("persona", {})
    caveats = spec.get("caveats") or []
    # The agent prompt describes WHO the agent is — not a goal. A goal arrives with
    # each MISSION (contracts create agents · agents consume missions), so the prompt
    # sets character and limits, and the mission supplies the objective at run time.
    lines = [
        f"You are {p.get('name', 'an agent')} — {p.get('role', 'an autonomous agent')}, "
        f"a signed gorgon agent. You carry out MISSIONS you are given, each within this contract.",
        f"SCRUTINY: {spec.get('scrutiny', 'strict')} — "
        + {"strict": "use only mission-related tools; do not explore beyond the mission.",
           "medium": "you may explore beyond the mission for coverage.",
           "loose": "you are free to act as needed in service of the mission."}.get(spec.get("scrutiny", "strict"), ""),
        f"ETHICS: {spec.get('ethics', '(unspecified)')}.  LEGALITY: {spec.get('legality', '(unspecified)')}.",
    ]
    if caveats:
        lines.append("CAVEATS: " + "; ".join(caveats) + ".")
    lines += ["Carry out each mission within your contract; never cross a red line.", "{state_section}"]
    return lines


def forge(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble a .grgn agent from a negotiated contract spec. NOT yet signed."""
    base = _base_innate()
    tools_spec = spec.get("tools", {}) or {}
    mode = tools_spec.get("mode", "whitelist")
    listed = tools_spec.get("list", [])

    # Whitelisted tools inherit their innate risk assessment; others aren't offered.
    tools = {t: base["tools"][t] for t in listed if t in base.get("tools", {})}
    forbidden = list(spec.get("forbidden", []))
    if mode == "blacklist":
        forbidden = sorted(set(forbidden) | set(listed))
        tools = {}   # blacklist mode: everything but the blacklist is allowed (no per-tool whitelist)

    # A forged .grgn is a pure AGENT — identity + default parameters, no tasking.
    # Goals/titles/acceptance live on MISSIONS the agent consumes, not here.
    return {
        "_type": "gorgon.agent/v1",
        "_forged": True,
        "_doc": f"Forged gorgon agent: {spec.get('persona', {}).get('name', '(unnamed)')}.",
        "persona": {
            "name": spec.get("persona", {}).get("name"),
            "role": spec.get("persona", {}).get("role", "autonomous agent"),
            "disposition": spec.get("persona", {}).get("disposition", "autonomous"),
            "layers": ["innate"],
        },
        "prompts": {"system": _build_prompt(spec)},
        "contract": {
            "tiers": base["tiers"],
            "formula": base["formula"],
            "fleet_actions": base.get("fleet_actions", {}),
            "tools": tools,
            "tool_mode": mode,
            "toolkit": listed if mode == "whitelist" else None,   # default whitelist (a mission may narrow it)
            "forbidden": forbidden,                                # default blacklist (red lines; a mission may add to it)
            "ethics": spec.get("ethics"),                         # abide [rules] | surface to user | disregard
            "legality": spec.get("legality"),                     # follow [laws] | none
            "caveats": spec.get("caveats", []),
            "rules": spec.get("rules", []),                       # [{text, weight}] — 0=wildcard/void-if-broken, 1=default
            # Agent DEFAULTS — a mission inherits these for any field it doesn't set.
            "defaults": {
                "reward": spec.get("reward", 1.0),                # default payoff a mission inherits
                "scrutiny": spec.get("scrutiny", "strict"),       # strict | medium | loose
            },
            "expiry": spec.get("expiry"),         # ISO date | None (never); enforced at load — the agent's credential
            "safeword": None,                     # set at signing (the agent's kill-switch)
            "signed": False,                      # two-phase: negotiable until signed
        },
    }


def review(grgn: Dict[str, Any]) -> List[str]:
    """Coherence check (the AI-review / arithmetic-ruling step): surface every problem
    that would make the contract un-signable. Empty list = coherent."""
    try:
        from executor.command_catalog import KNOWN_TOOLS
    except ImportError:
        KNOWN_TOOLS = frozenset()
    issues: List[str] = []
    c = grgn.get("contract", {})
    offered, forbidden = set(c.get("tools", {})), set(c.get("forbidden", []))
    toolkit = set(c.get("toolkit") or [])

    for t in offered | forbidden | toolkit:
        if KNOWN_TOOLS and t not in KNOWN_TOOLS:
            issues.append(f"references a tool that doesn't exist: {t!r}")
    both = (offered | toolkit) & forbidden
    if both:
        issues.append(f"tool is BOTH offered and forbidden (contradiction): {sorted(both)}")
    if not grgn.get("persona", {}).get("name"):
        issues.append("no persona name")
    if c.get("tool_mode") == "whitelist" and not c.get("toolkit"):
        issues.append("empty toolkit — the agent can do nothing")
    # Weighted-rule coherence: a signed rule set must not silently contradict itself
    # (a rule at two weights, a duplicate, an out-of-range weight). resolve() gives the
    # deterministic precedence order; conflicts() are un-signable.
    from ..contract.rules import conflicts as _rule_conflicts
    issues += _rule_conflicts(c.get("rules"))
    return issues


def sign(grgn: Dict[str, Any], safeword: str) -> Dict[str, Any]:
    """The signing ceremony: lock a COHERENT contract and set its safeword. Refuses an
    incoherent contract (the conscience gate at entry) or a missing safeword."""
    issues = review(grgn)
    if issues:
        raise ValueError("cannot sign an incoherent contract: " + "; ".join(issues))
    if not safeword:
        raise ValueError("a safeword is required to sign (the kill-switch)")
    grgn["contract"]["safeword"] = safeword
    grgn["contract"]["signed"] = True
    return grgn


def _safeword_sig(word: Any) -> str:
    """A hash of a (normalized) safeword — so the amendment log proves WHICH safeword was
    in force without storing it in cleartext, and re-auth compares in constant time."""
    import hashlib
    norm = str(word or "").strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest() if norm else ""


def amend(grgn: Dict[str, Any], changes: Dict[str, Any], safeword: str, *,
          prior_safeword: str, at: Any = None) -> Dict[str, Any]:
    """Amend a SIGNED contract — the controlled re-sign lifecycle (NOT a raw overwrite, NOT
    a full re-forge). This is the safe re-open the old sign()-locks-forever design lacked:

      1. CONSENT / re-auth — the operator must present the CURRENT safeword (you can't amend
         what you can't stop). A wrong word is refused. This is the amendment's referendum.
      2. Apply the change delta (a shallow merge of contract-level fields — rules, toolkit,
         forbidden, expiry, …).
      3. RE-REVIEW — the amended contract must still be coherent (rules included, via E1),
         or the amendment is refused before it can be signed.
      4. Re-sign under `safeword` (may be the same), BUMP the version, and append a
         tamper-evident amendment-log entry (the prior safeword's HASH + what changed) — so
         a watchdog alert or a denied consent re-opens a human amendment that re-signs,
         instead of forcing a full re-forge.

    Refuses an unsigned contract (forge→sign a new one), a wrong prior safeword, or a change
    that makes the contract incoherent."""
    import copy
    import hmac
    c = grgn.get("contract", {})
    if not c.get("signed"):
        raise ValueError("amend applies to a SIGNED contract; forge→sign a new one instead")
    if not prior_safeword or not hmac.compare_digest(_safeword_sig(prior_safeword), _safeword_sig(c.get("safeword"))):
        raise ValueError("the current safeword is required to amend (operator re-auth)")
    if not safeword:
        raise ValueError("a safeword is required to re-sign the amendment")
    # Build the amendment on a CANDIDATE so a rejected change never leaves the live
    # contract half-mutated — grgn is replaced only once the result is coherent.
    candidate = copy.deepcopy(c)
    for k, v in (changes or {}).items():           # apply the delta over the contract fields
        candidate[k] = v
    issues = review({**grgn, "contract": candidate})   # coherence gate BEFORE the new signature
    if issues:
        raise ValueError("cannot amend into an incoherent contract: " + "; ".join(issues))
    prior_version = int(c.get("version", 1))
    entry: Dict[str, Any] = {"version": prior_version + 1,
                             "prior_safeword_sha": _safeword_sig(prior_safeword),
                             "changed": sorted((changes or {}).keys())}
    if at is not None:
        entry["at"] = at
    candidate["amendments"] = list(c.get("amendments", [])) + [entry]
    candidate["version"]  = prior_version + 1
    candidate["safeword"] = safeword
    candidate["signed"]   = True
    grgn["contract"] = candidate                   # commit atomically
    return grgn


def render(grgn: Dict[str, Any], width: int = 68) -> str:
    """A human-readable terminal view of an AGENT contract — identity + default
    parameters (persona · scrutiny/ethics/legality · toolkit · red lines · weighted
    rules · defaults · safeword). No goals — those live on missions. For `gorgon
    contract show`."""
    p = grgn.get("persona", {})
    c = grgn.get("contract", {})
    d = c.get("defaults", {})
    bar = "─" * width

    def wrap(label, text):
        return f"  {label:<12} {text}"

    L = ["",                                      # leading blank line so the box starts fresh
         "╔" + "═" * width + "╗",
         f"  AGENT CONTRACT — {p.get('name') or '(unnamed)'}"
         + ("     ✔ SIGNED" if c.get("signed") else "     … unsigned · negotiable"),
         "╠" + "═" * width + "╣",
         wrap("Identity", f"{p.get('name','?')}  ·  {p.get('role','')}  ·  {p.get('disposition','?')}"),
         bar,
         wrap("Scrutiny", d.get("scrutiny", "")),
         wrap("Ethics", c.get("ethics", "")),
         wrap("Legality", c.get("legality", "")),
         bar,
         wrap("Toolkit", ", ".join(c.get("toolkit") or sorted(c.get("tools", {})) or ["(none)"])
              + (f"   [{c.get('tool_mode')}]" if c.get("tool_mode") else "")),
         wrap("Red lines", (", ".join(c.get("forbidden", [])) or "(none)") + "   [weight 0 · inviolable]"),
         bar,
         wrap("Def. reward", _reward_render(d.get("reward", ""))),
         wrap("Expires", c.get("expiry") or "never")]

    rules = c.get("rules", [])
    if rules or c.get("caveats"):
        from ..contract.rules import resolve as _resolve_rules
        L.append(bar)
        L.append("  RULES  (weight: 0 = wildcard/void-if-broken · 1 = default · higher = weaker)")
        # Render in resolved PRECEDENCE order (strongest first), not declaration order —
        # so the reviewer reads the rules the way they actually rank.
        for r in _resolve_rules(rules):
            tag = " ⚑ inviolable" if r["inviolable"] else ""
            L.append(f"    [{r['weight']:g}] {r['text']}{tag}")
        for cav in c.get("caveats", []):
            L.append(f"    [·] {cav}")

    L += [bar,
          wrap("Safeword", "set  (kill-switch armed)" if c.get("safeword") else "— (set at signing)"),
          "  A mission gives this agent a goal:  gorgon mission new",
          "╚" + "═" * width + "╝"]
    return "\n".join(L)


def write_grgn(grgn: Dict[str, Any], path: str) -> str:
    """Persist a forged .grgn — ENCRYPTED (Fernet), so the safeword and campaign
    never sit in cleartext on disk. Point GORGON_AGENT at it to run the agent.
    Falls back to plaintext only if the crypto layer is unavailable."""
    try:
        from shared.grgn_sign import write_encrypted
        return write_encrypted(grgn, path)
    except Exception:
        with open(path, "w") as f:                 # degraded: crypto layer absent
            json.dump(grgn, f, indent=2, ensure_ascii=False)
        return path

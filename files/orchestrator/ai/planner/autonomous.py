"""
autonomous.py — the autonomous execution loop: run an agent to a goal, no human.

This is the driver that actually RUNS the Score tree for an autonomous agent (a
Conductor). Everything the tree needs was built already — this wires it together and
turns it loose: the model proposes, the tree decomposes to primitives, the CONTRACT
gates each leaf (halt a red line / checkpoint a destructive one), a leaf is DONE only
if VERIFIED against reality, a soft-failed branch BACKTRACKS with a different approach,
and a checkpointed dead branch ROLLS BACK first. No human backstop — the agent's
disposition (from its .grgn) drives handling.

Dependency-injected like run_score, so the whole loop is testable with stubs:
  run_autonomous(goal, call_model=…, execute=…, tools=…, vms_getter=…)
and a live convenience that wires the real Ollama + executor + Active Library:
  run_autonomous_live(goal)

The Library-backed `verify` here is what ACTIVATES verified-completion: it evaluates
the contract's per-tool success criterion (present / absent / running / stopped /
restored) against the live VM registry, catching a tool that reports success but didn't
actually change the world.
"""
import re
from typing import Any, Callable, Dict, List, Optional

from .score import run_score, _first_tool_call, _NODE_SYSTEM, DECOMPOSE_TOOL
from ..agent import contract as _contract
from .method_cache import seeded as _seeded_cache
from .findings import Findings, DEFAULT_SCHEMA
from .reward_cost import (economics as _economics, p_self_estimate as _p_self, dials as _dials,
                          cfg_with as _cfg_with, leaf_cost as _leaf_cost, ce as _ce,
                          tool_counts as _tool_counts, merge_counts as _merge_counts,
                          p_world_estimate as _p_world_estimate, p_world_lookup as _p_world_lookup,
                          compound_ce as _compound_ce, economics_tree as _economics_tree,
                          should_commit as _should_commit)
from .watchdog import Watchdog
from .engine import Engine
from .killswitch import KillSwitch, DeadMansSwitch


def _is_running(rec: Optional[Dict[str, Any]]) -> bool:
    return bool(rec) and "run" in str(rec.get("status", "")).lower()


def _criterion_holds(criterion: str, name: Optional[str], vms: Dict[str, Dict[str, Any]]) -> bool:
    """Does a success criterion hold for `name` against the live VM registry?

    The shared vocabulary (present / absent / running / stopped / restored) used by
    BOTH the per-leaf verifier (verified-completion) and the goal verifier (the
    contract root predicate). Unknown criteria pass — never block on the uncheckable.
    """
    if criterion == "present":  return name in vms
    if criterion == "absent":   return name not in vms
    if criterion == "running":  return _is_running(vms.get(name))
    if criterion == "stopped":  return name in vms and not _is_running(vms.get(name))
    if criterion == "restored": return name in vms
    return True


def make_library_verifier(vms_getter: Callable[[], Dict[str, Dict[str, Any]]]):
    """A verify(criterion, tool, args, result) that checks the contract's success
    criterion against the live VM registry (`vms_getter() -> {name: {status,…}}`).

    Unknown criteria pass (don't block on something we can't check). This is the
    "how" that pairs with the contract's "what" (contract.success_criterion).
    """
    def verify(criterion: str, tool: str, args: Dict[str, Any], result: Any) -> bool:
        name = args.get("name") or args.get("new_name") or args.get("net_name")
        return _criterion_holds(criterion, name, vms_getter() or {})
    return verify


def make_probe(execute: Callable[[str, Dict], Any]):
    """A probe(spec) -> Optional[bool] that verifies a `probe:` predicate clause with a real
    read-only probe. spec is "scope:assertion:target[:value]":
      • a VM name in the scope slot (e.g. "web01:port_listening:443") → guest_probe (in-VM);
      • the sentinel "local" or "host" (e.g. "local:file_exists:out.csv") → local_probe, which
        verifies run_command's effects in the host workspace.
    Returns the assertion's truth, or None when it can't be verified (malformed spec, or the
    probe itself failed) — the caller treats None as "unverifiable", never as "done"."""
    def probe(spec: str) -> Optional[bool]:
        parts = (spec or "").split(":", 3)            # scope:assertion:target[:value]
        if len(parts) < 3 or not all(parts[:3]):
            return None
        scope, assertion, target = parts[0], parts[1], parts[2]
        value = parts[3] if len(parts) == 4 and parts[3] else None
        if scope in ("local", "host"):
            tool, pargs = "local_probe", {"assertion": assertion, "target": target}
        else:
            tool, pargs = "guest_probe", {"name": scope, "assertion": assertion, "target": target}
        if value is not None:                         # file_contains/matches/user_in_group operand
            pargs["value"] = value
        res = execute(tool, pargs)
        if isinstance(res, dict) and res.get("success"):
            return bool(res.get("holds"))
        return None                                   # channel/probe failure → unverifiable
    return probe


# An ASSURANCE goal asserts a checkable end-state the plan must actually establish —
# not merely a set of steps to run. Detecting that intent lets the ephemeral (no-
# predicate) path apply a goal-level honesty rule instead of closing on structure alone.
_ASSURANCE_RE = re.compile(
    r"\b(make sure|ensure|verify|confirm|guarantee|prove|check that|validate|"
    r"ping each other|ping one another|reach each other|reach one another|"
    r"can (?:all |each )?(?:ping|reach)|all (?:can )?(?:ping|reach|connect)|"
    r"connectivity|all connected|mutually reachable)\b", re.I)
_CONNECTIVITY_RE = re.compile(r"\b(ping|reach|connect|connectivity|mesh)\b", re.I)


def _has_assurance_intent(goal: str) -> bool:
    return bool(_ASSURANCE_RE.search(goal or ""))


def make_goal_verifier(vms_getter: Callable[[], Dict[str, Dict[str, Any]]], findings=None, probe=None,
                       predicate=None):
    """A verify_goal(goal, children, ledger) — the CONTRACT ROOT PREDICATE.

    Checks the active contract's structured goal predicate (contract.goal_predicate(),
    a list of {criterion, target} clauses). Two kinds of clause:
      • STATE clauses (present/absent/running/stopped/restored) → checked against the
        live VM registry (what IS).
      • EPISTEMIC clauses (`mesh` → the fact mesh(target); `reachable` → reachable(target))
        → checked against the FINDINGS ledger (what was LEARNED). This is how a
        connectivity goal ("all ping each other") is accepted: the ping's recorded
        result must be truthy — NOT the tool merely returning success.

    The root goal is accepted only if EVERY clause holds — so a plan that ran cleanly
    but did not actually achieve the goal (a broken mesh) books no reward. Returns None
    when the contract declares no structured predicate (no clauses, no gate).
    """
    def _finding_true(fact: str) -> bool:
        # `usable` excludes a PENDING claim — an unverified fact can't close a goal
        # until a human confirms it (see findings.usable / gorgon claim confirm).
        return findings is not None and findings.usable(fact)

    def verify_goal(goal: str, children: list, ledger: list) -> Optional[bool]:
        # Acceptance clauses come from the MISSION (what you tasked) when one is given;
        # otherwise fall back to the contract's legacy goal_predicate (pre-split). No
        # clauses → None, so acceptance falls to the Library (state) + findings grounding.
        clauses = predicate if predicate is not None else _contract.goal_predicate()
        if not clauses:
            # GOAL-LEVEL HONESTY RULE (the composite twin of the leaf `unverifiable`
            # rule): a plain goal falls to structural acceptance — EXCEPT an ASSURANCE
            # goal ("make sure they all ping each other", "ensure/verify X") that asserts
            # a checkable end-state. Such a goal must be affirmatively GROUNDED in the
            # findings ledger, or a plan that merely RAN closes `unverified`, never `done`
            # (false success is the worst failure mode for a corrigible agent). No
            # assurance intent → None, so ordinary goals keep structural acceptance.
            if findings is None or not _has_assurance_intent(goal):
                return None
            facts = list(findings.facts())
            if _CONNECTIVITY_RE.search(goal or ""):
                # A connectivity assurance needs at least one USABLE mesh/reachable
                # finding — a recorded-but-false mesh (the "plan ran, mesh is broken"
                # case) does NOT count, so the goal can't falsely close on it.
                conn = [f for f in facts if f.startswith("mesh(") or f.startswith("reachable(")]
                return any(_finding_true(f) for f in conn)
            # Generic assurance → at least one usable (probe-grounded or human-vouched)
            # finding; a plan that learned nothing verifiable can't claim assurance.
            return any(_finding_true(f) for f in facts)
        vms = vms_getter() or {}
        for c in clauses:
            crit, target = c.get("criterion"), c.get("target")
            if crit == "mesh":
                if not _finding_true(f"mesh({target})"):
                    return False
            elif crit == "reachable":
                if not _finding_true(f"reachable({target})"):
                    return False
            elif crit == "found":
                # Generic epistemic acceptance: the target IS the fact key
                # (e.g. found:ip(web01)) — accept only if the ledger learned it.
                # Generalizes mesh/reachable to any registered yield-schema fact.
                if not _finding_true(target):
                    return False
            elif crit == "probe":
                # Grounded: verify the assertion with an actual read-only probe.
                # Unverifiable (no probe fn, or the probe failed) → NOT done.
                if probe is None or probe(target) is not True:
                    return False
            elif not _criterion_holds(crit, target, vms):
                return False
        return True
    return verify_goal


def make_ce_estimator(call_model, tools, cost_of, cfg=None, reward=None, p_of=None, compound_p=None):
    """A per-alternative CE estimator for OR ordering/pruning (gauntlet C).

    For an alternative sub-goal, PEEK at which primitive the model would use (a model
    call with NO execution) and price THAT tool deterministically: CE = μ − (λ/2)σ²
    with μ = p·R − cost, cost = leaf_cost(cost_of(tool)). The model proposes the tool;
    the HARNESS prices the value — no p_self self-rating (the design's firewall). Reward
    is the goal-closing payoff, common to all alternatives, so ranking prefers the cheaper
    / more-reliable route to the SAME goal.

    A COMPOUND alternative (the model would DECOMPOSE) is priced by its α-credited backed-
    up CE from the peeked step count — so a deep-but-reliable route competes on merit
    instead of being fizzled to ~0 (this is how superadditive α steers LIVE planning, not
    just the post-run economics). Only when α > 0: at α = 0 a compound route stays unpriced
    (kept, never pruned) — the original act-observe-correct default. A nested-OR
    (`alternatives`) peek is still unpriced (too deep to cost cheaply here).
    """
    c = _cfg_with(cfg)
    R = c.get("R", 1.0) if reward is None else reward

    def estimate(alt_goal: str, depth: int) -> Optional[float]:
        msgs = [{"role": "system", "content": _NODE_SYSTEM},
                {"role": "user", "content": f"Goal: {alt_goal}"}]
        name, args = _first_tool_call(call_model(msgs, list(tools) + [DECOMPOSE_TOOL]))  # PEEK, no execute
        if not name or name == "alternatives":
            return None                                   # no-op / nested OR → don't price, don't prune
        if name == "decompose":
            n_steps = len([s for s in (args.get("steps") or []) if s])
            if c["alpha"] <= 0 or n_steps <= 0:
                return None                               # α off → keep the old unpriced default
            # price the deep route with per-step partial credit. Unknown sub-tools → the
            # LEARNED-AVERAGE p_world (`compound_p`, the mean of this env's observed tool
            # reliability) when available, else the static default; plus a nominal per-step cost.
            # compound_p may be a live callable (recomputed per read) or a plain float.
            _cp = compound_p() if callable(compound_p) else compound_p
            return _compound_ce(n_steps, c, reward=R, p=_cp, cost=_leaf_cost(None, c))
        cost = _leaf_cost(cost_of(name), c)
        p = p_of(name) if p_of else c["p_world"]
        mu = p * R - cost
        var = p * (1 - p) * R * R
        return _ce(mu, var, c)
    return estimate


def _reason_target(args: Dict[str, Any]) -> Optional[str]:
    """The entity an action operates on (for the reason-vs-action check)."""
    for k in ("name", "vm_name", "new_name", "net_name", "target", "vm"):
        v = (args or {}).get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _response_text(resp: Any) -> str:
    """The free-text content of a model response (no tool call)."""
    msg = (resp or {}).get("message", {}) if isinstance(resp, dict) else {}
    c = msg.get("content")
    return c.strip() if isinstance(c, str) else ""


# Present-state vocabulary → canonical status. Used to catch a reason that asserts a
# FALSE current state to justify an action (grounded reason-check, mitigation A).
_STATUS_CANON = {
    "running": "running", "up": "running", "on": "running", "active": "running",
    "live": "running", "started": "running", "online": "running",
    "stopped": "stopped", "down": "stopped", "off": "stopped", "inactive": "stopped",
    "halted": "stopped", "offline": "stopped", "shut down": "stopped", "not running": "stopped",
}
_STATUS_ALT = "|".join(sorted((re.escape(w) for w in _STATUS_CANON), key=len, reverse=True))


def _canon_status(status: Any) -> Optional[str]:
    s = str(status or "").lower()
    if "run" in s:  return "running"
    if "stop" in s or "shut" in s or "off" in s: return "stopped"
    return None


def _asserted_status(reason: str, target: str) -> Optional[str]:
    """A PRESENT-state claim the reason makes about the NAMED target — "<target> is
    [currently|already|still] <status>". Requires the target name + a present "is", and is
    blind to DESIRED outcomes ("to make it running", "so it's up"): those don't match
    "<target> is …", so a legit launch/stop is never mis-flagged. Returns the canonical
    asserted status, or None."""
    m = re.search(rf"\b{re.escape(target)}\b\s+(?:is|was|'s)\s+(?:currently\s+|already\s+|still\s+)?({_STATUS_ALT})\b",
                  reason, re.I)
    return _STATUS_CANON.get(m.group(1).lower()) if m else None


def make_reason_gate(call_model, state_getter=None):
    """A reason_gate(goal, tool, args) -> {"reason": str, "problem": tag|None} (the D1 gate).

    STAGE 1 — validate the reason: elicit the model's ONE-sentence justification for THIS
    action (a free-text call, no tools). An action it can't justify fails ("no_reason").
    STAGE 2a — action vs stated intent: the entity the action TARGETS must appear in that
    reason ("target_absent" — acting on X while the reason only mentions Y).
    STAGE 2b — reason vs REALITY (mitigation A, the deception-resistant half): if the reason
    asserts a present STATE for the target that CONTRADICTS the live registry (e.g. "web is
    running" when the Library shows it stopped), flag "reason_contradicts_state". A liar
    can't satisfy this without the world actually matching — the fact-check the pure
    consistency check couldn't do. Needs `state_getter()` → {name: {status}}; without it,
    2b is skipped (structural-only, as before).

    Deliberately STRUCTURAL, never a model-judge (the p_self firewall: a weak model grading
    its own reasoning is a second bad draw). Conservative — only clear divergence /
    contradiction is flagged. The elicited reason is RETURNED (mitigation B) so the harness
    can record it on the ledger + surface it in the live tree, making even a passed lie
    auditable after."""
    import json as _json

    def gate(goal: str, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            payload = _json.dumps(args, sort_keys=True)
        except Exception:
            payload = str(args)
        msgs = [{"role": "system", "content":
                 "State in ONE short sentence WHY this action serves the goal, naming the "
                 "specific thing you are acting on. Reply with the sentence only — no tool call."},
                {"role": "user", "content": f"Goal: {goal}\nAbout to call: {tool}({payload})"}]
        reason = _response_text(call_model(msgs, []))
        if not reason:
            return {"reason": "", "problem": "no_reason"}
        target = _reason_target(args)
        if target and target.lower() not in reason.lower():
            return {"reason": reason, "problem": "target_absent"}
        if target and state_getter is not None:                 # STAGE 2b — ground vs reality
            rec = (state_getter() or {}).get(target)
            if rec:
                asserted = _asserted_status(reason, target)
                actual = _canon_status(rec.get("status"))
                if asserted and actual and asserted != actual:
                    return {"reason": reason, "problem": "reason_contradicts_state"}
        return {"reason": reason, "problem": None}
    return gate


# COLLECTIVE (distributive) decomposition — Track 1.1. The benchmark's cliff: the weak
# model nails atomic steps and dependency chains but CANNOT expand "do X to all/them/each"
# over a set into per-member steps (0/3 at N=3). So the HARNESS does the loop: a collective
# sub-goal is expanded deterministically against the LIVE entity set into one atomic
# sub-goal per member — playing to the model's strength (each atomic step) and covering its
# weakness (the loop). No model call, no variance.
_COLLECTIVE_RE = re.compile(
    r"\b(them all|all of them|all the \w+|all \w+|each of them|each \w+|every \w+|them|each)\b", re.I)
# Inherently-collective operations are NOT distributive — "ping each other" / mesh is one
# fact over the whole set, not a per-member step. Never expand these (they're the assurance
# clause the goal-honesty rule + mesh acceptance already handle).
_INHERENT_COLLECTIVE_RE = re.compile(r"\b(each other|one another|ping all|connectivity|mesh|reachable)\b", re.I)


def make_collective_expander(entities_getter: Callable[[], Dict[str, Any]]):
    """An expand_collective(goal, path) -> [per-member sub-goal] | None. Fires when a
    sub-goal applies a DISTRIBUTIVE operation to a collective of live entities ("put them
    all on the network"); resolves the collective to the current entity set and substitutes
    each member in, yielding one atomic sub-goal per member. Skips inherently-collective ops
    (mesh/ping-each-other) and no-ops when there are <2 entities or no collective phrase."""
    def expand(goal: str, path: List[str]) -> Optional[List[str]]:
        g = goal or ""
        if _INHERENT_COLLECTIVE_RE.search(g):
            return None
        m = _COLLECTIVE_RE.search(g)
        if not m:
            return None
        members = list((entities_getter() or {}).keys())
        if len(members) < 2:
            return None
        # replace the collective phrase with each member name → per-member atomic sub-goals
        return [(g[:m.start()] + e + g[m.end():]).strip() for e in members]
    return expand


# REFERENCE GROUNDING (Track 1.2). The weak model often decomposes "create a vm named a
# and put it on the network" into ["create a vm named a", "add VM to the network"] — the
# second step DROPS which vm ("a"). An un-grounded step targets the wrong/no entity and
# fails. When the parent goal names exactly ONE entity, bind a bare reference ("the vm",
# "it") in a child step back to that entity — so "add vm to the network" → "add a to the
# network". Deterministic; only fires on an unambiguous single-entity parent.
_NAMED_ENTITY_RE = re.compile(r"\b(?:named|called)\s+([a-z][\w-]*)", re.I)
_BARE_REF_RE = re.compile(r"\b(?:the\s+|this\s+|that\s+)?(?:vm|virtual machine|machine|instance|node|box|it)\b", re.I)


def make_step_grounder():
    """A ground_steps(parent_goal, steps) -> steps that binds bare entity references in
    decomposed steps to the parent's single named entity (Track 1.2). No-op unless the
    parent names EXACTLY ONE entity (so binding is unambiguous) and a step both omits that
    name and carries a bare reference."""
    def ground(parent_goal: str, steps: List[str]) -> List[str]:
        ents = list(dict.fromkeys(_NAMED_ENTITY_RE.findall(parent_goal or "")))
        if len(ents) != 1:
            return steps
        e = ents[0]
        present = re.compile(rf"\b{re.escape(e)}\b", re.I)     # word-boundary (a 1-char name isn't a substring hit)
        out = []
        for s in steps:
            if present.search(s or "") or not _BARE_REF_RE.search(s or ""):
                out.append(s)                       # already grounded, or nothing to bind
            else:
                out.append(_BARE_REF_RE.sub(e, s, count=1))
        return out
    return ground


# DEPENDENCY COMPLETION (Track 1.4). The benchmark's real blocker: the weak model plans
# "put a/b/c on the lab network" but NEVER creates `lab` — it assumes the shared
# prerequisite exists, so every attach fails and it can't recover from "no such network".
# The harness completes the plan: if a decomposition REFERENCES a network that no step
# CREATES, prepend its creation. Deterministic; the model's plausible-but-incomplete plan
# is made whole. (Networks are the first prerequisite; the rule set is extensible.)
_NET_NAMED_RE = re.compile(r"\bnetwork\s+(?:called|named)\s+([a-z][\w-]*)", re.I)   # "network called lab"
_NET_ADJ_RE   = re.compile(r"\b([a-z][\w-]*)\s+network\b", re.I)                   # "lab network"
_NET_CREATE_RE = re.compile(r"\b(?:create|make|provision|set\s*up|add)\s+(?:a\s+|an\s+|the\s+)?(?:new\s+|isolated\s+|private\s+)*network\b", re.I)
_NET_ARTICLES = {"a", "an", "the", "new", "isolated", "private", "this", "that", "same", "one", "virtual"}


def _network_names(text: str):
    """The network name(s) a step names — 'network called lab' or 'lab network' → {'lab'}.
    Two ordered passes so 'a network called lab' yields 'lab', not the article 'a'."""
    t = text or ""
    out = {m.group(1).lower() for m in _NET_NAMED_RE.finditer(t)}
    for m in _NET_ADJ_RE.finditer(t):
        nm = m.group(1).lower()
        if nm not in _NET_ARTICLES:
            out.add(nm)
    return out


def make_prereq_completer(networks_getter: Optional[Callable[[], Any]] = None):
    """A complete_steps(parent_goal, steps) -> steps that PREPENDS a creation step for any
    network the plan REFERENCES but never CREATES (and that isn't already in state, if a
    networks_getter is given) — the dropped-prerequisite the weak model can't recover from.
    A step 'create a vm named a and put it on lab network' references `lab` but doesn't
    create it; with no 'create ... network' step for `lab`, prepend 'create a network
    called lab'. No-op when every referenced network is created or already exists."""
    def complete(parent_goal: str, steps: List[str]) -> List[str]:
        referenced, created = set(), set()
        for s in steps:
            names = _network_names(s)
            if _NET_CREATE_RE.search(s or ""):
                created |= names                     # this step creates a network
            else:
                referenced |= names
        existing = {str(n).lower() for n in (networks_getter() or [])} if networks_getter else set()
        missing = referenced - created - existing
        if not missing:
            return steps
        return [f"create a network called {m}" for m in sorted(missing)] + steps
    return complete


def make_tool_selector(cap: int = 14):
    """Per-node tool NARROWING for the weak model (the autonomous twin of the chat path's
    round-0 narrowing). Offered all ~50 tools at once, llama3.1 degrades to emitting text
    instead of tool-calls; narrowed to a node's sub-goal it tool-calls correctly. This
    closes that gap for run_autonomous.

    Narrows at COMMAND granularity: scan the node goal for trigger hints (the SAME matcher
    the context assistant uses — scan_tool_hints) then expand each hinted tool to its whole
    command's toolset, so a per-tool tag gap can't strand a sibling (hint 'network' → the
    network command's create/delete/list/attach, not just one). Adds a small always-on
    read-only kit so the model can always ground against state. A vague goal that hints
    NOTHING (already surfaced upstream) falls back to a default lab kit — never the full
    50 (which is what muted the model). Width is capped below the degradation cliff, with
    the hinted tools kept first so the cap never drops them. Meta-tools (decompose/
    alternatives) are appended by the engine, not here."""
    from orchestrator.ai.chat.context_assistant import scan_tool_hints
    from executor.command_catalog import COMMAND_CATALOG
    siblings: Dict[str, set] = {}                    # tool → every tool sharing a command with it
    for e in COMMAND_CATALOG:
        ts = set(e.get("tools") or [])
        for t in ts:
            siblings.setdefault(t, set()).update(ts)
    ALWAYS   = ["list_vms", "list_networks", "list_labels", "vm_status"]   # grounding, always offered
    DEFAULT  = ["create_vm", "launch_vm", "stop_vm", "create_network",
                "add_vm_to_network", "add_label", "fleet"]                 # vague-goal fallback kit

    def select(goal: str, tools: List[Dict]) -> List[Dict]:
        by_name = {t["function"]["name"]: t for t in tools}
        core: set = set()
        for h in scan_tool_hints(goal or ""):
            core |= siblings.get(h, {h})
        ordered: List[str] = []
        def _add(names):
            for n in names:
                if n in by_name and n not in ordered:
                    ordered.append(n)
        _add(sorted(core))                          # hinted commands' tools FIRST (cap-safe)
        if not core:
            _add(DEFAULT)                            # vague → the lab kit, not all 50
        _add(ALWAYS)                                 # read-only grounding, last
        return [by_name[n] for n in ordered[:cap]]
    return select


def render_mission_plan(steps: List[str]) -> str:
    """The mission's declared sub_goals as an intended ROOT decomposition. Injected into
    planning context so the plan tree forms along these steps — which is what makes them
    reward-bearing: reward-cost's α books each CLOSED step its share of the mission reward
    (vs. the model decomposing however it likes and α crediting emergent branches)."""
    lines = "\n  ".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
    return ("MISSION PLAN — decompose the goal along these steps; each step you CLOSE "
            f"earns its share of the reward:\n  {lines}")


def render_state(vms: Dict[str, Dict[str, Any]]) -> str:
    """Compact current-state grounding from the VM registry — so the model plans
    against reality (won't act on VMs that don't exist) and, on a retry, SEES why the
    last approach failed. The live loop grounds against LIBRARY.ai_digest the same way.
    """
    if not vms:
        return "CURRENT STATE: no VMs exist yet — do not act on VMs that don't exist."
    items = ", ".join(f"{n}({r.get('status', '?')})" for n, r in sorted(vms.items()))
    return ("CURRENT STATE (resolve references against this; never act on a VM not "
            f"listed here):\n  known VMs: {items}")


def _summarize(result: Dict[str, Any]) -> Dict[str, Any]:
    """Walk the tree for the headline counts an operator wants after a run."""
    halted = unverified = rolled = forbidden = aborted = 0

    def walk(n: Dict[str, Any]) -> None:
        nonlocal halted, unverified, rolled, forbidden, aborted
        if n.get("status") == "blocked" and n.get("reason") in ("contract_halt", "consent_denied"):
            halted += 1
        if n.get("status") == "forbidden":
            forbidden += 1
        if n.get("status") == "aborted":
            aborted += 1
        if n.get("status") == "unverified":
            unverified += 1
        rolled += int(n.get("rolled_back", 0))
        for c in n.get("children", []):
            walk(c)

    walk(result["root"])
    return {"status": result["root"].get("status"), "ok": result.get("ok"),
            "executed": len(result.get("ledger", [])),
            "halted": halted, "forbidden": forbidden, "aborted": aborted,
            "unverified": unverified, "rolled_back": rolled}


def run_autonomous(
    goal: str,
    *,
    call_model:  Callable[[List[Dict], List[Dict]], Dict],
    execute:     Callable[[str, Dict], Any],
    tools:       List[Dict],
    vms_getter:  Optional[Callable[[], Dict[str, Dict[str, Any]]]] = None,
    gate:        Optional[Callable[[str, Dict], str]] = None,
    build_context: Optional[Callable[[str, List[str]], str]] = None,
    select_tools:  Optional[Callable[[str, List[Dict]], List[Dict]]] = None,
    on_event:    Optional[Callable[[Dict[str, Any]], None]] = None,
    on_node:     Optional[Callable[[Dict[str, Any]], None]] = None,
    decompose_first: bool = True,
    method_cache=None,
    findings=None,
    findings_schema=None,
    reward=None,
    referendum=None,
    watchdog=None,
    killswitch=None,
    prior=None,
    max_retries: int = 2,
    max_revisions: int = 1,
    max_depth:   int = 3,
    max_steps:   int = 60,
    validate_reasons: bool = False,
    persist_claims: bool = False,
    agent_key: Optional[str] = None,
    mission=None,
    verbose: bool = False,
) -> Dict[str, Any]:
    """Run `goal` autonomously with the active agent's contract. No human in the loop.

    Wires run_score with the contract's gate + success criteria (defaults), a Library-
    backed verifier (when `vms_getter` is given → verified-completion is live), and NO
    confirm backstop. Returns run_score's {root, ledger, ok} plus `events` (one per
    executed tool call), `disposition`, and a `summary` (executed / halted / unverified
    / rolled_back). `gate` defaults to the active agent's contract.gate_action, so an
    autonomous .grgn halts red lines and checkpoints destructive leaves for real.

    Also returns the reward-cost outputs: `economics` (μ/σ²/CE/cost priced with the
    LEARNED per-tool p_world), `reliability` (the p_self dials to feed the next run as
    `prior=`), and `tool_counts` / `p_world` (the accumulated tallies and learned world-
    success rates). `prior=` feeds a previous run's reliability + tool_counts forward;
    tool_counts also persist durably in the findings store when `persist_claims`.
    """
    events: List[Dict[str, Any]] = []

    # A MISSION narrows the agent to this tasking: restrict the toolkit to the
    # mission's whitelist minus its (agent∪mission) blacklist before the model ever
    # sees them. The agent's own red lines still apply as a hard backstop in the gate.
    if mission is not None:
        tools = mission.filter_tools(tools)

    def _exec(tool: str, args: Dict[str, Any]) -> Any:
        r = execute(tool, args)
        ev = {"tool": tool, "args": args,
              "ok": not (isinstance(r, dict) and (r.get("success") is False or r.get("error")))}
        events.append(ev)
        if on_event:
            on_event(ev)
        return r

    verify = make_library_verifier(vms_getter) if vms_getter else None
    # Ground planning in current state (the Active Library's job): inject the live VM
    # registry into every planning call so the model won't plan/retry on VMs that
    # don't exist. The live loop uses LIBRARY.ai_digest the same way.
    if findings is None:
        findings = Findings()
    if findings_schema is None:
        findings_schema = DEFAULT_SCHEMA
    # Seed the ledger from the per-agent claim store: confirmed claims come back as
    # USABLE facts (a human already vouched for them) and prior pending claims stay
    # pending (so they aren't re-surfaced as brand-new). Best-effort — a bad/missing
    # store must never brick a run.
    if persist_claims:
        try:
            from ..agent.contract import active_agent_key as _agent_key
            from . import findings_store as _store
            agent_key = agent_key or _agent_key()
            findings.merge(_store.load(agent_key))
        except Exception:
            pass
    # Built AFTER findings exists: the root predicate reads epistemic clauses (mesh /
    # reachable) from the findings ledger, not just VM state.
    verify_goal = make_goal_verifier(
        vms_getter, findings, probe=make_probe(execute),
        predicate=(mission.predicate() if mission is not None else None),
    ) if vms_getter else None
    # Ground planning in BOTH state (what is) and findings (what's known) — the two
    # externalized memories that stop the weak model acting on the nonexistent or
    # re-discovering what it already learned. A mission's declared sub_goals seed the ROOT
    # decomposition so the plan tree forms ALONG them — which is how they become reward-
    # bearing: reward-cost's α then books each closed step its share of the mission reward.
    if build_context is None:
        _steps = mission.sub_goals if mission is not None else []
        def build_context(goal, path):
            parts = []
            if _steps and not path:                       # root only — guide the decomposition
                parts.append(render_mission_plan(_steps))
            if vms_getter:
                parts += [s for s in (render_state(vms_getter()), findings.render()) if s]
            return "\n\n".join(parts)
    if method_cache is None:
        method_cache = _seeded_cache()
    # HARD-seed the root decomposition from a mission's declared sub_goals: score.py's
    # depth-0 method-cache path (a known goal shape decomposes DETERMINISTICALLY, no model)
    # then forces the plan tree to form along those exact steps — so they are GUARANTEED
    # reward-bearing under α, not merely nudged by the planning-context hint. Needs ≥2 steps
    # (a single step is atomic, not a decomposition).
    if mission is not None and len(mission.sub_goals) >= 2:
        method_cache.remember(goal, list(mission.sub_goals))
    if watchdog is None:
        watchdog = Watchdog()
    if killswitch is None:                        # arm the safeword kill-switch from the contract
        killswitch = KillSwitch(safeword=_contract.safeword())
    # UNATTENDED backstop: if the contract declares a dead-man's timeout, arm a timer that
    # aborts the run if it goes that long without a sign of life (the engine checks in at
    # each step). None (default) = off — the safeword is the attended stop.
    deadman = None
    _dm_timeout = _contract.deadman_timeout()
    if _dm_timeout:
        deadman = DeadMansSwitch(killswitch, _dm_timeout).start()
    # Reward-cost constants come from the active contract (.grgn). A PRIOR run's
    # reliability feeds FORWARD (the global p_self control): a shakier last run →
    # higher θ/λ this run + a shallower depth budget D_max.
    rc_cfg = _contract.reward_cost_cfg()
    if mission is not None:                  # a tasking may LAYER reward-shaping knobs (alpha,
        rc_cfg = {**rc_cfg, **mission.reward_cost_overrides()}   # H, …) over the contract policy
    if reward is None:                       # payoff for closing the goal
        # A mission's resolved reward (its own, importance-scaled, or the agent default)
        # when tasked via a mission; otherwise the agent's default payoff.
        reward = mission.reward() if mission is not None else _contract.campaign_reward()
    prior_counts: Dict[str, Dict[str, int]] = {}
    # Reliability dials (p_self → θ/λ/D_max) feed FORWARD so the harness self-tightens
    # run-to-run: an explicit in-memory `prior=` wins; otherwise the durable per-agent
    # reliability store closes the loop, so the live drivers inherit last run's stance
    # WITHOUT hand-threading `prior=` (mirrors the p_world/toolstats durability).
    prior_dials = dict(prior) if prior else None
    if prior_dials is None and persist_claims:
        try:
            from ..agent.contract import active_agent_key as _agent_key
            from . import findings_store as _store
            agent_key = agent_key or _agent_key()
            prior_dials = _store.load_reliability(agent_key) or None
        except Exception:
            prior_dials = None
    if prior_dials:
        rc_cfg = {**rc_cfg, "theta": prior_dials.get("theta", rc_cfg.get("theta", 0.0)),
                  "lambda": prior_dials.get("lambda", rc_cfg.get("lambda", 0.5))}
        if prior_dials.get("D_max"):
            max_depth = min(max_depth, prior_dials["D_max"])
        prior_counts = prior_dials.get("tool_counts") or {}   # only an in-memory prior= carries these
    if not prior_counts and persist_claims:       # no in-memory forward-feed → the durable
        try:                                       # per-agent store IS the cross-run p_world memory
            from ..agent.contract import active_agent_key as _agent_key
            from . import findings_store as _store
            agent_key = agent_key or _agent_key()
            prior_counts = _store.load_tool_counts(agent_key)
        except Exception:
            pass
    # LEARNED p_world, updated LIVE as the mission runs: price each primitive by its
    # measured success rate from prior runs' tallies PLUS this mission's events so far
    # (smoothed toward the static default). Recomputed per call against the growing
    # `events` log, so a tool that starts failing mid-mission has its p_world fall and OR
    # ranking routes around it AS THE RUN PROCEEDS — not just between runs.
    # Shared with run_score so p_of reads the SAME ledger the persisted p_world is
    # learned from (score.py records the VERIFIED verdict there). Counting `events`
    # instead would raise a tool's live p_world on a bare tool-return success while
    # the ledger — and the next run — lower it after verification fails: the learned
    # parameter would contradict itself. One source, no contradiction.
    run_ledger: List[Dict[str, Any]] = []
    def p_of(tool: str) -> float:
        counts = _merge_counts(prior_counts, _tool_counts(run_ledger))
        return _p_world_lookup(_p_world_estimate(counts, rc_cfg or None), rc_cfg or None)(tool)
    # Learned-AVERAGE p_world (mean of tool reliability) — the estimator prices a
    # COMPOUND route's unknown sub-tools with this data-grounded prior instead of the
    # static default. LIVE, mirroring p_of: recomputed over prior + this run's ledger
    # each time it's read, so a tool degrading mid-run lowers deep-route pricing too —
    # a frozen-at-start value would contradict the live-p_world design above. None (no
    # history) → compound_ce falls back to the static p_world. (The per-tool Beta prior
    # in _p_world_estimate already pins sparse-data tools near p₀, so this unweighted
    # mean isn't dominated by 1-observation outliers.)
    def compound_p() -> Optional[float]:
        live = _p_world_estimate(_merge_counts(prior_counts, _tool_counts(run_ledger)), rc_cfg or None)
        return (sum(live.values()) / len(live)) if live else None
    # OR worth-it: rank alternatives by CE and prune the ones below θ. The estimator
    # prices the tool each alternative would use (contract risk = cost); θ from rc_cfg.
    estimate = make_ce_estimator(call_model, tools, _contract.tool_risk,
                                 cfg=rc_cfg or None, reward=reward, p_of=p_of, compound_p=compound_p)
    # Per-leaf commit gate (deliberation scales with irreversibility): a reversible leaf
    # always commits (act-observe-correct); an IRREVERSIBLE one only if its simulated CE
    # — priced at the goal reward and the leaf's LEARNED p_world — clears the worth-it bar.
    def commit_gate(tool: str, args: Dict[str, Any]) -> bool:
        return _should_commit(_contract.tool_risk(tool), rc_cfg or None,
                              reward=reward, p=p_of(tool))
    # Reason-validation gate (opt-in — an extra model call per leaf): capture the model's
    # stated reason and check the action against it structurally + against the LIVE STATE
    # (never a self-graded score). state_getter grounds the reason vs reality.
    reason_gate = make_reason_gate(call_model, state_getter=vms_getter) if validate_reasons else None
    # Collective decomposition (Track 1.1): the harness deterministically loops a
    # distributive "do X to all/them" sub-goal over the live entity set — covers the weak
    # model's proven inability to expand a collective operation itself. On whenever we can
    # see the entity set (vms_getter); the per-member steps are atomic, the model's strength.
    expand_collective = make_collective_expander(vms_getter) if vms_getter else None
    # Reference grounding (Track 1.2): bind bare entity references in the model's decomposed
    # steps to the parent's single named entity — deterministic, always on.
    ground_steps = make_step_grounder()
    # Dependency completion (Track 1.4): inject a missing prerequisite (create the network a
    # step attaches to) the weak model drops. Always on; deterministic, plan-level.
    complete_steps = make_prereq_completer()
    engine = Engine(
        gate=gate, verify=verify, verify_goal=verify_goal, referendum=referendum,
        watchdog=watchdog, killswitch=killswitch, findings=findings,
        findings_schema=findings_schema, method_cache=method_cache,
        decompose_first=decompose_first, estimate=estimate,
        ce_floor=(rc_cfg or {}).get("theta", 0.0),
        retry_penalty=(rc_cfg or {}).get("H", 0.0),   # each wasted retry raises the abandon bar
        whole_goal_gate=True,   # refuse a not-worth-it whole goal up-front (α-priced compound/leaf roots)
        max_revisions=max_revisions,   # plan-level self-correction: re-plan a partial composite
        commit_gate=commit_gate,   # per-leaf simulated-ĈE gate for irreversible commits
        reason_gate=reason_gate,   # opt-in two-stage reason validation (validate_reasons)
        on_node=on_node,           # live node-lifecycle events for a streaming tree view
        expand_collective=expand_collective,   # Track 1.1: harness-driven collective decomposition
        ground_steps=ground_steps,             # Track 1.2: bind bare references in decomposed steps
        complete_steps=complete_steps,         # Track 1.4: inject a dropped prerequisite (create network)
    )   # criterion_of/legal_filter default to the active contract inside run_score
    try:
        result = run_score(
            goal,
            call_model=call_model, execute=_exec, tools=tools, engine=engine,
            build_context=build_context, select_tools=select_tools,
            max_retries=max_retries, max_depth=max_depth, max_steps=max_steps, ledger=run_ledger,
        )
    finally:
        if deadman is not None:               # disarm the timer no matter how the run ends
            deadman.stop()
    result["events"] = events
    result["disposition"] = _contract.disposition()
    result["findings"] = {f: findings.get(f) for f in findings.facts()}
    # Unverified claims the run recorded — what no probe could confirm, plus the
    # operator's evidence pointer for each, so a human can close the loop by hand.
    result["claims_for_review"] = findings.claims_for_review()
    # Persist this run's claims (pending + confirmed) back to the per-agent store so
    # a human can review/confirm them AFTER the run — and the next run inherits them.
    if persist_claims:
        try:
            from . import findings_store as _store
            _store.merge_into(agent_key, findings.persistable())
        except Exception:
            pass
    # Accumulate per-tool world-reliability tallies (prior runs + this run) and learn
    # p_world from them. Fed forward two ways: in-memory via `result["tool_counts"]` (pass
    # as the next run's prior) AND durably via the per-agent findings store below, so
    # p_world survives process restarts. p_world is now DATA-GROUNDED, not a static knob.
    run_counts = _tool_counts(result.get("ledger", []))
    all_counts = _merge_counts(prior_counts, run_counts)
    result["tool_counts"] = all_counts
    result["p_world"] = _p_world_estimate(all_counts, rc_cfg or None)
    if persist_claims:                            # persist THIS run's OWN counts (not the merged
        try:                                       # total — the store already holds the prior)
            from . import findings_store as _store
            _store.merge_tool_counts(agent_key, run_counts)
        except Exception:
            pass
    # Reward-cost economics: price the run (μ, σ², CE, cost, reward) using the contract's
    # per-tool risk as the cost source and the LEARNED p_world per tool. Makes the tree
    # reward-cost-aware, with sub-goal closures credited (superadditive, if α > 0).
    _econ_p_of = _p_world_lookup(result["p_world"], rc_cfg or None)
    result["economics"] = _economics(result["root"], cost_of=_contract.tool_risk,
                                      reward=reward, cfg=rc_cfg or None, p_of=_econ_p_of)
    if verbose:
        # PER-NODE economics for the verbose debug view — the caller (CLI) renders it; the
        # loop stays headless. Shows μ/CE/worth_it at every sub-goal so an operator sees
        # WHERE value and uncertainty sit across the plan, not just the run total.
        result["economics_tree"] = _economics_tree(result["root"], cost_of=_contract.tool_risk,
                                                    reward=reward, cfg=rc_cfg or None, p_of=_econ_p_of)
    result["watchdog"] = watchdog.status()
    result["aborted"] = killswitch.tripped
    if killswitch.tripped:
        result["kill_reason"] = killswitch.reason
    # Reliability: measure p_self from this run's ledger → the dials it implies (θ, λ,
    # depth budget), plus the accumulated per-tool tallies for learned p_world. Pass this
    # whole dict as `prior=` to the NEXT run to feed both the dials and p_world forward.
    result["reliability"] = _dials(_p_self(result.get("ledger", [])), rc_cfg or None)
    result["reliability"]["tool_counts"] = all_counts
    if persist_claims:                            # durably chain the p_self dials forward too, so
        try:                                       # the live drivers self-tighten without prior=
            from . import findings_store as _store
            _store.save_reliability(agent_key, result["reliability"])
        except Exception:
            pass
    result["summary"] = _summarize(result)
    return result


def run_autonomous_live(goal: str, **kw) -> Dict[str, Any]:
    """Convenience: wire the REAL Ollama model + executor + Active Library and run.

    Imports are local so this module stays importable (and unit-testable) without the
    runtime. Requires a running Ollama and executor; the active agent is whatever
    GORGON_AGENT points at (a Conductor .grgn for a real autonomous run).
    """
    from ..chat.ollama_client import _call_ollama
    from ..tools import TOOLS
    from ..active_library import LIBRARY
    from orchestrator.executor_client import execute_tool

    kw.setdefault("persist_claims", True)              # the real runtime persists claims
    kw.setdefault("select_tools", make_tool_selector())  # narrow the ~50 tools per node, or the weak model goes mute
    return run_autonomous(
        goal,
        call_model=_call_ollama,                       # prepends the active agent's system prompt
        execute=lambda t, a: execute_tool(t, a),
        tools=TOOLS,
        vms_getter=LIBRARY.vms,
        **kw,
    )

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
from typing import Any, Callable, Dict, List, Optional

from .score import run_score, _first_tool_call, _NODE_SYSTEM, DECOMPOSE_TOOL
from . import contract as _contract
from .method_cache import seeded as _seeded_cache
from .findings import Findings, DEFAULT_SCHEMA
from .reward_cost import (economics as _economics, p_self_estimate as _p_self, dials as _dials,
                          cfg_with as _cfg_with, leaf_cost as _leaf_cost, ce as _ce,
                          tool_counts as _tool_counts, merge_counts as _merge_counts,
                          p_world_estimate as _p_world_estimate, p_world_lookup as _p_world_lookup,
                          compound_ce as _compound_ce, economics_tree as _economics_tree)
from .watchdog import Watchdog
from .engine import Engine
from .killswitch import KillSwitch


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
    """A probe(spec) -> Optional[bool] that verifies a `probe:` predicate clause
    with an actual read-only guest_probe. spec is "vm:assertion:target" (e.g.
    "web01:port_listening:443"). Returns the assertion's truth, or None when it
    can't be verified (malformed spec, or the probe itself failed) — the caller
    treats None as "unverifiable", never as "done"."""
    def probe(spec: str) -> Optional[bool]:
        parts = (spec or "").split(":", 3)            # vm:assertion:target[:value]
        if len(parts) < 3 or not all(parts[:3]):
            return None
        pargs = {"name": parts[0], "assertion": parts[1], "target": parts[2]}
        if len(parts) == 4 and parts[3]:              # file_contains/matches/user_in_group operand
            pargs["value"] = parts[3]
        res = execute("guest_probe", pargs)
        if isinstance(res, dict) and res.get("success"):
            return bool(res.get("holds"))
        return None                                   # channel/agent failure → unverifiable
    return probe


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
            return None
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
            return _compound_ce(n_steps, c, reward=R, p=compound_p, cost=_leaf_cost(None, c))
        cost = _leaf_cost(cost_of(name), c)
        p = p_of(name) if p_of else c["p_world"]
        mu = p * R - cost
        var = p * (1 - p) * R * R
        return _ce(mu, var, c)
    return estimate


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
    max_depth:   int = 3,
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
            from .contract import active_agent_key as _agent_key
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
    if prior:
        rc_cfg = {**rc_cfg, "theta": prior.get("theta", rc_cfg.get("theta", 0.0)),
                  "lambda": prior.get("lambda", rc_cfg.get("lambda", 0.5))}
        if prior.get("D_max"):
            max_depth = min(max_depth, prior["D_max"])
        prior_counts = prior.get("tool_counts") or {}
    if not prior_counts and persist_claims:       # no in-memory forward-feed → the durable
        try:                                       # per-agent store IS the cross-run p_world memory
            from .contract import active_agent_key as _agent_key
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
    def p_of(tool: str) -> float:
        counts = _merge_counts(prior_counts, _tool_counts(events))
        return _p_world_lookup(_p_world_estimate(counts, rc_cfg or None), rc_cfg or None)(tool)
    # Learned-AVERAGE p_world (mean of prior tool reliability) — the estimator prices a
    # COMPOUND route's unknown sub-tools with this data-grounded prior instead of the
    # static default. None (no history) → compound_ce falls back to the static p_world.
    _pw_prior = _p_world_estimate(prior_counts, rc_cfg or None)
    compound_p = (sum(_pw_prior.values()) / len(_pw_prior)) if _pw_prior else None
    # OR worth-it: rank alternatives by CE and prune the ones below θ. The estimator
    # prices the tool each alternative would use (contract risk = cost); θ from rc_cfg.
    estimate = make_ce_estimator(call_model, tools, _contract.tool_risk,
                                 cfg=rc_cfg or None, reward=reward, p_of=p_of, compound_p=compound_p)
    engine = Engine(
        gate=gate, verify=verify, verify_goal=verify_goal, referendum=referendum,
        watchdog=watchdog, killswitch=killswitch, findings=findings,
        findings_schema=findings_schema, method_cache=method_cache,
        decompose_first=decompose_first, estimate=estimate,
        ce_floor=(rc_cfg or {}).get("theta", 0.0),
        retry_penalty=(rc_cfg or {}).get("H", 0.0),   # each wasted retry raises the abandon bar
    )   # criterion_of/legal_filter default to the active contract inside run_score
    result = run_score(
        goal,
        call_model=call_model, execute=_exec, tools=tools, engine=engine,
        build_context=build_context, select_tools=select_tools,
        max_retries=max_retries, max_depth=max_depth,
    )
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
    result["summary"] = _summarize(result)
    return result


def run_autonomous_live(goal: str, **kw) -> Dict[str, Any]:
    """Convenience: wire the REAL Ollama model + executor + Active Library and run.

    Imports are local so this module stays importable (and unit-testable) without the
    runtime. Requires a running Ollama and executor; the active agent is whatever
    GORGON_AGENT points at (a Conductor .grgn for a real autonomous run).
    """
    from .ollama_client import _call_ollama
    from .tools import TOOLS
    from .active_library import LIBRARY
    from orchestrator.executor_client import execute_tool

    kw.setdefault("persist_claims", True)              # the real runtime persists claims
    return run_autonomous(
        goal,
        call_model=_call_ollama,                       # prepends the active agent's system prompt
        execute=lambda t, a: execute_tool(t, a),
        tools=TOOLS,
        vms_getter=LIBRARY.vms,
        **kw,
    )

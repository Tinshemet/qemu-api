"""
engine_core.py — run_score, the recursive goal→primitive decomposition engine.

The irreducible core: run_score and its nested closures (_resolve ↔ _attempt and
the AND/OR closers) capture the injected policy bundle, so they stay one atomic
unit. The stateless helpers, meta-tool schemas, and injected-dep fallbacks are
imported from the sibling modules.
"""

from typing import Any, Callable, Dict, List, Optional

from .meta_tools import DECOMPOSE_TOOL, ALTERNATIVES_TOOL, _NODE_SYSTEM, _OPAQUE_TOOLS
from .ledger_util import _node, _norm, _progress_summary, _attach_steer, _first_tool_call
from ._deps import (
    _default_gate, _default_criterion, _default_legal, _consent_verb, _tool_risk,
    _yield_fact, _extract_value, _finding_probe_spec,
)


def run_score(
    goal: str,
    *,
    call_model:     Callable[[List[Dict], List[Dict]], Dict],
    execute:        Callable[[str, Dict], Any],
    tools:          List[Dict],
    engine=None,
    build_context:  Optional[Callable[[str, List[str]], str]] = None,
    select_tools:   Optional[Callable[[str, List[Dict]], List[Dict]]] = None,
    max_retries:    int = 2,
    max_depth:      int = 3,
    max_steps:      int = 0,
    ledger:         Optional[List[Dict[str, Any]]] = None,
    **_legacy,
) -> Dict[str, Any]:
    """Reduce `goal` to primitive tool calls and execute them; return tree + ledger.

    call_model(messages, tools) -> model response dict (message.tool_calls).
    execute(tool_name, args)    -> result (dict with "success"/"error", ideally).
    tools                       -> the primitive tool schemas offered at every node
                                   (decompose is appended automatically).
    build_context(goal, path)   -> optional str prepended as system grounding
                                   (e.g. the Active Library digest). DETERMINISTIC.
    is_destructive(tool, args)  -> optional; when True + confirm given, ask first.
    confirm(tool, args)         -> optional human backstop; False skips the leaf.
    select_tools(goal, tools)   -> optional PER-NODE tool selection. Return a subset
                                   of `tools` to offer at this node (decompose is
                                   appended). VERIFIED necessary for llama3.1: with
                                   all ~46 tools the weak model replies with pseudo-
                                   code text instead of tool-calling; narrowed to the
                                   node's sub-goal it emits decompose/primitives
                                   correctly (2026-07-17). Default None = all tools.
    verify(criterion, tool,     -> the reality-check for VERIFIED completion. After a
      args, result)                leaf's execute reports success, the tree confirms
                                   the contract's success CRITERION actually holds
                                   (against the Active Library / event_log). Returns
                                   False → the node is `unverified`, NOT done (the
                                   "execute said ok but it didn't really happen" case).
                                   Default None = trust the execute result.
    criterion_of(tool)          -> the per-tool success criterion (what "done" means),
                                   e.g. create_vm -> "present". Default = the active
                                   agent's contract.success_criterion (the contract
                                   declares the criterion; `verify` checks it).
    gate(tool, args)            -> the CONTRACT bound: maps a proposed leaf's risk
                                   tier through the active agent's disposition to a
                                   handling action. "halt" blocks the leaf (a red
                                   line the tree cannot cross); "checkpoint" takes a
                                   savepoint before it; anything else executes. This
                                   is how the contract bounds the tree — dynamic
                                   replanning cannot escape it. Default = the active
                                   agent's contract.gate_action.
    decompose_first             -> DECOMPOSE-FIRST scaffolding. A weak model won't
                                   volunteer decomposition when it can grab a primitive,
                                   so before offering primitives at a node we FORCE the
                                   atomicity question by offering ONLY `decompose`. If
                                   it splits the goal into 2+ real steps → decompose; if
                                   it collapses to the goal itself → the goal is atomic,
                                   offer the primitive. Costs one extra model call per
                                   node. Default off (preserves the offer-both behavior).
    max_retries                 -> BACKTRACK budget per node. A leaf that soft-fails
                                   (failed / unverified) is re-attempted up to this
                                   many times, each time with the approaches that
                                   already failed here fed back so the model can't
                                   repeat them (failed-branch memory). Backtrack is
                                   LOCAL to the failing node, so already-succeeded
                                   siblings are never re-run. 0 = no backtracking.
    max_depth                   -> recursion bound (a node deeper than this is
                                   marked blocked rather than decomposed further).

    Returns {"root": <node>, "ledger": [<executed leaf records>], "ok": bool}.
    A node's status is one of: done / failed / partial / blocked / skipped /
    no_action / unverified. A recovered node carries retries/tried/recovered.
    """
    from orchestrator.ai.planner.engine import Engine
    if engine is None:                       # legacy kwargs (gate=…, verify=…) → Engine
        engine = Engine.from_kwargs(_legacy)
    # Unpack the policy bundle into the local names the body already uses (defaults
    # fall back to the active contract's functions), so the logic below is unchanged.
    gate            = engine.gate or _default_gate
    verify          = engine.verify
    verify_goal     = engine.verify_goal
    criterion_of    = engine.criterion_of or _default_criterion
    legal_filter    = engine.legal_filter or _default_legal
    referendum      = engine.referendum
    watchdog        = engine.watchdog
    killswitch      = engine.killswitch
    findings        = engine.findings
    findings_schema = engine.findings_schema
    method_cache    = engine.method_cache
    decompose_first = engine.decompose_first
    estimate        = engine.estimate
    ce_floor        = engine.ce_floor
    retry_penalty   = engine.retry_penalty
    whole_goal_gate = engine.whole_goal_gate
    max_revisions   = engine.max_revisions
    commit_gate     = engine.commit_gate
    reason_gate     = engine.reason_gate
    on_node         = engine.on_node
    expand_collective = engine.expand_collective
    ground_steps    = engine.ground_steps
    complete_steps  = engine.complete_steps

    def _refine_steps(parent_goal: str, steps: list) -> list:
        """Apply the harness's step-refinement passes to a model decomposition: bind bare
        references (1.2), then inject missing prerequisites (1.4) — so a plausible-but-
        incomplete plan is grounded and made whole before it runs."""
        if ground_steps:
            steps = ground_steps(parent_goal, steps)
        if complete_steps:
            steps = complete_steps(parent_goal, steps)
        return steps

    def _emit(kind: str, node_goal: str, depth: int, path: List[str], **extra) -> None:
        """Fire a live node-lifecycle event (enter/plan/leaf/close) for a streaming tree
        view. A no-op with zero overhead when no observer is attached. Never lets a
        renderer error break the run."""
        if on_node is None:
            return
        try:
            on_node({"kind": kind, "goal": node_goal, "depth": depth, "path": list(path), **extra})
        except Exception:
            pass

    # Caller may pass its own ledger list (so it can read verified verdicts LIVE
    # as the run proceeds — see autonomous.run_autonomous's p_of); default owns one.
    if ledger is None:
        ledger = []
    _RETRY_STATUS = {"failed", "unverified"}   # soft failures worth a different approach
    # THRASHING BOUND (Track 1.5): a broken decomposition can send backtrack × revision ×
    # re-decompose into a call explosion that never converges. `max_steps` caps the total
    # node attempts; once spent, planning stops and the offending node closes `blocked
    # (step_budget)` — the run fails FAST and honestly instead of burning the model. 0 = off.
    _budget = {"n": 0}

    def _approach_desc(node: Dict[str, Any]) -> str:
        """One-line summary of the attempt that just failed — for the retry prompt."""
        if node.get("tool"):
            return f"{node['tool']} → {node['status']}" + (f" ({node['reason']})" if node.get("reason") else "")
        if node.get("children"):
            return "decompose into [" + "; ".join(c["goal"] for c in node["children"]) + f"] → {node['status']}"
        return node.get("status", "?")

    def _fail_detail(node: Dict[str, Any]) -> str:
        """The CONCRETE reason a step failed — its status reason PLUS the executor's own
        error message (Track 1.3/1.4). Surfacing "no such network lab" (not just "failed")
        is what lets the model's re-plan actually FIX the plan (create the missing
        prerequisite) instead of re-emitting the same broken steps."""
        bits = []
        if node.get("reason"):
            bits.append(str(node["reason"]))
        res = node.get("result")
        if isinstance(res, dict) and res.get("error"):
            bits.append(str(res["error"]))
        return f" ({'; '.join(bits)})" if bits else ""

    def _plan_desc(node: Dict[str, Any]) -> str:
        """One-line post-mortem of a composite plan that came up short — for the REVISION
        prompt. Marks each step done (✓) or failed (✗ + CONCRETE error) so the model re-plans
        the CORRECTIVE remainder — creating a missing prerequisite, grounding a reference —
        instead of repeating the decomposition that fell short."""
        parts = []
        for c in node.get("children") or []:
            if c.get("status") == "done":
                parts.append(f"✓ {c['goal']}")
            else:
                parts.append(f"✗ {c['goal']}{_fail_detail(c)}")
        return "plan [" + "; ".join(parts) + f"] → {node.get('status')}"

    def _root_gate(node_goal: str, depth: int, children: List[Dict[str, Any]],
                   extra: Dict[str, Any]) -> Dict[str, Any]:
        """The CONTRACT ROOT PREDICATE (gauntlet E). A composite whose children satisfy
        it is 'done' — UNLESS it is the ROOT and the contract's goal predicate says the
        goal does not actually hold, in which case it is `unverified` (books NO reward:
        a clean-executing WRONG plan earns nothing). Gated to depth 0 on purpose —
        intermediate composites have no contract-declared end-state, and inventing one
        is the design's flagged soft-underbelly. verify_goal → True/False/None(no-op).
        """
        if depth == 0 and verify_goal is not None:
            if verify_goal(node_goal, children, ledger) is False:
                return _node(node_goal, "unverified", children=children,
                             reason="goal_predicate_unmet", **extra)
        return _node(node_goal, "done", children=children, **extra)

    def _close_and(node_goal: str, depth: int, children: List[Dict[str, Any]],
                   **extra) -> Dict[str, Any]:
        """AND closure: every child is a REQUIRED step — all must be done, else partial.
        (all-done is necessary but, at the root, not sufficient — the predicate decides.)"""
        if not all(c.get("status") == "done" for c in children):
            return _node(node_goal, "partial", children=children, **extra)
        return _root_gate(node_goal, depth, children, extra)

    def _rank_alternatives(opts: List[str], depth: int) -> tuple:
        """WORTH-IT ordering + pruning for OR alternatives (gauntlet C/F). Price each
        alternative's CE (estimate → pre-execution guess from the tool it'd use), TRY
        the highest-CE first, and PRUNE any whose CE ≤ θ (the worth-it floor) — those
        are booked as forgone, never executed. Returns (to_try, pruned) as lists of
        (option, ce). No estimator → keep the model's given order, prune nothing (the
        act-observe-correct default)."""
        if estimate is None:
            return [(o, None) for o in opts], []
        scored = [(o, estimate(o, depth)) for o in opts]
        priced   = [(o, s) for o, s in scored if s is not None]
        unpriced = [(o, None) for o, s in scored if s is None]   # couldn't price → don't prune it
        keep   = sorted([(o, s) for o, s in priced if s > ce_floor], key=lambda x: x[1], reverse=True)
        pruned = [(o, s) for o, s in priced if s <= ce_floor]
        return keep + unpriced, pruned            # priced-best-first, then unpriced in order

    def _close_or(node_goal: str, depth: int, children: List[Dict[str, Any]],
                  satisfied: bool, **extra) -> Dict[str, Any]:
        """OR closure: children are ALTERNATIVES to the same goal — ONE done is enough.
        `satisfied` says an alternative succeeded; none → failed (a soft failure that
        backtracks). Carries mode='or' so the economics prices it as max-over-alts."""
        if not satisfied:
            return _node(node_goal, "failed", children=children, mode="or", **extra)
        return _root_gate(node_goal, depth, children, {"mode": "or", **extra})

    def _resolve(node_goal: str, depth: int, path: List[str],
                 best_alt: float = 0.0) -> Dict[str, Any]:
        _emit("enter", node_goal, depth, path)
        node = _resolve_inner(node_goal, depth, path, best_alt)
        _emit("close", node_goal, depth, path, status=node.get("status"),
              reason=node.get("reason"), revised=node.get("revised"))
        return node

    def _resolve_inner(node_goal: str, depth: int, path: List[str],
                       best_alt: float = 0.0) -> Dict[str, Any]:
        # WHOLE-GOAL WORTH-IT GATE (gauntlet F, top level): before touching the ROOT
        # goal, price it and refuse up-front if it isn't worth doing — the go/no-go the
        # OR gate already applies to alternatives, lifted to the whole goal. ROOT only
        # (depth 0): AND sub-steps are REQUIRED (you can't skip one and still claim the
        # goal), so only the whole goal and OR-alternatives carry a worth-it gate.
        # Unpriceable (no estimator, or a compound route the estimator won't cost at
        # α=0) → proceed, the act-observe-correct default. Books no reward and executes
        # nothing — the gate legitimately choosing inaction, surfaced not silently done.
        # Opt-in (whole_goal_gate): the autonomous driver turns it on; run_score's OR /
        # backtrack unit tests leave it off so their stub estimators aren't pre-empted.
        if whole_goal_gate and depth == 0 and estimate is not None:
            root_ce = estimate(node_goal, depth)
            if root_ce is not None and root_ce <= ce_floor:
                return _node(node_goal, "skipped", mode="whole_goal",
                             reason="not_worth_it", ce_est=round(root_ce, 4))
        # BACKTRACK: attempt the goal; on a SOFT failure (failed / unverified),
        # re-attempt with a DIFFERENT approach, feeding the model the approaches that
        # already failed HERE so it can't repeat them. Hard stops (done / skipped /
        # no_action / blocked, incl. contract_halt) never backtrack; and because we
        # retry the failing node itself (not its parent), succeeded siblings stand.
        #
        # CE-BASED ABANDON (gauntlet F): don't retry to the budget blindly — ABANDON as
        # soon as a fresh attempt is worth no more than the opportunity cost. Continue-
        # value = estimate(goal) − H·(retries so far): each wasted try raises the bar by
        # the holding cost, so a marginal goal is dropped early while a high-CE one keeps
        # its full budget. The floor is max(0, best_alt): 0 = the always-free do-nothing
        # option; best_alt = the next-best alternative's CE (passed by an OR parent), so
        # a failing alternative is abandoned in favour of a better sibling. No estimator
        # → the plain max_retries budget (backward compatible).
        floor = max(0.0, best_alt)
        failed: List[str] = []
        mark  = len(ledger)
        node  = _attempt(node_goal, depth, path, True, failed)
        tries = rolled = 0
        while node.get("status") in _RETRY_STATUS and tries < max_retries:
            give_up = False
            cont = None
            if estimate is not None:
                cont = estimate(node_goal, depth)
                if cont is not None:
                    cont -= retry_penalty * tries
                    give_up = cont <= floor
            # Rollback-on-backtrack: if the failed attempt was gate-checkpointed
            # (an autonomous destructive leaf), UNDO its side effects — restore the
            # savepoint and drop its now-stale ledger records — so the next step (retry
            # OR abandon) starts from clean state. Non-checkpointed leaves have nothing
            # to undo.
            if node.get("checkpoint"):
                execute("rollback", {"label": node["checkpoint"]})
                del ledger[mark:]
                rolled += 1
            if give_up:
                node["abandoned"] = True
                node["abandon"] = {"continue_ce": round(cont, 4), "floor": round(floor, 4)}
                break
            failed.append(_approach_desc(node))
            tries += 1
            # A re-attempt SKIPS the method cache: the cached decomposition is exactly the
            # one that just failed (the root-replan landmine), so re-planning must reach the
            # model, not re-hit the same short-circuit.
            node = _attempt(node_goal, depth, path, True, failed, use_cache=False)
        if tries:
            node["retries"] = tries
            node["tried"]   = list(failed)
            if rolled:
                node["rolled_back"] = rolled
            if node.get("status") == "done":
                node["recovered"] = True

        # PLAN-LEVEL REVISION (self-correction): an AND plan that came up `partial` — a
        # REQUIRED step failed for good — is not a dead branch. Re-PLAN the goal: the
        # model sees what's already done (progress summary, injected in _attempt) plus a
        # post-mortem of which steps failed, so it produces the CORRECTIVE remainder
        # rather than repeating the decomposition that fell short. Distinct from the leaf
        # backtrack above (same sub-goal, new approach) — this regenerates the PLAN. OR
        # nodes already re-plan via backtrack (a failed OR is a soft failure); leaves
        # can't be re-planned. Re-attempts skip the cache (same landmine). Off unless
        # max_revisions > 0 (the autonomous driver turns it on; run_score defaults off).
        revisions = 0
        while (max_revisions and revisions < max_revisions
               and node.get("children") and node.get("status") == "partial"):
            failed.append(_plan_desc(node))
            revisions += 1
            node = _attempt(node_goal, depth, path, True, failed, use_cache=False)
        if revisions:
            node["revisions"] = revisions
            if node.get("status") == "done":
                node["revised"] = True
        return node

    def _attempt(node_goal: str, depth: int, path: List[str],
                 allow_decompose: bool, failed: List[str],
                 use_cache: bool = True) -> Dict[str, Any]:
        # SAFEWORD KILL-SWITCH (infrastructural): if the operator tripped it, stop the
        # tree HERE — no planning, no execution. The agent gets no say; the ledger so
        # far is preserved (suspend, not delete).
        if killswitch is not None and killswitch.tripped:
            return _node(node_goal, "aborted", reason=killswitch.reason)
        _budget["n"] += 1                 # count every node attempt (Track 1.5 thrashing bound)
        if max_steps and _budget["n"] > max_steps:
            return _node(node_goal, "blocked", reason="step_budget")
        if killswitch is not None:
            killswitch.checkin()          # a sign of life — resets any armed dead-man's timer
        # COLLECTIVE DECOMPOSITION (Track 1.1): a DISTRIBUTIVE "do X to all/them/each" over a
        # live set is expanded deterministically into one atomic sub-goal per member — the
        # HARNESS does the loop the weak model can't (the benchmark cliff). Runs FIRST, at any
        # depth, taking precedence over attach-steer/decompose-first so a collective goal is
        # never collapsed to a single action or left to the model to loop. No model call, no
        # variance; per-member sub-goals are atomic, so they never re-expand.
        if expand_collective is not None and allow_decompose and depth < max_depth:
            csteps = expand_collective(node_goal, path)
            if csteps and len(csteps) >= 2:
                _emit("plan", node_goal, depth, path, children=list(csteps), mode="and", method="collective")
                children = [_resolve(s, depth + 1, path + [node_goal]) for s in csteps]
                return _close_and(node_goal, depth, children, method="collective")
        system = _NODE_SYSTEM
        if build_context:
            ctx = build_context(node_goal, path)
            if ctx:
                system += "\n\n" + ctx
        # Carry-forward: what earlier steps in this plan already produced, so late
        # steps ground references ("launch probe") to the real entity they created.
        prog = _progress_summary(ledger)
        if prog:
            system += "\n\n" + prog
        # Failed-branch memory: on a retry, the approaches already tried at THIS goal.
        if failed:
            system += ("\n\n═══ ALREADY TRIED HERE (failed — take a DIFFERENT approach, do NOT repeat) ═══\n"
                       + "\n".join(f"- {d}" for d in failed))
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": f"Goal: {node_goal}"}]
        base = select_tools(node_goal, tools) if select_tools else list(tools)
        # Ledger-aware: after a network (etc.) was created earlier in THIS plan, steer
        # a node referencing it to ATTACH, not re-create. An attach-to-existing is one
        # primitive, so when steered we drop decompose to stop over-decomposition.
        base, steered = _attach_steer(base, node_goal, ledger, tools)

        offer_decompose = allow_decompose and not steered
        # DECOMPOSE-FIRST scaffolding: force the atomicity question by offering ONLY
        # decompose (primitives off the table), so the model can't dodge a compound
        # goal into a one-shot. 2+ real steps → decompose; 0-1 (collapses to the goal)
        # → atomic, fall through to a primitive. ROOT ONLY (depth 0): forcing it at
        # every level makes the weak model OVER-decompose atomic sub-goals ("create a
        # vm named beta" → "create a vm" + "name it beta" = junk). Sub-goals use the
        # natural primitive-first path, which one-shots atomic goals correctly.
        if decompose_first and offer_decompose and depth == 0:
            # METHOD CACHE first: a known goal shape decomposes DETERMINISTICALLY (no
            # model, no variance). Only a novel goal reaches the model, and a good
            # model decomposition is LEARNED back into the cache ("un-reasons over time").
            cached = method_cache.lookup(node_goal) if (method_cache and use_cache) else None
            if cached and len(cached) >= 2:
                _emit("plan", node_goal, depth, path, children=list(cached), mode="and", method="cache")
                children = [_resolve(s, depth + 1, path + [node_goal]) for s in cached]
                return _close_and(node_goal, depth, children, method="cache")
            plan_msgs = messages + [{"role": "user", "content": (
                "PLAN FIRST. Break this goal into the smallest ORDERED list of individual "
                "actions — one tool call each. If it is truly a single action, return just "
                "that one step. Call decompose with the steps.")}]
            pname, pargs = _first_tool_call(call_model(plan_msgs, [DECOMPOSE_TOOL]))
            if pname == "decompose":
                steps = [s for s in (pargs.get("steps") or []) if _norm(s) != _norm(node_goal)]
                steps = _refine_steps(node_goal, steps)           # Track 1.2 ground + 1.4 complete
                if len(steps) >= 2:
                    if method_cache:
                        method_cache.remember(node_goal, steps)   # learn this decomposition
                    _emit("plan", node_goal, depth, path, children=list(steps), mode="and", method="model")
                    children = [_resolve(s, depth + 1, path + [node_goal]) for s in steps]
                    return _close_and(node_goal, depth, children, method="model")
            offer_decompose = False   # atomic (or the model refused) → let it pick a primitive

        # Both meta-tools ride with the primitives when decomposition is allowed:
        # `decompose` (AND, ordered steps) and `alternatives` (OR, one-of).
        offered = base + ([DECOMPOSE_TOOL, ALTERNATIVES_TOOL] if offer_decompose else [])
        name, args = _first_tool_call(call_model(messages, offered))

        if name is None:
            return _node(node_goal, "no_action")

        if name == "decompose":
            # Drop non-progressing steps — the weak model often "decomposes" an atomic
            # goal into itself. If nothing progresses (or we're too deep), re-ask WITHOUT
            # the meta-tools so the model MUST pick a primitive (the progress guard).
            steps = [s for s in (args.get("steps") or []) if _norm(s) != _norm(node_goal)]
            steps = _refine_steps(node_goal, steps)               # Track 1.2 ground + 1.4 complete
            if not steps or depth >= max_depth:
                if allow_decompose:
                    return _attempt(node_goal, depth, path, False, failed, use_cache)
                return _node(node_goal, "blocked", reason="no_progress")
            _emit("plan", node_goal, depth, path, children=list(steps), mode="and")
            children = [_resolve(s, depth + 1, path + [node_goal]) for s in steps]
            return _close_and(node_goal, depth, children)

        if name == "alternatives":
            # OR goal: try each alternative in order, STOP at the first that's done, and
            # mark the untried rest `skipped` (they were never needed). A failed
            # alternative that took a savepoint is ROLLED BACK before the next one, so
            # each alternative starts from clean state (same discipline as backtrack).
            opts = [o for o in (args.get("options") or []) if _norm(o) != _norm(node_goal)]
            if len(opts) < 2 or depth >= max_depth:   # not real alternatives → force a primitive
                if allow_decompose:
                    return _attempt(node_goal, depth, path, False, failed, use_cache)
                return _node(node_goal, "blocked", reason="no_alternatives")
            # WORTH-IT: rank by CE, try best first, and prune the alternatives not worth trying.
            to_try, pruned = _rank_alternatives(opts, depth + 1)
            pruned_nodes = [_node(o, "skipped", reason="pruned_low_ce",
                                  ce_est=round(s, 4)) for o, s in pruned]
            if not to_try:
                # every alternative is below the worth-it floor → don't pursue this goal
                # (the gate legitimately choosing inaction; surfaced, not silently done).
                return _node(node_goal, "skipped", children=pruned_nodes, mode="or",
                             reason="not_worth_it")
            _emit("plan", node_goal, depth, path, mode="or",
                  children=[o for o, _ in to_try] + [o for o, _ in pruned])
            children: List[Dict[str, Any]] = []
            satisfied = False
            for i, (opt, est) in enumerate(to_try):
                mark = len(ledger)
                # Opportunity cost for abandoning this alternative's retries = the CE of
                # the next-best alternative still to try (0 if it's the last one).
                nxt = to_try[i + 1][1] if i + 1 < len(to_try) else None
                best_alt = float(nxt) if isinstance(nxt, (int, float)) else 0.0
                child = _resolve(opt, depth + 1, path + [node_goal], best_alt=best_alt)
                if est is not None:
                    child["ce_est"] = round(est, 4)
                children.append(child)
                if child.get("status") == "done":
                    satisfied = True
                    children += [_node(o, "skipped", reason="alt_satisfied") for o, _ in to_try[i + 1:]]
                    break
                # this alternative failed — undo any savepoint residue before the next
                cps = [e for e in ledger[mark:] if e.get("tool") == "checkpoint"]
                if cps:
                    execute("rollback", {"label": cps[0]["args"]["label"]})
                    del ledger[mark:]
            children += pruned_nodes
            return _close_or(node_goal, depth, children, satisfied)

        # A primitive → a leaf. The active agent's CONTRACT bounds what the tree may
        # do here: gate() maps the tool's risk tier through the agent's disposition.
        # HALT is a red line the tree cannot cross (blocked, never executed) — so
        # dynamic replanning can't escape the contract. CHECKPOINT takes a savepoint
        # FIRST, so a destructive-but-authorized leaf stays revertible (the
        # autonomous act-observe-correct default).
        # LEGAL FILTER (gauntlet A): a hard, categorical red line — dropped up front,
        # never costed, never surfaced. Distinct from the destructiveness/consent axis.
        if legal_filter and legal_filter(name, args):
            return _node(node_goal, "forbidden", tool=name, args=args, reason="legal_red_line")

        # Anti-rediscovery (Findings ledger): if this call's finding is already
        # known, don't re-run the tool that would learn it — return the cached fact.
        fact = _yield_fact(name, args, findings_schema) if (findings is not None and findings_schema) else None
        if fact and findings.has(fact):
            return _node(node_goal, "done", tool=name, args=args, cached_finding=fact,
                         result={"finding": fact, "value": findings.get(fact), "cached": True})

        # SIMULATED ĈE COMMIT GATE (deliberation scales with irreversibility): a
        # REVERSIBLE leaf just acts — reality is a free act-observe-correct oracle, no
        # simulation. An IRREVERSIBLE leaf commits only if its SIMULATED certainty-
        # equivalent clears the worth-it bar; otherwise it's blocked here, before any
        # savepoint or execution. Distinct from the whole-goal gate (prices the GOAL) and
        # the checkpoint path (revertibility): this is the per-leaf irreversible go/no-go.
        # commit_gate returns True for reversible/unknown risk (the default when unset).
        if commit_gate is not None and not commit_gate(name, args):
            return _node(node_goal, "blocked", tool=name, args=args, reason="not_worth_committing")

        # REASON-VALIDATION GATE (two-stage, opt-in): capture the model's stated REASON for
        # this action, then check the ACTION against it as a spec — STRUCTURAL, not a
        # model-judge (the p_self firewall). Flags clear divergence (no reason; the target
        # absent from the justification; or — grounded — a reason that CONTRADICTS the live
        # state). The stated reason is RECORDED on the leaf (rationale) and streamed to the
        # live tree, so even a passed lie stays auditable after the run.
        rationale = None
        if reason_gate is not None:
            rg = reason_gate(node_goal, name, args)
            problem   = rg.get("problem") if isinstance(rg, dict) else rg
            rationale = rg.get("reason")  if isinstance(rg, dict) else None
            if problem:
                return _node(node_goal, "blocked", tool=name, args=args, rationale=rationale,
                             reason=f"reason_mismatch:{problem}")
        _rat = {"rationale": rationale} if rationale else {}

        act = gate(name, args) if gate else "proceed"
        checkpoint_label = None
        if act == "halt":
            # CONSENT SURFACE (gauntlet D): destructive-but-legal → a referendum WITH
            # its consequence. Granted → proceed (kept revertible via checkpoint);
            # denied, or no referendum handler → blocked. (Was a categorical halt.)
            if referendum and referendum(name, args, _consent_verb(name)):
                act = "checkpoint"
            else:
                return _node(node_goal, "blocked", tool=name, args=args,
                             reason="consent_denied" if referendum else "contract_halt")
        if act == "checkpoint":
            checkpoint_label = f"pre_{name}_{len(ledger)}"
            cp    = execute("checkpoint", {"label": checkpoint_label})
            cp_ok = not (isinstance(cp, dict) and (cp.get("success") is False or cp.get("error")))
            ledger.append({"goal": node_goal, "tool": "checkpoint", "args": {"label": checkpoint_label},
                           "ok": cp_ok, "result": cp})
            if not cp_ok:               # can't make it revertible → don't do the irreversible thing
                return _node(node_goal, "blocked", reason="checkpoint_failed", tool=name, args=args)
        # Carry the savepoint label onto the leaf so backtrack can roll back to it.
        _cp = {"checkpoint": checkpoint_label} if checkpoint_label else {}

        # WATCHDOG (farming/loop): a signature throttled for zero-progress repetition
        # is blocked (reversibly) — the deterministic backstop once the tree acts.
        if watchdog is not None and watchdog.throttled(name, args):
            return _node(node_goal, "blocked", tool=name, args=args, reason="watchdog_throttle")

        # Kill-switch may have tripped during planning — check once more before we ACT.
        if killswitch is not None and killswitch.tripped:
            return _node(node_goal, "aborted", tool=name, args=args, reason=killswitch.reason)

        _emit("leaf", node_goal, depth, path, tool=name, args=args, rationale=rationale)
        result = execute(name, args)
        ok = not (isinstance(result, dict) and (result.get("success") is False or result.get("error")))

        # Record what this call LEARNED into the Findings ledger (its epistemic
        # result), so acceptance can read it and the loop won't re-discover it.
        new_finding = bool(fact) and (findings is not None) and not findings.has(fact)
        if ok and fact:
            # Deterministic finding-validation: if the schema declares a `verify`
            # probe for this finding, an independent read-only guest_probe must
            # CONFIRM it before it's recorded. A value read from (possibly free-text)
            # output that a probe can't back up doesn't count — closes the "trust the
            # extracted value" hole. No `verify` → records as before.
            _confirmed = True
            _vspec = _finding_probe_spec(name, args, findings_schema)
            if _vspec:
                _p = _vspec.split(":", 3)             # vm:assertion:target[:value]
                if len(_p) >= 3 and all(_p[:3]):
                    _pargs = {"name": _p[0], "assertion": _p[1], "target": _p[2]}
                    if len(_p) == 4 and _p[3]:
                        _pargs["value"] = _p[3]
                    _pr = execute("guest_probe", _pargs)
                    _confirmed = isinstance(_pr, dict) and _pr.get("success") and bool(_pr.get("holds"))
                else:
                    _confirmed = False
            if _confirmed:
                # An unverified claim carries `evidence` (the operator's note on where
                # they found it) through the result — preserve it on the ledger entry
                # so a human can check what no probe could.
                _ev = result.get("evidence") if isinstance(result, dict) else None
                findings.record(fact, _extract_value(result, findings_schema[name]),
                                source=name, evidence=_ev)
            else:
                new_finding = False   # unconfirmed → not learned; don't credit anti-rediscovery
        # Staleness fix: a state-mutating call (a tool the contract assessed as risky)
        # invalidates findings ABOUT the entities it touched, so anti-rediscovery can't
        # hand back a stale fact after the world changed under it.
        if ok and fact is None and findings is not None and _tool_risk(name):
            for v in (args or {}).values():
                if isinstance(v, str):
                    findings.invalidate_about(v)
        if watchdog is not None:
            watchdog.observe(name, args, new_finding=new_finding, result=result)

        # Verified completion: a leaf is DONE only if the contract's success
        # criterion actually holds in reality — not just because execute returned
        # success. The contract declares the criterion (criterion_of); `verify`
        # checks it against ground truth. A criterion that fails → `unverified`.
        if ok and verify and criterion_of:
            crit = criterion_of(name)
            if crit and not verify(crit, name, args, result):
                ledger.append({"goal": node_goal, "tool": name, "args": args,
                               "ok": False, "verified": False, "result": result})
                return _node(node_goal, "unverified", tool=name, args=args,
                             result=result, reason=f"criterion_unmet:{crit}", **_cp, **_rat)

        # Honesty rule (foreign-command grounding): an OPAQUE command with no declared
        # post-condition can't be trusted on its exit flag — surface it as UNVERIFIABLE,
        # never silently `done`. It books no reward until a criterion or probe confirms
        # the effect. (If the contract DID declare a criterion, the block above verified
        # it, so criterion_of(name) is truthy here and this doesn't fire.)
        if ok and name in _OPAQUE_TOOLS and not (criterion_of and criterion_of(name)):
            ledger.append({"goal": node_goal, "tool": name, "args": args,
                           "ok": False, "verified": False, "result": result})
            return _node(node_goal, "unverified", tool=name, args=args,
                         result=result, reason="unverifiable", **_cp, **_rat)

        ledger.append({"goal": node_goal, "tool": name, "args": args, "ok": ok, "result": result})
        return _node(node_goal, "done" if ok else "failed", tool=name, args=args, result=result, **_cp, **_rat)

    root = _resolve(goal, 0, [])
    return {"root": root, "ledger": ledger, "ok": root.get("status") == "done"}

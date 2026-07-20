"""
score.py — the Score: the recursive goal→primitive decomposition engine (+ ledger).

Named per the orchestra theme: a Conductor works from a SCORE — a goal decomposed
into what each instrument (tool) plays, step by step; "keeping score" is the ledger.
Shallow for the Doorman, deep for Conductors later — this is the shared engine.

The unreason gate, as code: at each node the model gets ONE choice — emit a single
grounded PRIMITIVE tool call (→ a leaf, executed) OR call the `decompose` meta-tool
with an ordered list of sub-goals (→ recurse on each). The model PROPOSES; whether a
node is atomic is decided objectively by WHICH it returned, never self-certified.
Long-horizon behavior emerges from the reduction, not from the model planning ahead.

MVP scope: decompose → execute in order → ledger → optional human backstop, depth-
bounded. DEFERRED to the Conductor: cost/destructiveness weights, backtrack +
failed-branch memory, verified-completion at the parent, active-heads frontier, the
contract/weight governance.

Dependencies are INJECTED (call_model / execute / tools) so the engine is fully
testable without Ollama or the live loop. Per the 2026-07-17 tool-narrowing finding,
each node is offered the FULL tool set (+ decompose), not a narrowed one — the fuller
context anchors the weak model.
"""
from typing import Any, Callable, Dict, List, Optional

# Ledger-aware attach steering (data-driven from the tool registry). Optional so the
# engine still imports in orchestrator-only checkouts / pure-unit tests without the
# executor package — steering is simply skipped then.
try:
    from executor.command_catalog import POST_CREATE_ATTACH as _POST_CREATE_ATTACH
    from orchestrator.ai.context_assistant import _NARROW_CORE_TOOLS
    from orchestrator.ai.contract import gate_action as _default_gate
    from orchestrator.ai.contract import success_criterion as _default_criterion
    from orchestrator.ai.findings import (yield_fact as _yield_fact, extract_value as _extract_value,
                                           finding_probe_spec as _finding_probe_spec)
    from orchestrator.ai.contract import is_forbidden as _default_legal, consent_verb as _consent_verb
    from orchestrator.ai.contract import tool_risk as _tool_risk
except ImportError:
    _POST_CREATE_ATTACH: Dict[str, Dict[str, str]] = {}
    _NARROW_CORE_TOOLS = frozenset()
    _default_gate = None
    _default_criterion = None
    _yield_fact = lambda *a, **k: None
    _extract_value = lambda r, s: r
    _finding_probe_spec = lambda *a, **k: None
    _default_legal = None
    _consent_verb = lambda t: t
    _tool_risk = lambda t: None


# OPAQUE tools: their result is free-text, so success isn't self-evident from the
# call. The honesty rule (foreign-command grounding): an opaque command with no
# declared post-condition must surface as UNVERIFIABLE, never silently `done`.
_OPAQUE_TOOLS = {"run_guest_command"}


# The meta-tool the model calls to say "this isn't one primitive — here are the steps".
DECOMPOSE_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "decompose",
        "description": (
            "Use ONLY when the goal needs MORE than one primitive tool call. Break it "
            "into an ordered list of smaller sub-goals; each will then be handled by a "
            "single tool call (or decomposed again if still too big). If the goal can be "
            "done with ONE tool call, call THAT tool instead — do not decompose."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Ordered sub-goals in PLAIN ENGLISH, e.g. "
                        "['create a ubuntu vm called dev', 'launch dev']. Each is a short "
                        "instruction of WHAT to do — NEVER tool names or tool-call syntax "
                        "like create_vm(name=...). Describe the action in words."
                    ),
                },
            },
            "required": ["steps"],
        },
    },
}

# The meta-tool for an OR goal: several ways to get there, only ONE need succeed.
ALTERNATIVES_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "alternatives",
        "description": (
            "Use ONLY when the goal has SEVERAL possible approaches and just ONE needs "
            "to succeed (an OR goal). List the alternative ways to achieve the SAME goal, "
            "MOST-LIKELY first; they are tried in order and the first that works wins — "
            "the rest are skipped. This is DIFFERENT from `decompose`, whose steps must "
            "ALL be done. If the goal is one action, call THAT tool instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Alternative sub-goals in PLAIN ENGLISH, best first — each a "
                        "COMPLETE way to achieve the goal on its own, e.g. "
                        "['reboot the vm via the guest agent', 'force-reset the vm']. "
                        "NEVER tool names or call syntax."
                    ),
                },
            },
            "required": ["options"],
        },
    },
}

_NODE_SYSTEM = (
    "You are reducing a goal to tool calls. For the goal below, either call ONE tool "
    "that fully accomplishes it, or — if it needs more than one primitive action — call "
    "`decompose` with the ordered sub-steps. Prefer a single tool call when possible."
)


def _node(goal: str, status: str, **kw) -> Dict[str, Any]:
    return {"goal": goal, "status": status, **kw}


def _norm(s: str) -> str:
    """Normalize a goal string for no-progress comparison."""
    return " ".join(str(s).lower().split())


def _progress_summary(ledger: List[Dict[str, Any]]) -> str:
    """What earlier steps in THIS plan already did — so a later step ('launch probe')
    knows the entity it references was just created, and uses its exact name instead
    of re-discovering (and mis-picking) it. This is the ledger's carry-forward.
    """
    if not ledger:
        return ""
    lines = []
    for e in ledger:
        a = e.get("args", {})
        name = a.get("name") or a.get("new_name") or a.get("net_name") or a.get("label") or ""
        mark = "" if e.get("ok") else "  (FAILED)"
        lines.append(f"- {e['tool']}: {name}{mark}" if name else f"- {e['tool']}{mark}")
    return ("PLAN PROGRESS — steps ALREADY done (use these EXACT names; do NOT re-create "
            "or re-discover them):\n" + "\n".join(lines))


def _attach_steer(base: List[Dict], node_goal: str, ledger: List[Dict[str, Any]],
                  tools: List[Dict]) -> tuple:
    """Ledger-aware tool steering (data-driven from POST_CREATE_ATTACH).

    Once a creator (e.g. create_network) has run in THIS plan, a later node that
    references the created entity should ATTACH to it, not re-create it. So we
    return a TIGHT set — the attach tool + always-available core — with the creator
    dropped, removing the re-create temptation that made 'put probe on the new
    network' resolve to create_network again. VERIFIED to flip that node (and
    'add probe to labnet') to add_vm_to_network with correct args, 4/4 (2026-07-17).

    Returns (tools, steered). When steered is True the node is an attach-to-existing,
    which is inherently ONE primitive — the caller drops decompose so the weak model
    can't over-decompose it into a spurious 're-create then attach'. When nothing is
    referenced, returns (base, False) unchanged.
    """
    if not _POST_CREATE_ATTACH:
        return base, False
    low = node_goal.lower()
    by_name = {t.get("function", {}).get("name"): t for t in tools}
    for creator, spec in _POST_CREATE_ATTACH.items():
        made = [e["args"].get(spec["name_arg"]) for e in ledger
                if e.get("tool") == creator and e.get("ok")]
        made = [n for n in made if n]
        if not made:
            continue
        referenced = spec["keyword"] in low or any(str(n).lower() in low for n in made)
        attach = by_name.get(spec["attach"])
        if referenced and attach is not None:
            core = [t for t in tools if t.get("function", {}).get("name") in _NARROW_CORE_TOOLS]
            tight = [attach] + [t for t in core if t is not attach]
            return tight, True
    return base, False


def _first_tool_call(resp: Any) -> tuple:
    """Extract (name, args) of the model's first tool call, or (None, None)."""
    msg = (resp or {}).get("message", {}) if isinstance(resp, dict) else {}
    tcs = msg.get("tool_calls") or []
    if not tcs:
        return None, None
    fn = tcs[0].get("function", {})
    args = fn.get("arguments", {})
    if isinstance(args, str):
        import json
        try:
            args = json.loads(args)
        except Exception:
            args = {}
    return fn.get("name"), (args or {})


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
    from orchestrator.ai.engine import Engine
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

    ledger: List[Dict[str, Any]] = []
    _RETRY_STATUS = {"failed", "unverified"}   # soft failures worth a different approach

    def _approach_desc(node: Dict[str, Any]) -> str:
        """One-line summary of the attempt that just failed — for the retry prompt."""
        if node.get("tool"):
            return f"{node['tool']} → {node['status']}" + (f" ({node['reason']})" if node.get("reason") else "")
        if node.get("children"):
            return "decompose into [" + "; ".join(c["goal"] for c in node["children"]) + f"] → {node['status']}"
        return node.get("status", "?")

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
            node = _attempt(node_goal, depth, path, True, failed)
        if tries:
            node["retries"] = tries
            node["tried"]   = list(failed)
            if rolled:
                node["rolled_back"] = rolled
            if node.get("status") == "done":
                node["recovered"] = True
        return node

    def _attempt(node_goal: str, depth: int, path: List[str],
                 allow_decompose: bool, failed: List[str]) -> Dict[str, Any]:
        # SAFEWORD KILL-SWITCH (infrastructural): if the operator tripped it, stop the
        # tree HERE — no planning, no execution. The agent gets no say; the ledger so
        # far is preserved (suspend, not delete).
        if killswitch is not None and killswitch.tripped:
            return _node(node_goal, "aborted", reason=killswitch.reason)
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
            cached = method_cache.lookup(node_goal) if method_cache else None
            if cached and len(cached) >= 2:
                children = [_resolve(s, depth + 1, path + [node_goal]) for s in cached]
                return _close_and(node_goal, depth, children, method="cache")
            plan_msgs = messages + [{"role": "user", "content": (
                "PLAN FIRST. Break this goal into the smallest ORDERED list of individual "
                "actions — one tool call each. If it is truly a single action, return just "
                "that one step. Call decompose with the steps.")}]
            pname, pargs = _first_tool_call(call_model(plan_msgs, [DECOMPOSE_TOOL]))
            if pname == "decompose":
                steps = [s for s in (pargs.get("steps") or []) if _norm(s) != _norm(node_goal)]
                if len(steps) >= 2:
                    if method_cache:
                        method_cache.remember(node_goal, steps)   # learn this decomposition
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
            if not steps or depth >= max_depth:
                if allow_decompose:
                    return _attempt(node_goal, depth, path, False, failed)
                return _node(node_goal, "blocked", reason="no_progress")
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
                    return _attempt(node_goal, depth, path, False, failed)
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
                findings.record(fact, _extract_value(result, findings_schema[name]), source=name)
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
                             result=result, reason=f"criterion_unmet:{crit}", **_cp)

        # Honesty rule (foreign-command grounding): an OPAQUE command with no declared
        # post-condition can't be trusted on its exit flag — surface it as UNVERIFIABLE,
        # never silently `done`. It books no reward until a criterion or probe confirms
        # the effect. (If the contract DID declare a criterion, the block above verified
        # it, so criterion_of(name) is truthy here and this doesn't fire.)
        if ok and name in _OPAQUE_TOOLS and not (criterion_of and criterion_of(name)):
            ledger.append({"goal": node_goal, "tool": name, "args": args,
                           "ok": False, "verified": False, "result": result})
            return _node(node_goal, "unverified", tool=name, args=args,
                         result=result, reason="unverifiable", **_cp)

        ledger.append({"goal": node_goal, "tool": name, "args": args, "ok": ok, "result": result})
        return _node(node_goal, "done" if ok else "failed", tool=name, args=args, result=result, **_cp)

    root = _resolve(goal, 0, [])
    return {"root": root, "ledger": ledger, "ok": root.get("status") == "done"}

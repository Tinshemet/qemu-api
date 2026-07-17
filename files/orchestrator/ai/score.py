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
    build_context:  Optional[Callable[[str, List[str]], str]] = None,
    is_destructive: Optional[Callable[[str, Dict], bool]] = None,
    confirm:        Optional[Callable[[str, Dict], bool]] = None,
    select_tools:   Optional[Callable[[str, List[Dict]], List[Dict]]] = None,
    max_depth:      int = 3,
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
    max_depth                   -> recursion bound (a node deeper than this is
                                   marked blocked rather than decomposed further).

    Returns {"root": <node>, "ledger": [<executed leaf records>], "ok": bool}.
    A node's status is one of: done / failed / partial / blocked / skipped / no_action.
    """
    ledger: List[Dict[str, Any]] = []

    def _resolve(node_goal: str, depth: int, path: List[str], allow_decompose: bool = True) -> Dict[str, Any]:
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
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": f"Goal: {node_goal}"}]
        base = select_tools(node_goal, tools) if select_tools else list(tools)
        offered = base + ([DECOMPOSE_TOOL] if allow_decompose else [])
        name, args = _first_tool_call(call_model(messages, offered))

        if name is None:
            return _node(node_goal, "no_action")

        if name == "decompose":
            # Drop non-progressing steps — the weak model often "decomposes" an atomic
            # goal into itself. If nothing progresses (or we're too deep), re-ask WITHOUT
            # the decompose option so the model MUST pick a primitive (the progress guard).
            steps = [s for s in (args.get("steps") or []) if _norm(s) != _norm(node_goal)]
            if not steps or depth >= max_depth:
                if allow_decompose:
                    return _resolve(node_goal, depth, path, allow_decompose=False)
                return _node(node_goal, "blocked", reason="no_progress")
            children = [_resolve(s, depth + 1, path + [node_goal]) for s in steps]
            done = all(c.get("status") == "done" for c in children)
            return _node(node_goal, "done" if done else "partial", children=children)

        # A primitive → a leaf.
        if is_destructive and is_destructive(name, args) and confirm and not confirm(name, args):
            return _node(node_goal, "skipped", tool=name, args=args)
        result = execute(name, args)
        ok = not (isinstance(result, dict) and (result.get("success") is False or result.get("error")))
        ledger.append({"goal": node_goal, "tool": name, "args": args, "ok": ok, "result": result})
        return _node(node_goal, "done" if ok else "failed", tool=name, args=args, result=result)

    root = _resolve(goal, 0, [])
    return {"root": root, "ledger": ledger, "ok": root.get("status") == "done"}

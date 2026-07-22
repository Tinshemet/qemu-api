"""
ledger_util.py — stateless helpers for the Score: node factory, goal normaliser,
ledger carry-forward, ledger-aware attach steering, and tool-call extraction.

These are the genuinely top-level (state-free) helpers the recursive engine calls;
they hold no engine state, so they live outside the closure in engine_core.
"""

from typing import Any, Dict, List

from ._deps import _POST_CREATE_ATTACH, _NARROW_CORE_TOOLS


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

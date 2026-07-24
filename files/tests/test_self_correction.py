#!/usr/bin/env python3
"""
test_self_correction.py — plan-level revision (self-correction).

Proves the corrigibility-spine addition: an AND plan left `partial` (a REQUIRED step
failed for good) is not a dead branch — the tree RE-PLANS the goal, feeding the model a
post-mortem of which steps failed so it produces the CORRECTIVE remainder, not the same
decomposition. Distinct from leaf backtrack (same sub-goal, new approach). Bounded by
max_revisions; re-attempts skip the method cache (the root-replan landmine); off unless
max_revisions > 0.

Run:  PYTHONPATH=files python3 files/tests/test_self_correction.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.planner.engine import Engine
from orchestrator.ai.planner.score import run_score
from orchestrator.ai.planner.method_cache import MethodCache

_PASS = 0
_FAIL = 0


def check(label, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ok   {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}")


_TOOLS = [{"type": "function", "function": {"name": n, "parameters": {}}}
          for n in ("create_vm", "configure_vm", "configure_fallback")]
_NO_LEGAL = lambda *a: False


class World:
    """create_vm is idempotent (re-running a done step is harmless); configure_vm always
    fails (a broken approach); configure_fallback is the corrective step that works."""
    def __init__(self):
        self.vms = {}
        self.configured = set()

    def execute(self, tool, args):
        n = args.get("name")
        if tool == "create_vm":
            self.vms[n] = {"status": "stopped"}
            return {"success": True}
        if tool == "configure_vm":
            return {"success": False, "error": "driver missing"}
        if tool == "configure_fallback":
            self.configured.add(n)
            return {"success": True}
        return {"success": True}


def _dec(steps):
    return {"message": {"tool_calls": [{"function": {"name": "decompose", "arguments": {"steps": steps}}}]}}


def _tc(name, args):
    return {"message": {"tool_calls": [{"function": {"name": name, "arguments": args}}]}}


def _goal(m):
    return next((x["content"][6:] for x in m if x["role"] == "user" and x["content"].startswith("Goal: ")), "")


def _sys(m):
    return next((x["content"] for x in m if x["role"] == "system"), "")


def _leaf_model(m, tools):
    """Shared leaf routing for the sub-goals (the whole test uses these primitives)."""
    g = _goal(m)
    if g == "create web":                 return _tc("create_vm", {"name": "web"})
    if g == "configure web":              return _tc("configure_vm", {"name": "web"})
    if g == "configure web via fallback": return _tc("configure_fallback", {"name": "web"})
    return {"message": {"tool_calls": []}}


def main():
    print("AND partial → RE-PLAN the corrective remainder → done (revised)")
    w = World()
    def model(m, tools):
        g = _goal(m)
        if g == "set up web":
            # On revision the post-mortem ("→ partial") is in the prompt → switch to the
            # working plan; the first, naive plan uses the broken configure_vm.
            if "→ partial" in _sys(m):
                return _dec(["create web", "configure web via fallback"])
            return _dec(["create web", "configure web"])
        return _leaf_model(m, tools)
    r = run_score("set up web", call_model=model, execute=w.execute, tools=_TOOLS,
                  engine=Engine(legal_filter=_NO_LEGAL, max_revisions=1), max_retries=0)
    check("root recovered to done via revision", r["root"]["status"] == "done" and r["ok"] is True)
    check("root flagged revised (1 revision)", r["root"].get("revised") is True and r["root"].get("revisions") == 1)
    check("the corrective step actually ran (world changed)", "web" in w.configured)

    print("\nrevision is BOUNDED — a persistently-broken plan stays partial after the budget")
    w = World()
    def stuck(m, tools):
        g = _goal(m)
        if g == "set up web":
            return _dec(["create web", "configure web"])   # never switches — always the broken plan
        return _leaf_model(m, tools)
    r = run_score("set up web", call_model=stuck, execute=w.execute, tools=_TOOLS,
                  engine=Engine(legal_filter=_NO_LEGAL, max_revisions=2), max_retries=0)
    check("still partial after exhausting revisions", r["root"]["status"] == "partial")
    check("used the full revision budget", r["root"].get("revisions") == 2)
    check("not falsely marked revised", "revised" not in r["root"])

    print("\noff by default — max_revisions=0 leaves a partial partial (backward compatible)")
    w = World()
    r = run_score("set up web", call_model=stuck, execute=w.execute, tools=_TOOLS,
                  engine=Engine(legal_filter=_NO_LEGAL), max_retries=0)
    check("partial, and no revision attempted", r["root"]["status"] == "partial" and "revisions" not in r["root"])

    print("\nlandmine fixed: a re-plan SKIPS the cached (failing) decomposition")
    w = World()
    mc = MethodCache()
    mc.remember("set up web", ["create web", "configure web"])   # a cached plan that fails
    def cache_model(m, tools):
        g = _goal(m)
        if g == "set up web":
            # If the cache were re-used on revision this never runs; reaching the model
            # with the post-mortem is how the corrective plan gets chosen.
            if "→ partial" in _sys(m):
                return _dec(["create web", "configure web via fallback"])
            return _dec(["create web", "configure web"])
        return _leaf_model(m, tools)
    r = run_score("set up web", call_model=cache_model, execute=w.execute, tools=_TOOLS,
                  engine=Engine(legal_filter=_NO_LEGAL, method_cache=mc, decompose_first=True,
                                max_revisions=1), max_retries=0)
    check("first plan came from the cache (deterministic)", any(
        c.get("goal") == "create web" for c in r["root"].get("children", [])))
    check("revision reached the model despite the cache → recovered", r["root"]["status"] == "done"
          and r["root"].get("revised") is True and "web" in w.configured)

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
test_root_predicate.py — the CONTRACT ROOT PREDICATE (gauntlet E, acceptance).

Proves the reward-hacking-by-bad-plan gate: all-children-done is NECESSARY but, at
the ROOT, not SUFFICIENT. A plan whose steps each 'succeed' but do not COMPOSE (a
later step undid an earlier one, or a step was simply omitted) is `unverified`, NOT
done — so it books no reward. The predicate comes from the CONTRACT and is checked
against ground truth, and it's gated to the root (intermediate composites stand).

Run:  PYTHONPATH=files python3 files/tests/test_root_predicate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.planner.engine import Engine
from orchestrator.ai.planner.score import run_score
from orchestrator.ai.planner.autonomous import make_goal_verifier
import orchestrator.ai.agent.contract as C

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


_TOOLS = [{"type": "function", "function": {"name": "create_vm", "parameters": {}}}]


def _dc(steps):
    return {"message": {"tool_calls": [{"function": {"name": "decompose", "arguments": {"steps": steps}}}]}}


def _tc(name, args):
    return {"message": {"tool_calls": [{"function": {"name": name, "arguments": args}}]}}


def _goal_of(m):
    return next((x["content"][6:] for x in m if x["role"] == "user" and x["content"].startswith("Goal: ")), "")


# A two-level plan: root → [set up web, set up db]; "set up web" → [create web1, create web2].
# Every leaf create_vm 'succeeds', so all-children-done holds at every composite.
def _model(m, t):
    goal = _goal_of(m)
    if goal == "build lab":
        return _dc(["set up web", "set up db"])
    if goal == "set up web":
        return _dc(["create web1", "create web2"])
    return _tc("create_vm", {"name": goal.split()[-1]})


def _run(verify_goal):
    return run_score("build lab", call_model=_model,
                     execute=lambda t, a: {"success": True}, tools=_TOOLS,
                     engine=Engine(verify_goal=verify_goal))


def main():
    print("a clean-but-WRONG plan books no reward")
    r = _run(lambda g, kids, led: False)          # every step 'succeeded'; the GOAL does not hold
    check("root is unverified, not done", r["root"]["status"] == "unverified")
    check("reason is goal_predicate_unmet", r["root"].get("reason") == "goal_predicate_unmet")
    check("run is not ok (no reward)", r["ok"] is False)
    # An unverified root is a SOFT failure → it backtracks: the gate doesn't just
    # reject a bad plan, it drives a retry (3 leaves × 3 attempts = 9 executions).
    check("the rejected plan backtracked (retried)", r["root"].get("retries") == 2)
    check("every attempt's steps ran", len(r["ledger"]) == 9)

    print("\nthe predicate is gated to the ROOT — intermediate composites stand")
    kids = {c["goal"]: c for c in r["root"]["children"]}
    check("the depth-1 composite is done (not gated)", kids["set up web"]["status"] == "done")
    check("its children both done", all(c["status"] == "done" for c in kids["set up web"]["children"]))

    print("\na plan whose goal DOES hold is accepted")
    r = _run(lambda g, kids, led: True)
    check("root is done", r["root"]["status"] == "done" and r["ok"] is True)

    print("\nno predicate (None) → behaviour unchanged")
    r = _run(lambda g, kids, led: None)
    check("root is done when the predicate has no opinion", r["root"]["status"] == "done")
    r = _run(None)
    check("root is done when no verify_goal is wired at all", r["root"]["status"] == "done")

    print("\nthe verifier is the arg to the predicate: goal-string, children, ledger")
    seen = []
    _run(lambda g, kids, led: seen.append((g, len(kids))) or True)
    check("called once, at the root, with the root goal + its children", seen == [("build lab", 2)])

    print("\ncontract.goal_predicate wires the state check (make_goal_verifier)")
    check("Doorman has no structured predicate → None", C.goal_predicate() is None)
    vg = make_goal_verifier(lambda: {})
    check("no predicate → verifier stays silent (None, never blocks)", vg("g", [], []) is None)

    orig = C.goal_predicate
    try:
        C.goal_predicate = lambda: [{"criterion": "present", "target": "honeypot"},
                                    {"criterion": "absent", "target": "web01"}]
        vg = make_goal_verifier(lambda: {"honeypot": {"status": "running"}})
        check("True when every clause holds against live state", vg("g", [], []) is True)
        vg = make_goal_verifier(lambda: {"honeypot": {"status": "running"}, "web01": {"status": "stopped"}})
        check("False when a clause fails (web01 should be absent)", vg("g", [], []) is False)
    finally:
        C.goal_predicate = orig

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

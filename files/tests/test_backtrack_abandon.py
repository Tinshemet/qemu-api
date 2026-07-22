#!/usr/bin/env python3
"""
test_backtrack_abandon.py — CE-based backtrack-abandon (gauntlet F).

Backtrack no longer retries a soft-failed goal blindly to the budget: it ABANDONS as
soon as a fresh attempt is worth no more than the opportunity cost. Continue-value =
estimate(goal) − H·(retries so far); floor = max(0, best_alt). Proves: a hopeless goal
(CE ≤ 0) is dropped with NO retries; a worth-it goal keeps its full budget; the holding
cost H gives graduated abandonment; an OR parent's next-best alternative raises the
floor so a failing alternative is abandoned in favour of the better sibling; and with no
estimator the plain max_retries budget stands (backward compatible).

Run:  PYTHONPATH=files python3 files/tests/test_backtrack_abandon.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.planner.engine import Engine
from orchestrator.ai.planner.score import run_score

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
          for n in ("stop_vm", "delete_vm")]
_NO_LEGAL = lambda *a: False


def _tc(name, args):
    return {"message": {"tool_calls": [{"function": {"name": name, "arguments": args}}]}}


def _alt(options):
    return {"message": {"tool_calls": [{"function": {"name": "alternatives", "arguments": {"options": options}}}]}}


def _goal_of(m):
    return next((x["content"][6:] for x in m if x["role"] == "user" and x["content"].startswith("Goal: ")), "")


# The goal is one primitive that always fails, so it's a pure backtrack scenario.
def _fail_model(m, t):
    return _tc("stop_vm", {"name": "x"})


def _run(model, execute, *, mr=3, **engine_kw):
    return run_score("do the thing", call_model=model, execute=execute, tools=_TOOLS,
                     max_retries=mr, engine=Engine(legal_filter=_NO_LEGAL, **engine_kw))


def main():
    print("a hopeless goal (CE ≤ 0) is abandoned with NO retries")
    calls = []
    r = _run(_fail_model, lambda t, a: (calls.append(t) or {"success": False}),
             estimate=lambda g, d: -1.0)
    check("node abandoned", r["root"].get("abandoned") is True)
    check("no retries were spent", r["root"].get("retries") is None)
    check("executed exactly once (the first attempt)", len(calls) == 1)
    check("abandon record shows continue_ce ≤ floor",
          r["root"]["abandon"]["continue_ce"] <= r["root"]["abandon"]["floor"])

    print("\na worth-it goal keeps its full retry budget (penalty 0)")
    calls = []
    r = _run(_fail_model, lambda t, a: (calls.append(t) or {"success": False}),
             estimate=lambda g, d: 5.0, retry_penalty=0.0, mr=3)
    check("not abandoned", r["root"].get("abandoned") is None)
    check("used all 3 retries", r["root"].get("retries") == 3)
    check("executed 1 + 3 times", len(calls) == 4)

    print("\nthe holding cost gives GRADUATED abandonment")
    # estimate 3.0, penalty 2.0: cont = 3, 1, -1 at tries 0,1,2 → abandon at try 2.
    calls = []
    r = _run(_fail_model, lambda t, a: (calls.append(t) or {"success": False}),
             estimate=lambda g, d: 3.0, retry_penalty=2.0, mr=9)
    check("abandoned before the budget", r["root"].get("abandoned") is True)
    check("spent exactly 2 retries (cont fell below 0 on the 3rd)", r["root"].get("retries") == 2)
    check("executed 1 + 2 times", len(calls) == 3)

    print("\nno estimator → the plain max_retries budget stands (backward compatible)")
    calls = []
    r = _run(_fail_model, lambda t, a: (calls.append(t) or {"success": False}), mr=2)
    check("not abandoned (no CE gate)", r["root"].get("abandoned") is None)
    check("used the full budget", r["root"].get("retries") == 2 and len(calls) == 3)

    print("\nan OR parent's next-best alternative raises the abandon floor")
    # Try route A (CE 2.0) first; it fails. Its retries are cut short because route B
    # (CE 1.5) is waiting — floor = 1.5, penalty 1.0: cont = 2.0, 1.0 → abandon at try 1.
    # Then B is tried and succeeds.
    def or_model(m, t):
        goal = _goal_of(m)
        if goal == "do the thing":
            return _alt(["route A", "route B"])
        return _tc("stop_vm", {"name": goal.split()[-1]})
    est = lambda g, d: {"route A": 2.0, "route B": 1.5}[g]
    calls = []
    r = _run(or_model, lambda t, a: (calls.append((t, a["name"])) or {"success": a["name"] != "A"}),
             estimate=est, ce_floor=0.0, retry_penalty=1.0, mr=9)
    check("root done via the second alternative", r["root"]["status"] == "done")
    kids = {c["goal"]: c for c in r["root"]["children"]}
    check("route A was abandoned early (1 retry, not 9)",
          kids["route A"].get("abandoned") is True and kids["route A"].get("retries") == 1)
    check("route A's abandon floor is the sibling's CE (1.5)", kids["route A"]["abandon"]["floor"] == 1.5)
    check("route B ran and won", kids["route B"]["status"] == "done")
    check("A tried twice then B once", calls == [("stop_vm", "A"), ("stop_vm", "A"), ("stop_vm", "B")])

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

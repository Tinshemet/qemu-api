#!/usr/bin/env python3
"""
test_watchdog.py — the farming/loop watchdog (reward-cost step 4).

Proves the load-bearing property: it throttles zero-progress REPETITION of the same
signature, but NOT legit bulk work (distinct signatures) or progress-making repeats
(new findings). And it's reversible.

Run:  PYTHONPATH=files python3 files/tests/test_watchdog.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.planner.watchdog import Watchdog
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


def main():
    print("throttles zero-progress repetition of the SAME signature")
    w = Watchdog(max_repeats=2)
    a = {"name": "web"}
    w.observe("stop_vm", a)   # 1st: new signature = progress
    check("first run not throttled", not w.throttled("stop_vm", a))
    w.observe("stop_vm", a)   # repeat, no progress -> 1
    w.observe("stop_vm", a)   # repeat, no progress -> 2 >= max -> THROTTLE
    check("throttled after max_repeats no-progress repeats", w.throttled("stop_vm", a))
    check("an alert was raised", len(w.alerts) == 1 and "no progress" in w.alerts[0])

    print("\nNOT raw frequency: bulk work with distinct signatures is fine")
    w = Watchdog(max_repeats=2)
    for n in ("a", "b", "c", "d", "e"):
        w.observe("create_vm", {"name": n})   # every one a new signature = progress
    check("50-VM-style bulk never throttled", not any(w.throttled("create_vm", {"name": n}) for n in "abcde"))

    print("\nrepeats that MAKE PROGRESS (new findings) are not throttled")
    w = Watchdog(max_repeats=2)
    for _ in range(5):
        w.observe("scan_network", {"net": "lab"}, new_finding=True)   # each scan finds new hosts
    check("progress-making repeats survive", not w.throttled("scan_network", {"net": "lab"}))

    print("\nreversible: a throttle can be lifted")
    w = Watchdog(max_repeats=2)
    w.observe("stop_vm", a); w.observe("stop_vm", a); w.observe("stop_vm", a)
    check("throttled", w.throttled("stop_vm", a))
    w.reset("stop_vm", a)
    check("reset lifts it", not w.throttled("stop_vm", a))

    print("\nin the planner: a throttled signature is blocked, not run")
    w = Watchdog(max_repeats=2)
    w.observe("stop_vm", a); w.observe("stop_vm", a); w.observe("stop_vm", a)   # pre-throttle it
    calls = []
    def model(m, t):
        return {"message": {"tool_calls": [{"function": {"name": "stop_vm", "arguments": {"name": "web"}}}]}}
    r = run_score("stop web", call_model=model, execute=lambda t, ar: (calls.append(t) or {"success": True}),
                  tools=[{"type": "function", "function": {"name": "stop_vm", "parameters": {}}}],
                  watchdog=w, max_retries=0)
    check("throttled leaf blocked", r["root"].get("reason") == "watchdog_throttle" and calls == [])

    print("\nresult-change is progress: a re-read of MOVING state is not a loop")
    w = Watchdog(max_repeats=2)
    s = {"name": "web"}
    w.observe("vm_status", s, result={"cpu": 10})
    w.observe("vm_status", s, result={"cpu": 20})   # result changed -> progress
    w.observe("vm_status", s, result={"cpu": 30})   # changed -> progress
    check("changing results never throttle", not w.throttled("vm_status", s))
    w.observe("vm_status", s, result={"cpu": 30})   # same -> 1
    w.observe("vm_status", s, result={"cpu": 30})   # same -> 2 -> throttle
    check("stuck on the SAME result does throttle", w.throttled("vm_status", s))

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

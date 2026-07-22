#!/usr/bin/env python3
"""
test_epistemic_acceptance.py — connectivity ("all ping each other") is VERIFIED.

The benchmark exposed a false positive: a goal like "make sure they all ping each other"
was marked done whenever a ping-shaped tool returned success, even if the tool's OWN
report said nobody could reach anybody. Connectivity is an EPISTEMIC result (a finding),
not owned state — so acceptance must read the recorded ping RESULT, not the tool's
success flag. This proves: the ping result is recorded (with a `when` guard so fleet
create/add don't poison it), and the contract root predicate's `mesh` clause accepts
only when that finding is truthy.

Run:  PYTHONPATH=files python3 files/tests/test_epistemic_acceptance.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.planner.findings import Findings, yield_fact, DEFAULT_SCHEMA
from orchestrator.ai.planner.autonomous import make_goal_verifier, run_autonomous
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


def main():
    print("the ping result is recorded — and the `when` guard keeps fleet create/add out")
    check("fleet PING yields mesh(lab)", yield_fact("fleet", {"label": "lab", "action": "ping"}, DEFAULT_SCHEMA) == "mesh(lab)")
    check("fleet CREATE yields nothing (when-guarded)", yield_fact("fleet", {"label": "lab", "action": "create"}, DEFAULT_SCHEMA) is None)
    check("guest_ping yields reachable(vm1)", yield_fact("guest_ping", {"name": "vm1"}, DEFAULT_SCHEMA) == "reachable(vm1)")

    print("\nthe mesh clause accepts only when the finding is truthy")
    fnd = Findings()
    vg = make_goal_verifier(lambda: {}, fnd)
    orig = C.goal_predicate
    try:
        C.goal_predicate = lambda: [{"criterion": "mesh", "target": "lab"}]
        check("no ping finding yet → not accepted", vg("g", [], []) is False)
        fnd.record("mesh(lab)", False)
        check("mesh recorded FALSE → not accepted", vg("g", [], []) is False)
        fnd.record("mesh(lab)", True)
        check("mesh recorded TRUE → accepted", vg("g", [], []) is True)
    finally:
        C.goal_predicate = orig

    print("\nend-to-end: a broken mesh is REJECTED, a real mesh is accepted")

    # Every leaf verifies fine in BOTH worlds (all vms created + running); only the ping
    # RESULT differs. This isolates the ROOT PREDICATE's mesh clause — the break isn't a
    # VM-state problem a leaf criterion could catch, so only epistemic acceptance can.
    class World:
        def __init__(self, broken): self.vms, self.broken = {}, broken
        def execute(self, tool, a):
            if tool == "create_vm": self.vms[a["name"]] = "stopped"
            elif tool == "launch_vm": self.vms[a["name"]] = "running"
            elif tool == "fleet" and a.get("action") == "ping":
                return {"success": True, "all_reachable": not self.broken}
            return {"success": True}
        def getter(self): return {n: {"status": s} for n, s in self.vms.items()}

    def model(msgs, tools):
        g = next((m["content"][6:] for m in msgs if m["role"] == "user" and m["content"].startswith("Goal: ")), "").lower()
        def call(n, ar): return {"message": {"tool_calls": [{"function": {"name": n, "arguments": ar}}]}}
        def dec(s): return {"message": {"tool_calls": [{"function": {"name": "decompose", "arguments": {"steps": s}}}]}}
        if "ping" in g and "build" in g:
            return dec(["create vm a", "create vm b", "launch a", "launch b", "make the fleet", "ping the fleet"])
        if g.startswith("create vm "): return call("create_vm", {"name": g.split()[-1]})
        if g.startswith("launch "):    return call("launch_vm", {"name": g.split()[-1]})
        if "make the fleet" in g:       return call("fleet", {"label": "lab", "action": "create"})
        if "ping the fleet" in g:       return call("fleet", {"label": "lab", "action": "ping"})
        return {"message": {"tool_calls": []}}

    tools = [{"type": "function", "function": {"name": n, "parameters": {}}}
             for n in ("create_vm", "launch_vm", "fleet")]

    orig = C.goal_predicate
    try:
        C.goal_predicate = lambda: [{"criterion": "mesh", "target": "lab"}]
        good = World(broken=False)
        r = run_autonomous("build a lab and make them ping", call_model=model, execute=good.execute,
                           tools=tools, vms_getter=good.getter, max_retries=0, max_depth=2)
        check("real mesh → root done, ok", r["root"]["status"] == "done" and r["ok"] and r["findings"]["mesh(lab)"] is True)

        bad = World(broken=True)
        r = run_autonomous("build a lab and make them ping", call_model=model, execute=bad.execute,
                           tools=tools, vms_getter=bad.getter, max_retries=0, max_depth=2)
        check("broken mesh → root unverified, NOT ok",
              r["root"]["status"] == "unverified" and not r["ok"] and r["root"]["reason"] == "goal_predicate_unmet")
        check("the ping result was recorded as False (not trusted from success flag)", r["findings"]["mesh(lab)"] is False)
    finally:
        C.goal_predicate = orig

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

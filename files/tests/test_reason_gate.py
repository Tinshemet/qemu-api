#!/usr/bin/env python3
"""
test_reason_gate.py — the reason-validation gate (D1, two-stage, opt-in).

Proves: an action whose STATED reason doesn't match what it does is blocked before it
runs; a matching reason passes; and the check is STRUCTURAL (target-in-reason), never a
weak-model self-grading. Covers the engine wiring (a stub reason_gate blocks a leaf) and
the driver's make_reason_gate (elicit a one-sentence reason → check the target appears).

Run:  PYTHONPATH=files python3 files/tests/test_reason_gate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.planner.engine import Engine
from orchestrator.ai.planner.score import run_score
from orchestrator.ai.planner.autonomous import make_reason_gate

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
          for n in ("launch_vm", "delete_vm")]
_NO_LEGAL = lambda *a: False


def _tc(name, args):
    return {"message": {"tool_calls": [{"function": {"name": name, "arguments": args}}]}}


def _txt(s):
    return {"message": {"content": s, "tool_calls": []}}


def main():
    print("engine wiring: a reason_gate problem blocks the leaf before it runs")
    calls = []
    model = lambda m, t: _tc("launch_vm", {"name": "web"})
    # gate flags this action → engine must block it, nothing executes
    r = run_score("start web", call_model=model, execute=lambda t, a: calls.append(t) or {"success": True},
                  tools=_TOOLS, engine=Engine(legal_filter=_NO_LEGAL,
                                              reason_gate=lambda g, n, a: "target_absent"),
                  max_retries=0)
    check("leaf blocked with a reason_mismatch tag",
          r["root"]["status"] == "blocked" and r["root"]["reason"] == "reason_mismatch:target_absent")
    check("nothing executed past the gate", calls == [])

    print("\nengine wiring: a passing gate lets the leaf run AND records the rationale")
    calls = []
    r = run_score("start web", call_model=model, execute=lambda t, a: calls.append(t) or {"success": True},
                  tools=_TOOLS, engine=Engine(legal_filter=_NO_LEGAL,
                                              reason_gate=lambda g, n, a: {"reason": "web serves the goal", "problem": None}),
                  max_retries=0)
    check("leaf ran when the reason checks out", r["root"]["status"] == "done" and calls == ["launch_vm"])
    check("the stated reason is recorded on the leaf (rationale, mitigation B)",
          r["root"].get("rationale") == "web serves the goal")

    print("\nmake_reason_gate: STAGE 2a — the target must appear in the stated reason")
    good = make_reason_gate(lambda m, t: _txt("Launching web so the service is reachable."))
    g = good("start web", "launch_vm", {"name": "web"})
    check("reason names the target → passes (no problem)", g["problem"] is None)
    check("the stated reason is RETURNED for the record (mitigation B)",
          g["reason"] == "Launching web so the service is reachable.")
    bad = make_reason_gate(lambda m, t: _txt("Launching db to warm the cache."))
    check("acting on web but the reason only mentions db → target_absent",
          bad("start web", "launch_vm", {"name": "web"})["problem"] == "target_absent")

    print("\nmake_reason_gate: the target can live in vm_name (add_vm_to_network schema)")
    ok = make_reason_gate(lambda m, t: _txt("Attaching web to the isolated network."))
    check("vm_name target found in the reason → passes",
          ok("wire up web", "add_vm_to_network", {"vm_name": "web", "net_name": "iso"})["problem"] is None)
    miss = make_reason_gate(lambda m, t: _txt("Attaching db to the network."))
    check("vm_name target absent from the reason → target_absent",
          miss("wire up web", "add_vm_to_network", {"vm_name": "web", "net_name": "iso"})["problem"] == "target_absent")

    print("\nmake_reason_gate: STAGE 1 — an unjustifiable action (no reason) is flagged")
    empty = make_reason_gate(lambda m, t: _txt(""))
    check("no reason → no_reason", empty("wipe web", "delete_vm", {"name": "web"})["problem"] == "no_reason")

    print("\nmake_reason_gate: no target in args → nothing to contradict (passes on a reason)")
    notarget = make_reason_gate(lambda m, t: _txt("Scanning the range to map hosts."))
    check("targetless action with a reason passes", notarget("map the range", "scan_network", {})["problem"] is None)

    print("\nmake_reason_gate: STAGE 2b — a reason that CONTRADICTS the live state is caught (mitigation A)")
    state = lambda: {"web": {"status": "stopped"}}          # ground truth: web is STOPPED
    # the model justifies a delete with a FALSE premise ("web is running")
    liar = make_reason_gate(lambda m, t: _txt("Deleting web because web is running and wasteful."), state_getter=state)
    check("reason 'web is running' vs actual stopped → reason_contradicts_state",
          liar("free resources", "delete_vm", {"name": "web"})["problem"] == "reason_contradicts_state")
    # a TRUE premise about state passes
    honest = make_reason_gate(lambda m, t: _txt("Launching web because web is stopped and the goal needs it up."),
                              state_getter=state)
    check("reason 'web is stopped' matches reality → passes",
          honest("start web", "launch_vm", {"name": "web"})["problem"] is None)
    # a DESIRED-outcome phrasing must NOT be mistaken for a present-state claim (no false flag)
    desire = make_reason_gate(lambda m, t: _txt("Launching web to make web running for the demo."), state_getter=state)
    check("desired outcome ('to make web running') is not a false-state flag",
          desire("start web", "launch_vm", {"name": "web"})["problem"] is None)

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

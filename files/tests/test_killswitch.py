#!/usr/bin/env python3
"""
test_killswitch.py — the safeword kill-switch (Phase 1, infrastructural abort).

Proves: the safeword trips ONLY on a match (the harness compares, not the model); an
out-of-band abort needs no word; a tripped switch stops the tree with NO further
execution while PRESERVING the ledger; and the contract's safeword arms it.

Run:  PYTHONPATH=files python3 files/tests/test_killswitch.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.planner.killswitch import KillSwitch
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
          for n in ("create_vm", "stop_vm")]


def main():
    print("the switch itself")
    ks = KillSwitch("Banana")
    check("armed when a safeword is set", ks.armed and not ks.tripped)
    check("a WRONG word does not trip", ks.safeword("apple") is False and not ks.tripped)
    check("the safeword trips (case-insensitive, trimmed)", ks.safeword("  banana ") is True and ks.tripped)
    check("reason is 'safeword'", ks.reason == "safeword")
    ks.reset()
    check("reset clears it", not ks.tripped)
    ks.abort("signal")
    check("out-of-band abort needs no word", ks.tripped and ks.reason == "signal")
    check("an unarmed switch never trips on a word", KillSwitch().safeword("anything") is False)

    print("\nharness: a PRE-tripped switch halts before any execution")
    ks = KillSwitch("stop"); ks.safeword("stop")
    calls = []
    r = run_score("do a thing", call_model=lambda m, t: {"message": {"tool_calls": [{"function": {"name": "create_vm", "arguments": {"name": "x"}}}]}},
                  execute=lambda t, a: (calls.append(t) or {"success": True}), tools=_TOOLS,
                  engine=Engine(killswitch=ks))
    check("aborted node, nothing executed", r["root"]["status"] == "aborted" and calls == [])
    check("abort reason surfaced", r["root"].get("reason") == "safeword")

    print("\nharness: a MID-run trip stops further steps, preserves the ledger")
    ks = KillSwitch("halt")
    calls = []
    # step 'a' executes and (like an operator typing the safeword) trips the switch;
    # step 'b' must then be aborted, never executed.
    def model(m, t):
        goal = next((x["content"][6:] for x in m if x["role"] == "user" and x["content"].startswith("Goal: ")), "")
        if "wind down both" in goal:
            return {"message": {"tool_calls": [{"function": {"name": "decompose", "arguments": {"steps": ["stop a", "stop b"]}}}]}}
        tool = "stop_vm"
        return {"message": {"tool_calls": [{"function": {"name": tool, "arguments": {"name": goal[-1]}}}]}}
    def execute(t, a):
        calls.append((t, a["name"]))
        if a["name"] == "a":
            ks.safeword("halt")   # operator hits the safeword right after the first action
        return {"success": True}
    r = run_score("wind down both", call_model=model, execute=execute, tools=_TOOLS, engine=Engine(killswitch=ks))
    check("first step ran", ("stop_vm", "a") in calls)
    check("second step did NOT run (aborted)", ("stop_vm", "b") not in calls)
    kids = {c.get("goal"): c["status"] for c in r["root"].get("children", [])}
    check("the un-run step is 'aborted'", "aborted" in kids.values())
    check("ledger preserved what ran (suspend, not wipe)", len(r["ledger"]) == 1 and r["ledger"][0]["tool"] == "stop_vm")

    print("\ncontract arms it")
    from orchestrator.ai.agent import contract as C
    check("contract.safeword() exposes the campaign safeword (None for the Doorman)", C.safeword() is None)
    check("contract.deadman_timeout() is off by default (None for the Doorman)", C.deadman_timeout() is None)

    print("\ndead-man's switch: unattended silence trips it")
    import time
    from orchestrator.ai.planner.killswitch import DeadMansSwitch
    ks = KillSwitch()
    dm = DeadMansSwitch(ks, timeout=0.08).start()
    check("not tripped immediately", ks.tripped is False)
    time.sleep(0.20)                                   # no check-in for > timeout
    check("trips on silence with reason 'deadman'", ks.tripped is True and ks.reason == "deadman")
    dm.stop()

    print("\ncheck-ins keep it alive; stop() disarms")
    ks = KillSwitch()
    dm = DeadMansSwitch(ks, timeout=0.12).start()
    for _ in range(4):                                 # keep checking in inside the window
        time.sleep(0.05)
        ks.checkin()                                   # the harness signals a sign of life
    check("still alive while checking in", ks.tripped is False)
    dm.stop()
    time.sleep(0.20)                                   # past the timeout, but disarmed
    check("stop() prevents a later fire", ks.tripped is False)

    print("\ncheckin() on a bare kill-switch is a harmless no-op")
    ks = KillSwitch()
    ks.checkin()                                        # nothing observing → no error, no trip
    check("no observer → no-op", ks.tripped is False)

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

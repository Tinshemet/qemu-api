#!/usr/bin/env python3
"""
test_fleet.py — Fleet broadcast unit tests (no real VM required).

Exercises QemuManager.fleet(): label-based member selection, per-action routing,
{vm: result} aggregation, ok/failed counts, and partial-failure capture. The
selector (list_vms) and the per-member ops (run_guest_command / guest_ping /
vm_status / stop_vm / launch_vm) are stubbed, so the fleet orchestration logic is
tested in isolation without launching QEMU or a guest agent. Sibling of
test_guest_agent.py / test_serial_agent.py.

Run:  PYTHONPATH=files python3 files/tests/test_fleet.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from executor.api.qemu_manager import QemuManager


# ── harness ──────────────────────────────────────────────────────────────────
_PASS = 0
_FAIL = 0

def check(label, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  \033[32mok\033[0m   {label}")
    else:
        _FAIL += 1
        print(f"  \033[31mFAIL\033[0m {label}")


def make_manager(members):
    """A QemuManager with its selector + per-VM ops stubbed.

    members: {label: [vm names]} — list_vms(label) returns those names as rows.
    Returns (manager, calls) where calls is a list of (action, name, ...) tuples
    recording what the fleet dispatched, in order.
    """
    mgr   = QemuManager()
    calls = []

    def list_vms(label=None):
        return [{"name": n} for n in members.get(label, [])]

    def run_guest_command(name, command, args=None, timeout=None):
        calls.append(("exec", name, command))
        return {"success": True, "exit_code": 0, "stdout": f"out-{name}", "stderr": ""}

    mgr.list_vms          = list_vms
    mgr.run_guest_command = run_guest_command
    mgr.guest_ping = lambda name, timeout=5: (calls.append(("ping", name))   or {"success": True, "alive": True})
    mgr.vm_status  = lambda name:             (calls.append(("status", name)) or {"name": name, "state": "running"})
    mgr.stop_vm    = lambda name, force=False: (calls.append(("stop", name))   or {"success": True})
    mgr.launch_vm  = lambda name, **k:         (calls.append(("launch", name)) or {"success": True})
    return mgr, calls


def main():
    print("fleet — selection + exec happy path")
    mgr, calls = make_manager({"redteam": ["a", "b"]})
    r = mgr.fleet("redteam", "exec", command="whoami")
    check("exec dispatched to every member, in order",
          [c for c in calls if c[0] == "exec"] == [("exec", "a", "whoami"), ("exec", "b", "whoami")])
    check("count/ok/failed correct", r["count"] == 2 and r["ok"] == 2 and r["failed"] == 0)
    check("success True", r["success"] is True)
    check("results keyed by vm name", set(r["results"]) == {"a", "b"})
    check("aggregate carries label + action", r["label"] == "redteam" and r["action"] == "exec")

    print("\nfleet — empty selection")
    mgr, _ = make_manager({"redteam": ["a"]})
    r = mgr.fleet("ghost", "exec", command="x")
    check("no members → success False with message", r["success"] is False and "No VMs" in r["error"])
    check("no members → zero counts, empty results", r["count"] == 0 and r["results"] == {})

    print("\nfleet — argument validation")
    mgr, _ = make_manager({"redteam": ["a"]})
    r = mgr.fleet("redteam", "frobnicate")
    check("unknown action rejected", r["success"] is False and "Unknown fleet action" in r["error"])
    r = mgr.fleet("redteam", "exec")
    check("exec without command rejected", r["success"] is False and "requires a command" in r["error"])

    print("\nfleet — partial failure aggregation")
    mgr, _ = make_manager({"redteam": ["a", "b"]})
    def rgc(name, command, args=None, timeout=None):
        if name == "b":
            return {"success": False, "error": "VM 'b' is not running."}
        return {"success": True, "exit_code": 0, "stdout": "ok", "stderr": ""}
    mgr.run_guest_command = rgc
    r = mgr.fleet("redteam", "exec", command="whoami")
    check("partial: ok=1 failed=1", r["ok"] == 1 and r["failed"] == 1)
    check("partial: success True (>=1 ok)", r["success"] is True)
    check("partial: failing member captured verbatim", r["results"]["b"]["success"] is False)
    check("partial: whole broadcast not aborted", set(r["results"]) == {"a", "b"})

    print("\nfleet — all-fail selection")
    mgr, _ = make_manager({"redteam": ["a"]})
    mgr.run_guest_command = lambda *a, **k: {"success": False, "error": "down"}
    r = mgr.fleet("redteam", "exec", command="x")
    check("all members failed → success False", r["success"] is False and r["ok"] == 0 and r["failed"] == 1)

    print("\nfleet — status counts as ok despite no success key")
    mgr, calls = make_manager({"work": ["w1"]})
    r = mgr.fleet("work", "status")
    check("status member ok (plain status dict)", r["ok"] == 1 and r["failed"] == 0)
    check("status routed to vm_status", ("status", "w1") in calls)

    print("\nfleet — lifecycle + liveness routing")
    mgr, calls = make_manager({"g": ["x", "y"]})
    mgr.fleet("g", "ping")
    mgr.fleet("g", "stop")
    mgr.fleet("g", "launch")
    check("ping routed to guest_ping for each member", ("ping", "x") in calls and ("ping", "y") in calls)
    check("stop routed to stop_vm", ("stop", "x") in calls and ("stop", "y") in calls)
    check("launch routed to launch_vm", ("launch", "x") in calls and ("launch", "y") in calls)

    print("\nfleet — action normalized (trim + lowercase)")
    mgr, _ = make_manager({"g": ["x"]})
    r = mgr.fleet("g", "  STATUS ")
    check("action trimmed + lowercased", r["action"] == "status" and r["success"] is True)

    print("\ngate config — high-stakes fleet actions")
    from orchestrator.ai.chat_turn import _FLEET_CONFIRM_ACTIONS as _CT
    from orchestrator.ai.http_chat import _FLEET_CONFIRM_ACTIONS as _HC
    check("chat_turn confirms exec+stop only", _CT == {"exec", "stop"})
    check("http_chat matches chat_turn (no dual-path drift)", _HC == _CT)

    print(f"\n{'='*48}\n  {_PASS} passed, {_FAIL} failed\n{'='*48}")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

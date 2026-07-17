#!/usr/bin/env python3
"""
test_active_library.py — Active Library unit/integration tests (no real VM boot).

Creates a few throwaway VM configs on disk (distinct os_type / labels / flags),
builds the Library from the real manager, and asserts: the full snapshot, the
relation indices (fleets / by_os / template), case-insensitive resolve, the
compact ai_digest projection, and targeted per-entity updates via apply()
(reload / status-only / remove / read-only no-op). Cleans up after itself.

Run:  PYTHONPATH=files python3 files/tests/test_active_library.py
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from executor.api.qemu_config import MachineConfig
from shared.executioner.tool_executor import manager
from orchestrator.ai.active_library import ActiveLibrary

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


NAMES = ["alib-a", "alib-b", "alib-c"]

def _mk(name, **kw):
    shutil.rmtree(os.path.expanduser(f"~/.qemu_vms/{name}"), ignore_errors=True)
    MachineConfig(name=name, **kw).save()

def _rm(name):
    for lb in ("redteam", "vip"):
        try: manager.remove_label(name, lb)
        except Exception: pass
    shutil.rmtree(os.path.expanduser(f"~/.qemu_vms/{name}"), ignore_errors=True)


def main():
    for n in NAMES:
        _rm(n)
    _mk("alib-a", os_type="linux",   os_name="ubuntu",     labels=["redteam"])
    _mk("alib-b", os_type="linux",   os_name="kali",       labels=["redteam"], stealth=True)
    _mk("alib-c", os_type="windows", os_name="windows 11")

    lib = ActiveLibrary().snapshot(manager)

    print("snapshot — records")
    check("all three VMs tracked", set(NAMES) <= lib.known_names())
    ra = lib.resolve("alib-a")
    check("record carries os_type + os_name", ra and ra["os_type"] == "linux" and ra["os_name"] == "ubuntu")
    check("resolve is case-insensitive", lib.resolve("ALIB-A") is not None)
    check("windows VM os_type correct", lib.resolve("alib-c")["os_type"] == "windows")

    print("\nrelation indices")
    fleets = lib.fleets()
    check("redteam fleet = both linux VMs", fleets.get("redteam") == ["alib-a", "alib-b"])
    check("stealth flag indexes as a fleet", "alib-b" in fleets.get("stealth", []))
    check("hardened implied by stealth", "alib-b" in fleets.get("hardened", []))
    by_os = lib.by_os()
    check("by_os groups linux", set(by_os.get("linux", [])) >= {"alib-a", "alib-b"})
    check("by_os groups windows", "alib-c" in by_os.get("windows", []))

    print("\nai_digest projection")
    dg = lib.ai_digest()
    check("digest names the VMs", "alib-a" in dg and "alib-c" in dg)
    check("digest carries OS for reference resolution", "ubuntu" in dg and "windows" in dg)
    check("digest lists the fleet", "redteam" in dg)
    check("digest has the KNOWN VMS header", "KNOWN VMS" in dg)

    print("\ntargeted updates via apply()")
    # read-only tool → no update
    check("read-only tool is a no-op", lib.apply("list_vms", {}) is False)
    check("unknown tool is a no-op", lib.apply("frobnicate", {"name": "alib-a"}) is False)

    # add_label: mutate on disk, then targeted reload of just that VM
    manager.add_label("alib-c", "redteam")
    updated = lib.apply("add_label", {"name": "alib-c"})
    check("add_label triggers an update", updated is True)
    check("fleet index reflects the new member", "alib-c" in lib.fleets().get("redteam", []))
    check("other VMs' records untouched", lib.resolve("alib-a")["os_name"] == "ubuntu")

    # status-only update keeps the record, refreshes just status
    before = dict(lib.resolve("alib-a"))
    lib.apply("stop_vm", {"name": "alib-a"})
    after = lib.resolve("alib-a")
    check("stop_vm keeps the record", after is not None and after["os_name"] == before["os_name"])
    check("stop_vm status is a valid state", after["status"] in ("stopped", "running", "unknown"))

    # create_vm: new config on disk → targeted add
    _mk("alib-d", os_type="linux", os_name="fedora")
    lib.apply("create_vm", {"name": "alib-d"})
    check("create_vm adds the new VM", "alib-d" in lib.known_names())

    # delete_vm: targeted removal
    lib.apply("delete_vm", {"name": "alib-d"})
    check("delete_vm drops the VM", "alib-d" not in lib.known_names())

    print("\ntransaction / event log")
    tx = lib.transactions()
    check("every apply() logged a transaction", len(tx) >= 6)
    kinds = [e["tool"] for e in tx]
    check("read-only tools are logged too", "list_vms" in kinds)
    check("mutating tools are logged", "add_label" in kinds and "delete_vm" in kinds)
    lib.apply("stop_vm", {"name": "alib-a"}, result={"success": False, "error": "boom"})
    last = lib.transactions()[-1]
    check("result outcome recorded (ok False + error)", last["ok"] is False and last.get("error") == "boom")
    check("recent_transactions returns the tail", lib.recent_transactions(2)[-1] is last)
    check("digest surfaces RECENT ACTIONS", "RECENT ACTIONS" in lib.ai_digest())

    for n in NAMES + ["alib-d"]:
        _rm(n)
    print(f"\n{'='*48}\n  {_PASS} passed, {_FAIL} failed\n{'='*48}")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
test_findings_store.py — the per-agent claim store (durable claim persistence).

Covers the store that lets a human's confirm/reject decision OUTLIVE a run:
save/load roundtrip, confirm→verified, reject→gone, per-agent isolation, and the
merge rule that a human confirmation is never undone by a later pending re-claim.

Run:  PYTHONPATH=files python3 files/tests/test_findings_store.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai import findings_store as store
from orchestrator.ai.findings import Findings

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
    store._DIR = tempfile.mkdtemp()      # isolate from ~/.gorgon

    print("save / load roundtrip")
    f = Findings()
    f.record("phone(555)", "555", source="claim_finding", evidence="sig block")   # pending
    f.record("ip(web)", "1.2.3.4", source="get_vm_ip")                            # NOT persisted
    store.merge_into("doorman", f.persistable())
    data = store.load("doorman")
    check("only the claim persisted (probe fact dropped)", set(data) == {"phone(555)"})
    check("stored as pending with its evidence",
          data["phone(555)"]["status"] == "pending" and data["phone(555)"]["evidence"] == "sig block")

    print("\nlisting splits pending / verified")
    lst = store.listing("doorman")
    check("pending listed, verified empty", [e["fact"] for e in lst["pending"]] == ["phone(555)"] and lst["verified"] == [])

    print("\nconfirm → verified (and usable in the next run)")
    check("confirm returns True", store.confirm("doorman", "phone(555)") is True)
    lst2 = store.listing("doorman")
    check("moved to verified", lst2["pending"] == [] and [e["fact"] for e in lst2["verified"]] == ["phone(555)"])
    check("confirm again is False (not pending)", store.confirm("doorman", "phone(555)") is False)
    g = Findings(); g.merge(store.load("doorman"))
    check("a fresh run inherits it as USABLE", g.usable("phone(555)"))

    print("\nmerge never undoes a confirmation")
    store.merge_into("doorman", {"phone(555)": {"value": "555", "status": "pending", "evidence": "re-claim"}})
    check("a later pending re-claim does not downgrade the verified fact",
          store.load("doorman")["phone(555)"]["status"] == "verified")

    print("\nreject drops the claim")
    check("reject returns True", store.reject("doorman", "phone(555)") is True)
    check("gone from the store", store.load("doorman") == {})
    check("reject a missing fact is False", store.reject("doorman", "nope") is False)

    print("\nper-agent isolation")
    store.merge_into("doorman", {"user(bob)": {"value": "bob", "status": "verified"}})
    check("barenboim's store is empty (doorman's claim doesn't leak)", store.load("barenboim") == {})
    check("a path-traversal agent name is sanitized (stays under the store dir)",
          os.path.dirname(store.store_path("../../etc/passwd")) == store._DIR)

    print("\ntool-stats store: learned p_world survives restarts")
    check("empty store → {}", store.load_tool_counts("barenboim") == {})
    store.merge_tool_counts("barenboim", {"scan": {"ok": 3, "n": 4}})
    store.merge_tool_counts("barenboim", {"scan": {"ok": 1, "n": 2}, "exec": {"ok": 0, "n": 1}})
    tc = store.load_tool_counts("barenboim")
    check("counts ACCUMULATE across runs (add ok/n)", tc["scan"] == {"ok": 4, "n": 6})
    check("a new tool is added on merge", tc["exec"] == {"ok": 0, "n": 1})
    check("tool stats don't leak into the claim store", store.load("barenboim") == {})
    check("tool stats are per-agent isolated", store.load_tool_counts("doorman") == {})
    check("a path-traversal agent name is sanitized",
          os.path.dirname(store.tool_stats_path("../../etc/passwd")) == store._DIR)
    # the round-trip a fresh process does: load → learn p_world
    from orchestrator.ai.reward_cost import p_world_estimate
    pw = p_world_estimate(store.load_tool_counts("barenboim"))
    check("reloaded counts feed learned p_world", 0.0 < pw["scan"] < 1.0)
    check("clear wipes the learned tallies (stale after a range change)",
          store.clear_tool_counts("barenboim") is True and store.load_tool_counts("barenboim") == {})
    check("clearing an empty store is False (nothing to remove)", store.clear_tool_counts("barenboim") is False)

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

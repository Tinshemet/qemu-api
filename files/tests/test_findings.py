#!/usr/bin/env python3
"""
test_findings.py — the Findings ledger (step 1 of the reward-cost engine).

Covers the ledger itself and its two live behaviors in the planner: a yielding tool
RECORDS what it learned, and a call whose finding is already known is SKIPPED
(anti-rediscovery) instead of re-run.

Run:  PYTHONPATH=files python3 files/tests/test_findings.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.findings import Findings, yield_fact, extract_value
from orchestrator.ai.score import run_score

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


_SCHEMA = {"get_vm_ip": {"fact": "ip({name})", "value": "ip"}}
_TOOLS = [{"type": "function", "function": {"name": "get_vm_ip", "parameters": {}}}]


def scripted(tool, args):
    def _call(messages, tools):
        return {"message": {"tool_calls": [{"function": {"name": tool, "arguments": args}}]}}
    return _call


def main():
    print("ledger basics")
    f = Findings()
    check("empty has nothing", not f.has("ip(web)") and f.render() == "")
    f.record("ip(web)", "10.0.0.5", source="get_vm_ip")
    check("record + has + get", f.has("ip(web)") and f.get("ip(web)") == "10.0.0.5")
    check("render lists known facts", "ip(web)=10.0.0.5" in f.render())

    print("\nyield-schema")
    check("yield_fact instantiates the template", yield_fact("get_vm_ip", {"name": "web"}, _SCHEMA) == "ip(web)")
    check("no schema entry -> None", yield_fact("list_vms", {}, _SCHEMA) is None)
    check("extract_value pulls the result field", extract_value({"success": True, "ip": "1.2.3.4"}, _SCHEMA["get_vm_ip"]) == "1.2.3.4")

    print("\nin the planner: a yielding tool RECORDS its finding")
    f = Findings()
    calls = []
    def ex(t, a):
        calls.append((t, a))
        return {"success": True, "ip": "10.0.0.9"}
    r = run_score("find the ip of web", call_model=scripted("get_vm_ip", {"name": "web"}),
                  execute=ex, tools=_TOOLS, findings=f, findings_schema=_SCHEMA)
    check("leaf executed once", len(calls) == 1 and r["root"]["status"] == "done")
    check("finding recorded", f.get("ip(web)") == "10.0.0.9")

    print("\nanti-rediscovery: a KNOWN finding is skipped, not re-run")
    calls2 = []
    def ex2(t, a):
        calls2.append((t, a))
        return {"success": True, "ip": "SHOULD-NOT-RUN"}
    r2 = run_score("find the ip of web again", call_model=scripted("get_vm_ip", {"name": "web"}),
                   execute=ex2, tools=_TOOLS, findings=f, findings_schema=_SCHEMA)
    check("known finding -> tool NOT executed", calls2 == [])
    check("node still done, marked cached", r2["root"]["status"] == "done" and r2["root"].get("cached_finding") == "ip(web)")

    print("\nno findings config -> old behavior (tool runs normally)")
    calls3 = []
    r3 = run_score("find the ip of db", call_model=scripted("get_vm_ip", {"name": "db"}),
                   execute=lambda t, a: (calls3.append(t) or {"success": True, "ip": "x"}),
                   tools=_TOOLS)   # no findings/schema
    check("without a ledger the tool just runs", calls3 == ["get_vm_ip"] and r3["root"]["status"] == "done")

    print("\ninvalidation: a stale fact can be dropped")
    f = Findings(); f.record("ip(web)", "1.1.1.1"); f.record("status(web)", "up"); f.record("ip(db)", "2.2.2.2")
    dropped = f.invalidate_about("web")
    check("invalidate_about drops every fact mentioning the entity", dropped == 2 and not f.has("ip(web)") and not f.has("status(web)"))
    check("unrelated facts survive", f.has("ip(db)"))

    print("\nstaleness fix: a mutation invalidates findings, so the read runs again")
    f = Findings()
    T = _TOOLS + [{"type": "function", "function": {"name": "stop_vm", "parameters": {}}}]
    run_score("find ip of web", call_model=scripted("get_vm_ip", {"name": "web"}),
              execute=lambda t, a: {"success": True, "ip": "10.0.0.1"}, tools=T, findings=f, findings_schema=_SCHEMA)
    check("finding recorded", f.get("ip(web)") == "10.0.0.1")
    run_score("stop web", call_model=scripted("stop_vm", {"name": "web"}),
              execute=lambda t, a: {"success": True}, tools=T, findings=f, findings_schema=_SCHEMA)
    check("the mutation invalidated the now-stale finding", not f.has("ip(web)"))
    calls2 = []
    run_score("find ip of web again", call_model=scripted("get_vm_ip", {"name": "web"}),
              execute=lambda t, a: (calls2.append(t) or {"success": True, "ip": "10.0.0.2"}),
              tools=T, findings=f, findings_schema=_SCHEMA)
    check("re-read RUNS again (not skipped as stale) and re-learns", calls2 == ["get_vm_ip"] and f.get("ip(web)") == "10.0.0.2")

    print("\nevidence: an unverified claim carries a human-checkable pointer")
    f = Findings()
    f.record("port_open(80)", 80, source="claim_finding")                       # grounded, no evidence
    f.record("email(bob@x)", "bob@x", source="claim_finding", evidence="/etc/aliases:12")
    f.record("balance(5000)", 5000, source="claim_finding", evidence="invoice.pdf")
    check("grounded fact has no evidence", f.evidence("port_open(80)") is None)
    check("claim keeps its evidence", f.evidence("email(bob@x)") == "/etc/aliases:12")
    review = f.claims_for_review()
    check("only evidenced claims are up for review", {r["fact"] for r in review} == {"email(bob@x)", "balance(5000)"})
    check("review carries value + evidence", all(r["value"] is not None and r["evidence"] for r in review))
    check("grounded fact stays out of review", "port_open(80)" not in {r["fact"] for r in review})

    print("\nevidence threads from a tool result into the ledger")
    f = Findings()
    SCH = {"claim_finding": {"value": "value"}}
    TL = [{"type": "function", "function": {"name": "claim_finding", "parameters": {}}}]
    run_score("record the finance email", call_model=scripted("claim_finding", {"type": "email", "value": "cfo@x"}),
              execute=lambda t, a: {"success": True, "value": "cfo@x", "grounded": False, "evidence": "payroll export row 3"},
              tools=TL, findings=f, findings_schema=SCH)
    check("claim recorded with its evidence", f.get("email(cfo@x)") == "cfo@x" and f.evidence("email(cfo@x)") == "payroll export row 3")

    print("\nconfirm-gate: a pending claim can't close a goal until a human confirms it")
    f = Findings()
    f.record("email(cfo@x)", "cfo@x", source="claim_finding", evidence="payroll row 3")
    f.record("ip(web)", "10.0.0.5", source="get_vm_ip")     # probe fact, no status
    check("pending claim is recorded but NOT usable", f.has("email(cfo@x)") and not f.usable("email(cfo@x)"))
    check("is_pending true for the claim", f.is_pending("email(cfo@x)"))
    check("a probe fact (no status) stays usable", f.usable("ip(web)"))
    check("confirm flips it to usable", f.confirm("email(cfo@x)") is True and f.usable("email(cfo@x)"))
    check("confirm again is a no-op", f.confirm("email(cfo@x)") is False)
    check("confirmed claim is off the review list", not f.claims_for_review())
    check("a fresh claim can't clobber a confirmed fact",
          (f.record("email(cfo@x)", "attacker@x", source="claim_finding", evidence="spoof"),
           f.get("email(cfo@x)") == "cfo@x")[1])

    print("\npersistence: only claims cross runs; probe facts don't")
    f = Findings()
    f.record("phone(555)", "555", source="claim_finding", evidence="sig block")   # pending
    f.record("ip(db)", "2.2.2.2", source="get_vm_ip")                             # not a claim
    check("persistable keeps the claim, drops the probe fact", set(f.persistable()) == {"phone(555)"})
    g = Findings(); g.merge(f.persistable())
    check("merge seeds the pending claim (still pending)", g.is_pending("phone(555)") and not g.usable("phone(555)"))
    g.confirm("phone(555)")
    h = Findings(); h.merge(g.persistable())
    check("a run inherits a CONFIRMED claim as usable", h.usable("phone(555)"))
    check("merge never clobbers a fact already present",
          (h.record("phone(555)", "999", source="x"), h.merge({"phone(555)": {"value": "000"}}),
           h.get("phone(555)") == "999")[2])

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

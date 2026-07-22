#!/usr/bin/env python3
"""
test_or_tree.py — OR goals: alternatives where only ONE need succeed.

Proves the OR half of the AND/OR tree (design: "AND = all children; OR = max over
alternatives"). The model declares an OR goal with the `alternatives` meta-tool; the
tree tries them in order, STOPS at the first success (the rest are skipped, never run),
ROLLS BACK a failed alternative's savepoint before the next, and fails only if every
alternative fails. The contract root predicate still governs an OR root, and the
economics prices it as max-over-alternatives.

Run:  PYTHONPATH=files python3 files/tests/test_or_tree.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.planner.engine import Engine
from orchestrator.ai.planner.score import run_score
from orchestrator.ai.planner.reward_cost import economics, backup, DEFAULTS

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


def _alt(options):
    return {"message": {"tool_calls": [{"function": {"name": "alternatives", "arguments": {"options": options}}}]}}


def _tc(name, args):
    return {"message": {"tool_calls": [{"function": {"name": name, "arguments": args}}]}}


def _goal_of(m):
    return next((x["content"][6:] for x in m if x["role"] == "user" and x["content"].startswith("Goal: ")), "")


def _run(model, execute, **engine_kw):
    return run_score("reboot vm", call_model=model, execute=execute, tools=_TOOLS,
                     engine=Engine(legal_filter=_NO_LEGAL, **engine_kw), max_retries=0)


def main():
    # A model that declares two alternatives for the root; each alternative is one leaf.
    def model(m, t):
        goal = _goal_of(m)
        if goal == "reboot vm":
            return _alt(["reboot via the agent", "force reset the box"])
        return _tc("stop_vm", {"name": goal.split()[-1]})

    print("first alternative wins → the rest are skipped, never executed")
    calls = []
    r = _run(model, lambda t, a: (calls.append((t, a["name"])) or {"success": True}))
    kids = r["root"]["children"]
    check("root is done", r["root"]["status"] == "done" and r["ok"])
    check("node is marked OR", r["root"].get("mode") == "or")
    check("first alternative ran and is done", kids[0]["status"] == "done")
    check("second alternative is skipped (alt_satisfied)",
          kids[1]["status"] == "skipped" and kids[1]["reason"] == "alt_satisfied")
    check("only ONE alternative actually executed", calls == [("stop_vm", "agent")])

    print("\nfirst fails → fall through to the second, which wins")
    calls = []
    # the first alternative's leaf ('agent') fails; the second ('box') succeeds.
    r = _run(model, lambda t, a: (calls.append((t, a["name"])) or {"success": a["name"] != "agent"}))
    kids = r["root"]["children"]
    check("root is done (an alternative succeeded)", r["root"]["status"] == "done")
    check("first alternative failed", kids[0]["status"] == "failed")
    check("second alternative done", kids[1]["status"] == "done")
    check("both were tried, in order", calls == [("stop_vm", "agent"), ("stop_vm", "box")])

    print("\nevery alternative fails → the OR node fails")
    r = _run(model, lambda t, a: {"success": False})
    check("root failed (no alternative worked)", r["root"]["status"] == "failed")
    check("still marked OR", r["root"].get("mode") == "or")

    print("\na failed alternative's savepoint is ROLLED BACK before the next")
    # gate a destructive leaf to 'checkpoint'; the failing first alternative must be
    # rolled back (restore its savepoint, drop its ledger residue) before the second.
    def dmodel(m, t):
        goal = _goal_of(m)
        if goal == "reboot vm":
            return _alt(["nuke A", "nuke B"])
        return _tc("delete_vm", {"name": goal.split()[-1]})
    ops = []
    def dexec(t, a):
        ops.append((t, a.get("label") or a.get("name")))
        return {"success": not (t == "delete_vm" and a["name"] == "A")}   # first alt fails
    r = _run(dmodel, dexec, gate=lambda t, a: "checkpoint" if t == "delete_vm" else "proceed")
    check("root is done via the second alternative", r["root"]["status"] == "done")
    did_rollback = any(t == "rollback" for t, _ in ops)
    check("the failed alternative was rolled back", did_rollback)
    # after rollback+trim, the ledger holds only the surviving (second) alternative's work
    led_tools = [e["tool"] for e in r["ledger"]]
    check("ledger keeps only the winning alternative's records", led_tools == ["checkpoint", "delete_vm"]
          and r["ledger"][-1]["args"]["name"] == "B")

    print("\nthe contract ROOT predicate still governs an OR root")
    r = _run(model, lambda t, a: {"success": True}, verify_goal=lambda g, k, l: False)
    check("satisfied OR root, but goal predicate fails → unverified",
          r["root"]["status"] == "unverified" and r["root"]["reason"] == "goal_predicate_unmet")

    print("\nalternatives are tried in CE order, best first (not list order)")
    # listed cheap-risky, solid, wasteful; CE ranks solid > cheap-risky > wasteful(<0).
    def rmodel(m, t):
        goal = _goal_of(m)
        if goal == "reboot vm":
            return _alt(["cheap risky", "solid", "wasteful"])
        return _tc("stop_vm", {"name": goal.split()[-1]})
    est = lambda g, d: {"cheap risky": 0.5, "solid": 2.0, "wasteful": -1.0}[g]
    calls = []
    # 'solid' (highest CE) is tried FIRST but fails; 'cheap risky' then wins.
    r = _run(rmodel, lambda t, a: (calls.append((t, a["name"])) or {"success": a["name"] != "solid"}),
             estimate=est, ce_floor=0.0)
    check("root done", r["root"]["status"] == "done")
    check("tried highest-CE first despite list order", calls == [("stop_vm", "solid"), ("stop_vm", "risky")])
    bygoal = {c["goal"]: c for c in r["root"]["children"]}
    check("the negative-CE alternative was PRUNED, never run",
          bygoal["wasteful"]["status"] == "skipped" and bygoal["wasteful"]["reason"] == "pruned_low_ce")
    check("pruned alt carries its CE estimate", bygoal["wasteful"]["ce_est"] == -1.0)
    check("tried alts carry their CE estimate", bygoal["solid"]["ce_est"] == 2.0)

    print("\nevery alternative below the worth-it floor → don't pursue (skipped, not run)")
    calls = []
    r = _run(rmodel, lambda t, a: (calls.append(t) or {"success": True}),
             estimate=lambda g, d: -5.0, ce_floor=0.0)
    check("node is skipped as not worth it", r["root"]["status"] == "skipped" and r["root"]["reason"] == "not_worth_it")
    check("nothing executed (all pruned)", calls == [])
    check("all alternatives recorded as pruned", all(c["reason"] == "pruned_low_ce" for c in r["root"]["children"]))

    print("\neconomics prices an OR node as max-over-alternatives (+ books its reward)")
    # a costly-but-likely alt vs a cheap-but-unlikely alt: OR should take the max-CE one.
    cfg = dict(DEFAULTS)
    node = {"kind": "or", "reward": 4.0, "children": [
        {"kind": "leaf", "cost": 0.5, "p": 0.9, "reward": 0.0},
        {"kind": "leaf", "cost": 0.1, "p": 0.2, "reward": 0.0}]}
    b = backup(node, cfg)
    # best alt is the p=0.9 one; its μ = 0.9·0 − 0.5 = −0.5, plus reward 4·0.9 = 3.6 → 3.1
    check("OR backs up the best alternative + its closure reward", abs(b["mu"] - 3.1) < 1e-6)
    check("OR success prob is the chosen alternative's", abs(b["p"] - 0.9) < 1e-9)

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

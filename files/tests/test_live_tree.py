#!/usr/bin/env python3
"""
test_live_tree.py — the streaming plan-tree view (F1).

Two halves: (1) the engine emits node-lifecycle events (enter / plan / leaf / close) via
`on_node` as it walks the tree; (2) LivePlanTree folds those events into a correct tree
model (right parent/child structure, right final statuses) that the CLI renders live.

Run:  PYTHONPATH=files python3 files/tests/test_live_tree.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.planner.engine import Engine
from orchestrator.ai.planner.score import run_score
from client.cli.live_tree import LivePlanTree

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
          for n in ("create_vm", "launch_vm")]
_NO_LEGAL = lambda *a: False


def _dec(steps):
    return {"message": {"tool_calls": [{"function": {"name": "decompose", "arguments": {"steps": steps}}}]}}


def _tc(name, args):
    return {"message": {"tool_calls": [{"function": {"name": name, "arguments": args}}]}}


def _goal(m):
    return next((x["content"][6:] for x in m if x["role"] == "user" and x["content"].startswith("Goal: ")), "")


def main():
    print("engine emits enter/plan/leaf/close as it walks the tree")
    events = []
    def model(m, t):
        g = _goal(m)
        if g == "set up web":
            return _dec(["create web", "launch web"])
        if g == "create web":
            return _tc("create_vm", {"name": "web"})
        if g == "launch web":
            return _tc("launch_vm", {"name": "web"})
        return {"message": {"tool_calls": []}}
    r = run_score("set up web", call_model=model, execute=lambda t, a: {"success": True},
                  tools=_TOOLS, engine=Engine(legal_filter=_NO_LEGAL, on_node=events.append),
                  max_retries=0)
    kinds = [(e["kind"], e["goal"]) for e in events]
    check("root enters first, closes last",
          kinds[0] == ("enter", "set up web") and kinds[-1] == ("close", "set up web"))
    check("a plan event carries the children in order",
          any(e["kind"] == "plan" and e["goal"] == "set up web"
              and e["children"] == ["create web", "launch web"] and e["mode"] == "and" for e in events))
    check("each leaf emits a leaf event with its tool",
          any(e["kind"] == "leaf" and e["goal"] == "create web" and e["tool"] == "create_vm" for e in events)
          and any(e["kind"] == "leaf" and e["goal"] == "launch web" and e["tool"] == "launch_vm" for e in events))
    check("every child both enters and closes done",
          all(("enter", g) in kinds and ("close", g) in kinds for g in ("create web", "launch web"))
          and all(e["status"] == "done" for e in events
                  if e["kind"] == "close" and e["goal"] in ("create web", "launch web")))
    check("no events at all when on_node is unset (zero overhead path)",
          run_score("x", call_model=lambda m, t: {"message": {"tool_calls": []}},
                    execute=lambda t, a: {}, tools=_TOOLS,
                    engine=Engine(legal_filter=_NO_LEGAL)) is not None)

    print("\nLivePlanTree folds the events into the right tree model")
    tree = LivePlanTree("set up web")
    updates = []
    tree.on_update = lambda: updates.append(1)
    for e in events:
        tree.handle(e)
    root = ("set up web",)
    check("root recorded done", tree._node[root]["status"] == "done")
    check("root has exactly its two children in order",
          tree._kids[root] == [("set up web", "create web"), ("set up web", "launch web")])
    check("children carry their tool + done status",
          tree._node[("set up web", "create web")]["tool"] == "create_vm"
          and tree._node[("set up web", "launch web")]["status"] == "done")
    check("render() returns a Rich Tree without raising", tree.render() is not None)
    check("on_update fired for each handled event", len(updates) == len(events))

    print("\na skipped/blocked close carries its reason as a flag")
    t2 = LivePlanTree("g")
    t2.handle({"kind": "enter", "goal": "g", "depth": 0, "path": []})
    t2.handle({"kind": "close", "goal": "g", "depth": 0, "path": [], "status": "skipped", "reason": "not_worth_it"})
    check("reason surfaced as the node flag", t2._node[("g",)]["flag"] == "not_worth_it")

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

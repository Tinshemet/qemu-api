#!/usr/bin/env python3
"""
test_score.py — Score engine unit tests (no Ollama required).

Drives run_score() with a SCRIPTED model (canned tool calls per goal) and a stub
executor, verifying the decompose-to-primitives logic in isolation: direct-primitive
leaves, recursive decomposition executed in order, depth-bounding, destructive
confirm/skip, failure capture, and the ledger.

Run:  PYTHONPATH=files python3 files/tests/test_score.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.score import run_score, DECOMPOSE_TOOL

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


def scripted_model(script):
    """A model that returns a canned (tool, args) per goal. Records goals seen."""
    seen = []
    def _call(messages, tools):
        goal = messages[-1]["content"].replace("Goal: ", "")
        seen.append(goal)
        entry = script.get(goal)
        if entry is None:
            return {"message": {"tool_calls": []}}
        name, args = entry
        return {"message": {"tool_calls": [{"function": {"name": name, "arguments": args}}]}}
    return _call, seen


def stub_exec(fail=None):
    """Executor stub: records (tool, args); returns success unless tool in `fail`."""
    calls = []
    fail = fail or set()
    def _exec(tool, args):
        calls.append((tool, args))
        if tool in fail:
            return {"success": False, "error": "boom"}
        return {"success": True}
    return _exec, calls


_TOOLS = [{"type": "function", "function": {"name": n, "parameters": {}}}
          for n in ("create_vm", "launch_vm", "stop_vm", "delete_vm")]


def main():
    print("direct primitive → single leaf")
    model, _ = scripted_model({"stop dev": ("stop_vm", {"name": "dev"})})
    ex, calls = stub_exec()
    r = run_score("stop dev", call_model=model, execute=ex, tools=_TOOLS)
    check("root done", r["root"]["status"] == "done")
    check("one leaf executed", calls == [("stop_vm", {"name": "dev"})])
    check("ledger has the leaf", len(r["ledger"]) == 1 and r["ledger"][0]["tool"] == "stop_vm")
    check("ok True", r["ok"] is True)

    print("\ndecompose → children executed IN ORDER")
    model, seen = scripted_model({
        "bring up dev": ("decompose", {"steps": ["create dev", "launch dev"]}),
        "create dev":   ("create_vm", {"name": "dev", "os_type": "linux"}),
        "launch dev":   ("launch_vm", {"name": "dev"}),
    })
    ex, calls = stub_exec()
    r = run_score("bring up dev", call_model=model, execute=ex, tools=_TOOLS)
    check("root has children", "children" in r["root"] and len(r["root"]["children"]) == 2)
    check("primitives executed in order", [c[0] for c in calls] == ["create_vm", "launch_vm"])
    check("ledger records both", [e["tool"] for e in r["ledger"]] == ["create_vm", "launch_vm"])
    check("root done (all children done)", r["root"]["status"] == "done")

    print("\ndepth bound → blocked, never infinite")
    model, _ = scripted_model({})  # unknown goal → but we force endless decompose:
    def always_decompose(messages, tools):
        return {"message": {"tool_calls": [{"function": {"name": "decompose", "arguments": {"steps": ["deeper"]}}}]}}
    ex, calls = stub_exec()
    r = run_score("infinite", call_model=always_decompose, execute=ex, tools=_TOOLS, max_depth=2)
    def deepest(node):
        return deepest(node["children"][0]) if node.get("children") else node
    check("bottoms out at max_depth (blocked)", deepest(r["root"])["status"] == "blocked")
    check("nothing executed on a pure-decompose tree", calls == [])

    print("\ndestructive leaf → confirm gate")
    model, _ = scripted_model({"wipe dev": ("delete_vm", {"name": "dev"})})
    ex, calls = stub_exec()
    r = run_score("wipe dev", call_model=model, execute=ex, tools=_TOOLS,
                  is_destructive=lambda t, a: t == "delete_vm", confirm=lambda t, a: False)
    check("destructive leaf skipped when confirm denies", r["root"]["status"] == "skipped" and calls == [])
    ex2, calls2 = stub_exec()
    r2 = run_score("wipe dev", call_model=scripted_model({"wipe dev": ("delete_vm", {"name": "dev"})})[0],
                   execute=ex2, tools=_TOOLS,
                   is_destructive=lambda t, a: t == "delete_vm", confirm=lambda t, a: True)
    check("destructive leaf runs when confirm approves", r2["root"]["status"] == "done" and len(calls2) == 1)

    print("\nfailure + no-action capture")
    model, _ = scripted_model({"stop dev": ("stop_vm", {"name": "dev"})})
    ex, _ = stub_exec(fail={"stop_vm"})
    r = run_score("stop dev", call_model=model, execute=ex, tools=_TOOLS)
    check("failed leaf → node failed, ok False", r["root"]["status"] == "failed" and r["ok"] is False)
    r = run_score("ponder", call_model=scripted_model({})[0], execute=stub_exec()[0], tools=_TOOLS)
    check("no tool call → no_action", r["root"]["status"] == "no_action")

    print("\nmeta-tool shape")
    check("decompose tool requires ordered steps",
          DECOMPOSE_TOOL["function"]["parameters"]["required"] == ["steps"])

    print(f"\n{'='*48}\n  {_PASS} passed, {_FAIL} failed\n{'='*48}")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

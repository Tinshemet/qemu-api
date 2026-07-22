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

from orchestrator.ai.planner.score import run_score, DECOMPOSE_TOOL

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


def tempting_model(plan, prim):
    """A weak model that ONE-SHOTS when a primitive is on the table (returns
    prim[goal]), but when FORCED (only decompose offered) returns plan[goal] steps —
    or [goal] (i.e. atomic) if the goal has no plan. Lets a test prove the decompose-
    first scaffolding flips a one-shot into a real decomposition."""
    def _call(messages, tools):
        goal = next((m["content"][6:] for m in messages
                     if m["role"] == "user" and m["content"].startswith("Goal: ")), "")
        if [t["function"]["name"] for t in tools] == ["decompose"]:
            steps = plan.get(goal) or [goal]
            return {"message": {"tool_calls": [{"function": {"name": "decompose", "arguments": {"steps": steps}}}]}}
        entry = prim.get(goal)
        if not entry:
            return {"message": {"tool_calls": []}}
        return {"message": {"tool_calls": [{"function": {"name": entry[0], "arguments": entry[1]}}]}}
    return _call


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

    # (the old is_destructive/confirm backstop is retired — the split gate below
    #  (legal filter + consent referendum) replaced it.)

    print("\ncontract bounds the tree: gate halt / checkpoint")
    # halt = a contract red line → the leaf is blocked, never executed.
    model, _ = scripted_model({"wipe dev": ("delete_vm", {"name": "dev"})})
    ex, calls = stub_exec()
    r = run_score("wipe dev", call_model=model, execute=ex, tools=_TOOLS,
                  gate=lambda t, a: "halt" if t == "delete_vm" else "proceed")
    check("halt → node blocked (contract_halt)",
          r["root"]["status"] == "blocked" and r["root"].get("reason") == "contract_halt")
    check("halt → leaf never executed", calls == [])
    # checkpoint = savepoint FIRST, then the leaf runs.
    model, _ = scripted_model({"restore dev": ("snapshot_restore", {"name": "dev", "snap_name": "s1"})})
    ex, calls = stub_exec()
    r = run_score("restore dev", call_model=model, execute=ex, tools=_TOOLS,
                  gate=lambda t, a: "checkpoint" if t == "snapshot_restore" else "proceed")
    check("checkpoint runs before the leaf",
          [c[0] for c in calls] == ["checkpoint", "snapshot_restore"])
    check("checkpoint + leaf both in ledger",
          [e["tool"] for e in r["ledger"]] == ["checkpoint", "snapshot_restore"] and r["root"]["status"] == "done")
    # checkpoint FAILS → can't make it revertible → block, don't run the leaf.
    ex, calls = stub_exec(fail={"checkpoint"})
    r = run_score("restore dev", call_model=scripted_model({"restore dev": ("snapshot_restore", {"name": "dev", "snap_name": "s1"})})[0],
                  execute=ex, tools=_TOOLS, gate=lambda t, a: "checkpoint" if t == "snapshot_restore" else "proceed")
    check("failed checkpoint → node blocked, leaf not run",
          r["root"]["status"] == "blocked" and r["root"].get("reason") == "checkpoint_failed"
          and [c[0] for c in calls] == ["checkpoint"])

    print("\nverified completion: contract criterion checked against reality")
    # criterion_of defaults to the contract: create_vm -> "present", etc.
    seen = []
    model, _ = scripted_model({"make dev": ("create_vm", {"name": "dev", "os_type": "linux"})})
    r = run_score("make dev", call_model=model, execute=stub_exec()[0], tools=_TOOLS,
                  verify=lambda crit, t, a, res: seen.append(crit) or True)
    check("leaf DONE when criterion verifies", r["root"]["status"] == "done")
    check("criterion came from the contract (create_vm -> present)", seen == ["present"])
    # execute says success, but reality check FAILS -> unverified, not done.
    model, _ = scripted_model({"make dev": ("create_vm", {"name": "dev", "os_type": "linux"})})
    r = run_score("make dev", call_model=model, execute=stub_exec()[0], tools=_TOOLS,
                  verify=lambda crit, t, a, res: False)
    check("execute-ok but criterion-unmet -> unverified", r["root"]["status"] == "unverified")
    check("unverified leaf recorded ok=False", r["ledger"][0]["ok"] is False and r["ledger"][0].get("verified") is False)
    # No verify callback -> trust execute (unchanged behavior even with criteria present).
    model, _ = scripted_model({"make dev": ("create_vm", {"name": "dev", "os_type": "linux"})})
    r = run_score("make dev", call_model=model, execute=stub_exec()[0], tools=_TOOLS)
    check("no verify -> trust execute (done)", r["root"]["status"] == "done")
    # Verified completion PROPAGATES: one unverified child -> parent not done.
    model, _ = scripted_model({
        "bring up dev": ("decompose", {"steps": ["create dev", "launch dev"]}),
        "create dev":   ("create_vm", {"name": "dev", "os_type": "linux"}),
        "launch dev":   ("launch_vm", {"name": "dev"}),
    })
    r = run_score("bring up dev", call_model=model, execute=stub_exec()[0], tools=_TOOLS,
                  verify=lambda crit, t, a, res: t != "launch_vm")   # launch fails its criterion
    kids = {c["tool"]: c["status"] for c in r["root"]["children"]}
    check("verified child done, unverified child flagged",
          kids.get("create_vm") == "done" and kids.get("launch_vm") == "unverified")
    check("parent not done when a child is unverified", r["root"]["status"] == "partial")

    print("\nhonesty rule: an opaque foreign command with no post-condition is unverifiable")
    _gtools = [{"type": "function", "function": {"name": "run_guest_command", "parameters": {}}}]
    r = run_score("scan the box",
                  call_model=scripted_model({"scan the box": ("run_guest_command", {"name": "dev", "command": "whoami"})})[0],
                  execute=stub_exec()[0], tools=_gtools)
    check("opaque run_guest_command → unverified", r["root"]["status"] == "unverified")
    check("reason is 'unverifiable' (not silently done)", r["root"].get("reason") == "unverifiable")
    r = run_score("scan the box",
                  call_model=scripted_model({"scan the box": ("run_guest_command", {"name": "dev", "command": "whoami"})})[0],
                  execute=stub_exec()[0], tools=_gtools,
                  criterion_of=lambda t: "present", verify=lambda c, t, a, res: True)
    check("with a declared+passing post-condition → done", r["root"]["status"] == "done")

    print("\ndeterministic finding-validation: a finding counts only if a probe confirms it")
    from orchestrator.ai.planner.findings import Findings
    _fs = {"scan_port": {"fact": "open({name}:{port})", "value": "state",
                         "verify": "{name}:port_listening:{port}"}}
    _ptools = [{"type": "function", "function": {"name": "scan_port", "parameters": {}}}]
    def _exec_probe(holds):
        def ex(t, a):
            return {"success": True, "holds": holds} if t == "guest_probe" else {"success": True, "state": "open"}
        return ex
    for holds, want in [(True, True), (False, False)]:
        f = Findings()
        run_score("scan it",
                  call_model=scripted_model({"scan it": ("scan_port", {"name": "web01", "port": "443"})})[0],
                  execute=_exec_probe(holds), tools=_ptools, findings=f, findings_schema=_fs,
                  criterion_of=lambda t: None)
        check(f"probe {'confirms' if holds else 'denies'} → recorded={want}",
              f.has("open(web01:443)") is want)

    print("\nbacktrack: soft-fail → retry a DIFFERENT approach (failed-branch memory)")
    prompts = []
    step = {"n": 0}
    def flaky(messages, tools):
        prompts.append(messages[0]["content"]); step["n"] += 1
        tool = "stop_vm" if step["n"] == 1 else "launch_vm"   # 1st approach fails, switch on retry
        return {"message": {"tool_calls": [{"function": {"name": tool, "arguments": {"name": "dev"}}}]}}
    ex, calls = stub_exec(fail={"stop_vm"})
    r = run_score("get dev going", call_model=flaky, execute=ex, tools=_TOOLS)
    check("recovered after backtrack (done)", r["root"]["status"] == "done" and r["root"].get("recovered") is True)
    check("retried once", r["root"].get("retries") == 1)
    check("switched to the second approach (launch_vm)", ("launch_vm", {"name": "dev"}) in calls)
    check("retry prompt carried the failed approach", any("ALREADY TRIED" in p and "stop_vm" in p for p in prompts))

    print("\nbacktrack: exhaust the budget → stay failed, remember every approach")
    ex, calls = stub_exec(fail={"stop_vm"})
    def stubborn(m, t):
        return {"message": {"tool_calls": [{"function": {"name": "stop_vm", "arguments": {"name": "x"}}}]}}
    r = run_score("stop x", call_model=stubborn, execute=ex, tools=_TOOLS, max_retries=2)
    check("exhausted → still failed", r["root"]["status"] == "failed" and r["root"].get("retries") == 2)
    check("tried-memory holds all failed approaches", len(r["root"].get("tried", [])) == 2)
    check("attempted max_retries+1 times", len([c for c in calls if c[0] == "stop_vm"]) == 3)

    print("\nbacktrack is LOCAL: a succeeded sibling is never re-run")
    seen2 = {"create": 0}
    def sibling_model(messages, tools):
        goal = messages[-1]["content"]
        if "decompose" in goal or "bring up" in goal:
            return {"message": {"tool_calls": [{"function": {"name": "decompose", "arguments": {"steps": ["create dev", "start dev"]}}}]}}
        if "create dev" in goal:
            seen2["create"] += 1
            return {"message": {"tool_calls": [{"function": {"name": "create_vm", "arguments": {"name": "dev", "os_type": "linux"}}}]}}
        return {"message": {"tool_calls": [{"function": {"name": "stop_vm", "arguments": {"name": "dev"}}}]}}  # 'start dev' → stop_vm (fails)
    ex, calls = stub_exec(fail={"stop_vm"})
    r = run_score("bring up dev", call_model=sibling_model, execute=ex, tools=_TOOLS)
    check("failing child retried, succeeded child (create_vm) run exactly once", seen2["create"] == 1)
    check("create_vm executed once despite sibling backtrack", len([c for c in calls if c[0] == "create_vm"]) == 1)

    print("\nrollback-on-backtrack: a failed destructive branch leaves no residue")
    _SNAP = _TOOLS + [{"type": "function", "function": {"name": "snapshot_restore", "parameters": {}}}]
    seq = []
    step = {"n": 0}
    def restore_then_launch(messages, tools):
        step["n"] += 1
        if step["n"] == 1:
            return {"message": {"tool_calls": [{"function": {"name": "snapshot_restore", "arguments": {"name": "dev", "snap_name": "s"}}}]}}
        return {"message": {"tool_calls": [{"function": {"name": "launch_vm", "arguments": {"name": "dev"}}}]}}
    def ex_seq(tool, args):
        seq.append(tool)
        return {"success": False, "error": "boom"} if tool == "snapshot_restore" else {"success": True}
    # autonomous gate: snapshot_restore is a name-tier op -> "checkpoint" before it.
    r = run_score("fix dev", call_model=restore_then_launch, execute=ex_seq, tools=_SNAP,
                  gate=lambda t, a: "checkpoint" if t == "snapshot_restore" else "proceed")
    check("checkpoint taken before the destructive leaf", seq[0] == "checkpoint")
    check("rollback fired when that branch failed", "rollback" in seq)
    check("recovered via a different approach", r["root"]["status"] == "done" and r["root"].get("recovered") is True)
    check("node records the rollback", r["root"].get("rolled_back") == 1)
    check("ledger has NO residue from the rolled-back branch",
          [e["tool"] for e in r["ledger"]] == ["launch_vm"])
    # order: checkpoint, snapshot_restore(fail), rollback, launch_vm
    check("full sequence: checkpoint→attempt→rollback→retry",
          seq == ["checkpoint", "snapshot_restore", "rollback", "launch_vm"])

    print("\ndecompose-first scaffolding: force a one-shotting model to split")
    plan = {"set up dev": ["create dev", "launch dev"]}
    prim = {"set up dev": ("create_vm", {"name": "dev", "os_type": "linux"}),   # the one-shot temptation
            "create dev": ("create_vm", {"name": "dev", "os_type": "linux"}),
            "launch dev": ("launch_vm", {"name": "dev"})}
    m = tempting_model(plan, prim)
    # OFF: the model grabs the primitive → a one-shot leaf, "launch" dropped.
    ex, calls = stub_exec()
    r = run_score("set up dev", call_model=m, execute=ex, tools=_TOOLS, decompose_first=False)
    check("without scaffolding → one-shot leaf", "children" not in r["root"] and [c[0] for c in calls] == ["create_vm"])
    # ON: the forced pre-gate makes it decompose into the two real steps.
    ex, calls = stub_exec()
    r = run_score("set up dev", call_model=m, execute=ex, tools=_TOOLS, decompose_first=True)
    check("with scaffolding → decomposed into 2", "children" in r["root"] and len(r["root"]["children"]) == 2)
    check("both steps executed in order", [c[0] for c in calls] == ["create_vm", "launch_vm"])
    check("root done", r["root"]["status"] == "done")
    # An atomic goal must NOT over-decompose: forced decompose collapses to the goal.
    ex, calls = stub_exec()
    r = run_score("stop dev", call_model=tempting_model({}, {"stop dev": ("stop_vm", {"name": "dev"})}),
                  execute=ex, tools=_TOOLS, decompose_first=True)
    check("atomic goal stays a leaf (no over-decompose)", "children" not in r["root"] and [c[0] for c in calls] == ["stop_vm"])

    print("\nsplit gate: LEGAL FILTER (hard) vs CONSENT SURFACE (referendum)")
    # A: a forbidden tool is dropped up front — never executed, never surfaced.
    ex, calls = stub_exec()
    r = run_score("wipe net", call_model=scripted_model({"wipe net": ("delete_network", {"net_name": "n"})})[0],
                  execute=ex, tools=_TOOLS + [{"type": "function", "function": {"name": "delete_network", "parameters": {}}}],
                  legal_filter=lambda t, a: t == "delete_network")
    check("forbidden tool → node 'forbidden'", r["root"]["status"] == "forbidden" and r["root"].get("reason") == "legal_red_line")
    check("forbidden tool NOT executed", calls == [])
    # D: destructive-but-legal → referendum. Granted (with consequence surfaced) → proceeds.
    seen = []
    ex, calls = stub_exec()
    r = run_score("wipe dev", call_model=scripted_model({"wipe dev": ("delete_vm", {"name": "dev"})})[0],
                  execute=ex, tools=_TOOLS, gate=lambda t, a: "halt" if t == "delete_vm" else "proceed",
                  referendum=lambda t, a, consequence: (seen.append(consequence) or True))
    check("consent GRANTED → proceeds (checkpoint then act)", [c[0] for c in calls] == ["checkpoint", "delete_vm"])
    check("referendum surfaced the CONSEQUENCE", seen == ["delete VM"])
    # D denied → blocked; no referendum handler → the old categorical halt.
    ex, calls = stub_exec()
    r = run_score("wipe dev", call_model=scripted_model({"wipe dev": ("delete_vm", {"name": "dev"})})[0],
                  execute=ex, tools=_TOOLS, gate=lambda t, a: "halt" if t == "delete_vm" else "proceed",
                  referendum=lambda t, a, c: False)
    check("consent DENIED → blocked, not executed", r["root"].get("reason") == "consent_denied" and calls == [])
    ex, calls = stub_exec()
    r = run_score("wipe dev", call_model=scripted_model({"wipe dev": ("delete_vm", {"name": "dev"})})[0],
                  execute=ex, tools=_TOOLS, gate=lambda t, a: "halt" if t == "delete_vm" else "proceed")
    check("no referendum → categorical halt (backward compat)", r["root"].get("reason") == "contract_halt" and calls == [])

    print("\nmethod cache: a known goal decomposes with ZERO model calls")
    from orchestrator.ai.planner.method_cache import seeded
    calls = {"n": 0}
    def counting_model(messages, tools):
        calls["n"] += 1   # a cache HIT must not reach here for the split
        goal = next((m["content"][6:] for m in messages
                     if m["role"] == "user" and m["content"].startswith("Goal: ")), "")
        prim = {"create a linux vm named alpha": ("create_vm", {"name": "alpha"}),
                "create a linux vm named beta": ("create_vm", {"name": "beta"})}
        e = prim.get(goal)
        return {"message": {"tool_calls": [{"function": {"name": e[0], "arguments": e[1]}}] if e else []}}
    ex, ran = stub_exec()
    r = run_score("create two linux vms named alpha and beta", call_model=counting_model,
                  execute=ex, tools=_TOOLS, decompose_first=True, method_cache=seeded())
    check("cache HIT → decomposed via cache (method=cache)", r["root"].get("method") == "cache")
    check("cache HIT → both leaves executed", [c[0] for c in ran] == ["create_vm", "create_vm"])
    check("cache HIT → the SPLIT cost no model call (only leaves)", calls["n"] == 2)

    print("\nfailure + no-action capture")
    model, _ = scripted_model({"stop dev": ("stop_vm", {"name": "dev"})})
    ex, _ = stub_exec(fail={"stop_vm"})
    r = run_score("stop dev", call_model=model, execute=ex, tools=_TOOLS)
    check("failed leaf → node failed, ok False", r["root"]["status"] == "failed" and r["ok"] is False)
    r = run_score("ponder", call_model=scripted_model({})[0], execute=stub_exec()[0], tools=_TOOLS)
    check("no tool call → no_action", r["root"]["status"] == "no_action")

    print("\nlate-step grounding: progress carry-forward")
    from orchestrator.ai.planner.score import _progress_summary
    led = [{"tool": "create_vm", "args": {"name": "probe"}, "ok": True},
           {"tool": "create_network", "args": {"net_name": "labnet"}, "ok": True}]
    ps = _progress_summary(led)
    check("progress names the created entities", "probe" in ps and "labnet" in ps)
    check("failed steps are marked", "FAILED" in _progress_summary([{"tool": "launch_vm", "args": {"name": "x"}, "ok": False}]))
    check("empty ledger → empty progress", _progress_summary([]) == "")

    print("\nmeta-tool shape")
    check("decompose tool requires ordered steps",
          DECOMPOSE_TOOL["function"]["parameters"]["required"] == ["steps"])

    print(f"\n{'='*48}\n  {_PASS}/{_PASS + _FAIL} passed\n{'='*48}")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

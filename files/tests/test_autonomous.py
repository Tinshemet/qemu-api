#!/usr/bin/env python3
"""
test_autonomous.py — the autonomous execution loop (autonomous.run_autonomous).

Drives the FULL loop against a stub "world" (VMs the stub executor mutates and the
Library-backed verifier reads), with a scripted model — no Ollama, no real executor.
Proves the loop end-to-end: decompose → execute → verify against reality → backtrack →
halt, all with no human.

Run:  PYTHONPATH=files python3 files/tests/test_autonomous.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.planner.autonomous import run_autonomous, make_library_verifier
from orchestrator.ai.mission.mission import Mission

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


class World:
    """A tiny stateful world: the executor mutates it, the verifier reads it — so
    verification is real (against reality), not a stub that always says yes."""
    def __init__(self, lie=False):
        self.vms = {}
        self.lie = lie   # when True, create_vm reports success but doesn't change the world

    def execute(self, tool, args):
        n = args.get("name") or args.get("new_name")
        if tool == "create_vm" and not self.lie:
            self.vms[n] = {"status": "stopped"}
        elif tool == "launch_vm" and n in self.vms:
            self.vms[n]["status"] = "running"
        elif tool == "stop_vm" and n in self.vms:
            self.vms[n]["status"] = "stopped"
        elif tool == "delete_vm":
            self.vms.pop(n, None)
        return {"success": True}


def scripted(script):
    def _call(messages, tools):
        goal = messages[-1]["content"].replace("Goal: ", "")
        entry = script.get(goal)
        if entry is None:
            return {"message": {"tool_calls": []}}
        name, args = entry
        return {"message": {"tool_calls": [{"function": {"name": name, "arguments": args}}]}}
    return _call


_TOOLS = [{"type": "function", "function": {"name": n, "parameters": {}}}
          for n in ("create_vm", "launch_vm", "stop_vm", "delete_vm")]


def main():
    print("happy path: decompose → execute → verify against reality → done")
    w = World()
    model = scripted({
        "set up dev":  ("decompose", {"steps": ["create dev", "launch dev"]}),
        "create dev":  ("create_vm", {"name": "dev", "os_type": "linux"}),
        "launch dev":  ("launch_vm", {"name": "dev"}),
    })
    seen = []
    r = run_autonomous("set up dev", call_model=model, execute=w.execute, tools=_TOOLS,
                       vms_getter=lambda: w.vms, on_event=lambda e: seen.append(e["tool"]))
    check("root done", r["root"]["status"] == "done" and r["ok"] is True)
    check("both primitives executed in order", [e["tool"] for e in r["events"]] == ["create_vm", "launch_vm"])
    check("events streamed via on_event", seen == ["create_vm", "launch_vm"])
    check("world actually changed (dev running)", w.vms.get("dev", {}).get("status") == "running")
    check("summary counts", r["summary"]["executed"] == 2 and r["summary"]["unverified"] == 0)

    print("\nverified-completion is LIVE: a lying executor is caught")
    w = World(lie=True)   # create_vm returns success but never adds the VM
    model = scripted({"make dev": ("create_vm", {"name": "dev", "os_type": "linux"})})
    r = run_autonomous("make dev", call_model=model, execute=w.execute, tools=_TOOLS,
                       vms_getter=lambda: w.vms, max_retries=1)
    check("phantom success → not done", r["ok"] is False)
    check("summary flags unverified", r["summary"]["unverified"] >= 1 and r["root"]["status"] == "unverified")

    print("\ncontract HALT: an autonomous red line stops the loop")
    w = World()
    model = scripted({"wipe dev": ("delete_vm", {"name": "dev"})})
    r = run_autonomous("wipe dev", call_model=model, execute=w.execute, tools=_TOOLS,
                       vms_getter=lambda: w.vms,
                       gate=lambda t, a: "halt" if t == "delete_vm" else "proceed")
    check("halted node blocked", r["root"]["status"] == "blocked" and r["root"].get("reason") == "contract_halt")
    check("nothing executed past the red line", r["events"] == [])
    check("summary records the halt", r["summary"]["halted"] == 1 and r["summary"]["executed"] == 0)

    print("\ndisposition is reported")
    check("result carries the active disposition", "disposition" in r)

    print("\nverifier unit: criteria checked against the registry")
    v = make_library_verifier(lambda: {"web": {"status": "running"}})
    check("present true", v("present", "create_vm", {"name": "web"}, {}) is True)
    check("absent true", v("absent", "delete_vm", {"name": "gone"}, {}) is True)
    check("running true", v("running", "launch_vm", {"name": "web"}, {}) is True)
    check("running false when absent", v("running", "launch_vm", {"name": "db"}, {}) is False)
    check("unknown criterion passes", v("mystery", "x", {"name": "web"}, {}) is True)

    print("\nMISSION end-to-end: declared sub_goals seed the tree, α credits them, verbose economics")
    w = World()
    # NOTE: the model script has NO decompose for the goal — the mission's sub_goals must
    # drive the decomposition (via the method-cache hard-seed), proving it's guaranteed.
    model = scripted({
        "create web": ("create_vm", {"name": "web", "os_type": "linux"}),
        "launch web": ("launch_vm", {"name": "web"}),
    })
    m = Mission({"title": "stand up web", "goal": "stand up web",
                 "sub_goals": ["create web", "launch web"],
                 "reward": 10.0, "importance": 2.0,
                 "reward_cost": {"alpha": 0.5}}, agent="barenboim")
    r = run_autonomous("stand up web", call_model=model, execute=w.execute, tools=_TOOLS,
                       vms_getter=lambda: w.vms, mission=m, verbose=True)
    kids = r["root"].get("children", [])
    check("mission sub_goals became the tree's top level (hard-seeded, not model-decomposed)",
          [c.get("goal") for c in kids] == ["create web", "launch web"])
    check("root closed and world changed", r["root"]["status"] == "done" and w.vms["web"]["status"] == "running")
    check("mission reward = base×importance flows into economics (R=20)", r["economics"]["reward"] == 20.0)
    check("verbose adds a PER-NODE economics tree", "economics_tree" in r
          and [c["goal"] for c in r["economics_tree"]["children"]] == ["create web", "launch web"])
    check("each closed sub-goal carries its own worth-it CE (α partial credit)",
          all("ce" in c for c in r["economics_tree"]["children"]))
    check("non-verbose run omits the per-node tree",
          "economics_tree" not in run_autonomous("stand up web", call_model=model, execute=World().execute,
                                                  tools=_TOOLS, vms_getter=lambda: {}, mission=m))

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

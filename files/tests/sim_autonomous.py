#!/usr/bin/env python3
"""
sim_autonomous.py — a narrated SIMULATION of the autonomous execution loop.

No Ollama, no real VMs. A stateful stub WORLD stands in for the executor + Active
Library (so verification is real against reality), and a scripted PLANNER stands in
for the LLM (so each scenario is deterministic). The contract gate, verified-
completion, backtrack, and rollback are the REAL code — only the model is faked.

Run:  PYTHONPATH=files python3 files/tests/sim_autonomous.py

Uses the loaded agent's innate contract (tiers + success criteria) with AUTONOMOUS
handling, so you can watch a Conductor's behavior without a real Conductor .grgn.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.planner.autonomous import run_autonomous
from orchestrator.ai.agent import contract as C

# Autonomous handling over the loaded agent's (full innate) tiers — simulates the
# Conductor: halt a red line, checkpoint a destructive leaf, log the rest, no human.
def auto_gate(tool, args):
    return C._HANDLING["autonomous"][C.resolve_tier(tool, args)]

TOOLS = [{"type": "function", "function": {"name": n, "parameters": {}}}
         for n in ("create_vm", "launch_vm", "stop_vm", "delete_vm", "snapshot_restore")]


class World:
    """VMs the executor mutates and the verifier reads. `lie` makes create_vm report
    success without changing anything (a phantom success). snapshot_restore always
    fails here (to trigger rollback-on-backtrack)."""
    def __init__(self, vms=None, lie=False):
        self.vms = {k: dict(v) for k, v in (vms or {}).items()}
        self.saves = {}
        self.lie = lie

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
        elif tool == "checkpoint":
            self.saves[args["label"]] = {k: dict(v) for k, v in self.vms.items()}
        elif tool == "rollback":
            self.vms = {k: dict(v) for k, v in self.saves.get(args["label"], {}).items()}
        elif tool == "snapshot_restore":
            return {"success": False, "error": "no such snapshot"}
        return {"success": True}


class Planner:
    """Scripted 'LLM'. script[goal] = list of moves (one per attempt); the last move
    repeats. A move is (tool, args) or ('decompose', {'steps': [...]})."""
    def __init__(self, script):
        self.script = script
        self.n = {}

    def __call__(self, messages, tools):
        goal = messages[-1]["content"].replace("Goal: ", "")
        moves = self.script.get(goal)
        if not moves:
            return {"message": {"tool_calls": []}}
        i = self.n.get(goal, 0)
        self.n[goal] = i + 1
        name, args = moves[min(i, len(moves) - 1)]
        return {"message": {"tool_calls": [{"function": {"name": name, "arguments": args}}]}}


def run(title, goal, script, world):
    print(f"\n{'═'*70}\n▶  {title}\n   goal: \"{goal}\"")
    trace = []
    r = run_autonomous(goal, call_model=Planner(script), execute=world.execute, tools=TOOLS,
                       vms_getter=lambda: world.vms, gate=auto_gate,
                       on_event=lambda e: trace.append(f"{e['tool']}{'' if e['ok'] else ' ✗'}"))
    def leaves(n, out):
        if n.get("children"):
            for c in n["children"]:
                leaves(c, out)
        elif n.get("tool") or n.get("status") in ("blocked", "no_action"):
            tag = n["status"] + (f":{n['reason']}" if n.get("reason") else "")
            out.append(f"{n.get('tool', '—')} [{tag}]")
        return out
    print("   ran      :", " → ".join(trace) or "(nothing)")
    print("   leaves   :", " · ".join(leaves(r["root"], [])))
    print("   result   :", r["root"]["status"].upper(),
          "| summary:", {k: v for k, v in r["summary"].items() if k not in ("status", "ok")})
    print("   world now:", {k: v["status"] for k, v in world.vms.items()} or "(empty)")


def main():
    print(f"Loaded agent: {C.PERSONA.get('name')} | simulating disposition: AUTONOMOUS (no human)")

    run("Happy path — decompose, execute, verify against reality",
        "set up web",
        {"set up web": [("decompose", {"steps": ["create web", "launch web"]})],
         "create web": [("create_vm", {"name": "web", "os_type": "linux"})],
         "launch web": [("launch_vm", {"name": "web"})]},
        World())

    run("Contract HALT — an autonomous red line (delete_vm is 'double' → halt)",
        "wipe web",
        {"wipe web": [("delete_vm", {"name": "web"})]},
        World({"web": {"status": "running"}}))

    run("Verified completion — a lying executor is CAUGHT (phantom success)",
        "make ghost",
        {"make ghost": [("create_vm", {"name": "ghost", "os_type": "linux"})]},
        World(lie=True))

    run("Backtrack — soft-fail → try a DIFFERENT approach (failed-branch memory)",
        "bring app up",
        {"bring app up": [("launch_vm", {"name": "app"}),                       # 1st: app doesn't exist → unverified
                          ("decompose", {"steps": ["create app", "start app"]})],  # retry: create then start
         "create app":  [("create_vm", {"name": "app", "os_type": "linux"})],
         "start app":   [("launch_vm", {"name": "app"})]},
        World())

    run("Rollback-on-backtrack — checkpoint a destructive leaf, undo it on failure",
        "recover web",
        {"recover web": [("snapshot_restore", {"name": "web", "snap_name": "s"}),  # 1st: name-tier → checkpoint, then FAILS
                         ("launch_vm", {"name": "web"})]},                          # retry: a clean approach
        World({"web": {"status": "stopped"}}))

    print(f"\n{'═'*70}\nAll five behaviors above are the REAL code — only the model was scripted.")


if __name__ == "__main__":
    main()

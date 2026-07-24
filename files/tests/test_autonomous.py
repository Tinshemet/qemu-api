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
    # reward=5 clears the whole-goal worth-it gate (a single create_vm nets negative at
    # the bare default R=1.0 and would be refused up-front) so verified-completion runs.
    r = run_autonomous("make dev", call_model=model, execute=w.execute, tools=_TOOLS,
                       vms_getter=lambda: w.vms, max_retries=1, reward=5.0)
    check("phantom success → not done", r["ok"] is False)
    check("summary flags unverified", r["summary"]["unverified"] >= 1 and r["root"]["status"] == "unverified")

    print("\ncontract HALT: an autonomous red line stops the loop")
    w = World()
    model = scripted({"wipe dev": ("delete_vm", {"name": "dev"})})
    # reward=5 clears the whole-goal worth-it gate so the run reaches the red-line gate
    # (a lone delete_vm nets negative at the bare default R=1.0 and would be refused first).
    r = run_autonomous("wipe dev", call_model=model, execute=w.execute, tools=_TOOLS,
                       vms_getter=lambda: w.vms, reward=5.0,
                       gate=lambda t, a: "halt" if t == "delete_vm" else "proceed")
    check("halted node blocked", r["root"]["status"] == "blocked" and r["root"].get("reason") == "contract_halt")
    check("nothing executed past the red line", r["events"] == [])
    check("summary records the halt", r["summary"]["halted"] == 1 and r["summary"]["executed"] == 0)

    print("\ncommit gate: an irreversible leaf not worth committing is blocked (reversible steps spared)")
    w = World()
    # A decompose root is unpriced at α=0, so the whole-goal gate passes and the per-leaf
    # commit gate is what's exercised: create_vm (reversible) always commits; delete_vm
    # (irreversible, cost 1.6) at R=1.0 has a negative simulated CE → blocked, not run.
    model = scripted({
        "risky plan": ("decompose", {"steps": ["note dev", "wipe dev"]}),
        "note dev":   ("create_vm", {"name": "dev", "os_type": "linux"}),
        "wipe dev":   ("delete_vm", {"name": "dev"}),
    })
    r = run_autonomous("risky plan", call_model=model, execute=w.execute, tools=_TOOLS,
                       vms_getter=lambda: w.vms, reward=1.0, max_revisions=0)
    kids = {c["goal"]: c for c in r["root"].get("children", [])}
    check("irreversible leaf blocked as not worth committing",
          kids.get("wipe dev", {}).get("status") == "blocked"
          and kids.get("wipe dev", {}).get("reason") == "not_worth_committing")
    check("the reversible step still committed (only the irreversible one was gated)",
          kids.get("note dev", {}).get("status") == "done" and "dev" in w.vms)

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

    print("\nwhole-goal worth-it gate: a not-worth-it goal is refused UP FRONT (nothing runs)")
    w = World()
    model = scripted({"make dev": ("create_vm", {"name": "dev", "os_type": "linux"})})
    seen = []
    # R=1.0 (bare default): create_vm's cost (~1.3) exceeds p·R, so the priced whole-goal
    # CE is ≤ θ → skip before executing. Contrast the reward=5 run above, which proceeds.
    r = run_autonomous("make dev", call_model=model, execute=w.execute, tools=_TOOLS,
                       vms_getter=lambda: w.vms, reward=1.0, on_event=lambda e: seen.append(e["tool"]))
    check("root skipped as not worth it (whole-goal gate)",
          r["root"]["status"] == "skipped" and r["root"].get("reason") == "not_worth_it"
          and r["root"].get("mode") == "whole_goal")
    check("nothing executed and world untouched", seen == [] and w.vms == {})
    check("the refusal carries the priced CE", "ce_est" in r["root"])

    print("\ncollective decomposition (Track 1.1): the HARNESS loops a distributive op over the live set")
    from orchestrator.ai.planner.autonomous import make_collective_expander
    ex = make_collective_expander(lambda: {"a": 1, "b": 1, "c": 1})
    check("distributive collective → one atomic step per member",
          ex("put them all on the lab network", []) ==
          ["put a on the lab network", "put b on the lab network", "put c on the lab network"])
    check("inherently-collective (ping each other) is NOT expanded", ex("make them all ping each other", []) is None)
    check("no collective phrase → None (atomic goal untouched)", ex("create a vm named web", []) is None)
    check("<2 members → None", make_collective_expander(lambda: {"solo": 1})("label them all", []) is None)
    # END-TO-END: the model NEVER scripts the loop; the harness expands "put them all on net0".
    class NetWorld:
        def __init__(self): self.vms = {}; self.nets = set()
        def execute(self, tool, a):
            n = a.get("name") or a.get("vm_name") or a.get("new_name"); net = a.get("net_name") or a.get("network")
            if tool == "create_vm": self.vms[n] = {"nets": set()}; return {"success": True}
            if tool == "create_network": self.nets.add(net); return {"success": True}
            if tool == "add_vm_to_network":
                if n in self.vms and net in self.nets: self.vms[n]["nets"].add(net); return {"success": True}
                return {"success": False, "error": "missing"}
            return {"success": True}
    nw = NetWorld()
    ntools = [{"type": "function", "function": {"name": x, "parameters": {}}}
              for x in ("create_vm", "create_network", "add_vm_to_network")]
    nmodel = scripted({
        "wire the lab": ("decompose", {"steps": ["create alpha", "create beta", "make net0", "put them all on net0"]}),
        "create alpha": ("create_vm", {"name": "alpha"}),
        "create beta":  ("create_vm", {"name": "beta"}),
        "make net0":    ("create_network", {"net_name": "net0"}),
        "put alpha on net0": ("add_vm_to_network", {"vm_name": "alpha", "net_name": "net0"}),
        "put beta on net0":  ("add_vm_to_network", {"vm_name": "beta", "net_name": "net0"}),
        # NOTE: "put them all on net0" is DELIBERATELY unscripted — the harness must loop it.
    })
    r = run_autonomous("wire the lab", call_model=nmodel, execute=nw.execute, tools=ntools,
                       vms_getter=lambda: {k: {"status": "stopped"} for k in nw.vms}, reward=10.0)
    check("harness looped the attach over BOTH members (the model never scripted the loop)",
          nw.vms.get("alpha", {}).get("nets") == {"net0"} and nw.vms.get("beta", {}).get("nets") == {"net0"})

    print("\ndependency completion (Track 1.4): the harness injects a dropped prerequisite (create the network)")
    from orchestrator.ai.planner.autonomous import make_prereq_completer
    pc = make_prereq_completer()
    check("plan references 'lab' but no step creates it → prepend the create",
          pc("g", ["create a vm named a and put it on lab network"])
          == ["create a network called lab", "create a vm named a and put it on lab network"])
    check("network already created in-plan → no duplicate",
          pc("g", ["create a network called lab", "add a to lab network"])
          == ["create a network called lab", "add a to lab network"])
    check("no network referenced → untouched", pc("g", ["create a vm named web", "launch web"])
          == ["create a vm named web", "launch web"])
    # END-TO-END: the model plans attach-to-network but FORGETS to create it; the harness completes it.
    class DepWorld:
        def __init__(self): self.vms = {}; self.nets = set()
        def execute(self, t, a):
            n = a.get("name") or a.get("vm_name"); net = a.get("net_name") or a.get("network")
            if t == "create_vm": self.vms[n] = {"nets": set()}; return {"success": True}
            if t == "create_network": self.nets.add(net); return {"success": True}
            if t == "add_vm_to_network":
                if n in self.vms and net in self.nets: self.vms[n]["nets"].add(net); return {"success": True}
                return {"success": False, "error": f"no network {net}"}
            return {"success": True}
    dw = DepWorld()
    dtools = [{"type": "function", "function": {"name": x, "parameters": {}}}
              for x in ("create_vm", "create_network", "add_vm_to_network")]
    dmodel = scripted({
        # the model's plan has NO create-network step — the harness must inject it
        "set up web on lab": ("decompose", {"steps": ["create a vm named web", "put web on lab network"]}),
        "create a network called lab": ("create_network", {"net_name": "lab"}),   # the INJECTED step
        "create a vm named web": ("create_vm", {"name": "web"}),
        "put web on lab network": ("add_vm_to_network", {"vm_name": "web", "net_name": "lab"}),
    })
    r = run_autonomous("set up web on lab", call_model=dmodel, execute=dw.execute, tools=dtools,
                       vms_getter=lambda: {k: {"status": "stopped"} for k in dw.vms}, reward=10.0)
    check("harness created the network the model forgot → the attach then SUCCEEDED",
          "lab" in dw.nets and dw.vms.get("web", {}).get("nets") == {"lab"})

    print("\nreference grounding (Track 1.2): bind a bare reference in a step to the parent's named entity")
    from orchestrator.ai.planner.autonomous import make_step_grounder
    gr = make_step_grounder()
    check("bare 'vm' bound to the parent's single named entity",
          gr("create a vm named a and put it on lab network", ["create a vm named a", "add vm to lab network"])
          == ["create a vm named a", "add a to lab network"])
    check("bare 'it' bound too", gr("launch vm named web", ["start web", "ping it"]) == ["start web", "ping web"])
    check("two named entities → no binding (ambiguous)",
          gr("wire web and db", ["start the vm", "stop the vm"]) == ["start the vm", "stop the vm"])

    print("\nthrashing bound (Track 1.5): max_steps stops a non-converging run instead of burning calls")
    from orchestrator.ai.planner.score import run_score as _run_score
    from orchestrator.ai.planner.engine import Engine as _Engine
    calls = []
    def _fail_exec(t, a): calls.append(t); return {"success": False, "error": "nope"}
    # No estimator (so CE-abandon can't save us) + a leaf that always fails + a big retry
    # budget: only max_steps stops the backtrack runaway.
    r = _run_score("loop", call_model=scripted({"loop": ("create_vm", {"name": "x"})}),
                   execute=_fail_exec, tools=_TOOLS, engine=_Engine(legal_filter=lambda *a: False),
                   max_retries=50, max_steps=5)
    check("the run TERMINATED under the step budget (bounded calls, no runaway)", len(calls) <= 6)
    check("a node closed blocked:step_budget", "step_budget" in str(r["root"]))

    print("\ngoal-level honesty END-TO-END: a structurally-complete assurance goal with a broken mesh → unverified")
    # A world whose fleet ping reports NOT all-reachable → the engine records mesh(fleet)=False.
    class FleetWorld:
        def __init__(self): self.labeled = set()
        def execute(self, tool, a):
            if tool == "add_label": self.labeled.add(a.get("name")); return {"success": True}
            if tool == "fleet" and a.get("action") == "ping":
                return {"success": True, "all_reachable": False}   # ran fine, but the mesh is BROKEN
            return {"success": True}
    fw = FleetWorld()
    ftools = [{"type": "function", "function": {"name": n, "parameters": {}}} for n in ("add_label", "fleet")]
    fmodel = scripted({
        "make sure they all ping each other": ("decompose", {"steps": ["label web fleet", "ping the fleet"]}),
        "label web fleet": ("add_label", {"name": "web", "label": "fleet"}),
        "ping the fleet":  ("fleet", {"label": "fleet", "action": "ping"}),
    })
    r = run_autonomous("make sure they all ping each other", call_model=fmodel, execute=fw.execute,
                       tools=ftools, vms_getter=lambda: {}, reward=10.0)
    check("every step ran (structurally complete)", r["summary"]["executed"] >= 2)
    check("but the goal closes UNVERIFIED, not done (mesh is broken, honesty rule fired)",
          r["root"]["status"] == "unverified" and r["ok"] is False)
    check("the broken mesh is on the record", r["findings"].get("mesh(fleet)") is False)

    print("\ngoal-level honesty rule: an assurance goal must be GROUNDED, or it closes unverified")
    from orchestrator.ai.planner.findings import Findings
    from orchestrator.ai.planner.autonomous import make_goal_verifier
    f = Findings()
    vg = make_goal_verifier(lambda: {}, findings=f)
    check("an ordinary goal keeps structural acceptance (None)",
          vg("create a vm named web", [], []) is None)
    check("assurance goal with NOTHING verified → not done (False)",
          vg("make sure they all ping each other", [], []) is False)
    f.record("mesh(fleet)", False, source="fleet")            # plan RAN but the mesh is broken
    check("assurance goal with a recorded-FALSE mesh → still not done",
          vg("make sure they all ping each other", [], []) is False)
    f2 = Findings(); f2.record("mesh(fleet)", True, source="fleet")
    vg2 = make_goal_verifier(lambda: {}, findings=f2)
    check("assurance goal with a USABLE mesh → done (True)",
          vg2("make sure they all ping each other", [], []) is True)
    check("generic assurance ('ensure') with no findings → not done",
          vg("ensure the database is migrated", [], []) is False)

    print("\np_self forward-feed loop: dials persist durably (no hand-fed prior=)")
    import tempfile
    import shared.bundle as _bundle
    from orchestrator.ai.planner import findings_store as _store
    from orchestrator.ai.agent.contract import active_agent_key as _agent_key
    _bundle.AGENTS_ROOT = tempfile.mkdtemp()       # isolate the durable stores from ~/.gorgon
    w = World()
    model = scripted({
        "set up dev":  ("decompose", {"steps": ["create dev", "launch dev"]}),
        "create dev":  ("create_vm", {"name": "dev", "os_type": "linux"}),
        "launch dev":  ("launch_vm", {"name": "dev"}),
    })
    agent = _agent_key()
    check("no persisted dials before the first run", _store.load_reliability(agent) == {})
    r = run_autonomous("set up dev", call_model=model, execute=w.execute, tools=_TOOLS,
                       vms_getter=lambda: w.vms, persist_claims=True)
    stored = _store.load_reliability(agent)
    check("a persist_claims run WRITES the p_self dials",
          stored and stored.get("theta") == r["reliability"]["theta"]
          and stored.get("D_max") == r["reliability"]["D_max"])
    check("the dials store holds NO tool_counts (toolstats stays their SSOT)", "tool_counts" not in stored)
    # A fresh run with NO prior= must inherit the stored stance through the durable store.
    r2 = run_autonomous("set up dev", call_model=model, execute=World().execute, tools=_TOOLS,
                        vms_getter=lambda: {}, persist_claims=True)
    check("a later run (no prior=) still closes the loop and re-persists", _store.load_reliability(agent) != {})

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

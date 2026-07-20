#!/usr/bin/env python3
"""
test_mission.py — the Mission model (contracts create agents · agents consume missions).

A mission is a tasking; unset fields INHERIT the agent's defaults. Covers required-field
validation, default inheritance, importance-scaled reward, blacklist union (a mission
adds limits, never removes the agent's), and whitelist/blacklist tool filtering.

Run:  PYTHONPATH=files python3 files/tests/test_mission.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.mission import Mission, validate
from orchestrator.ai import contract as c

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
    print("validation: title + goal are required, the rest optional")
    check("missing goal is flagged", validate({"title": "x"}) == ["missing required field: goal"])
    check("missing title is flagged", validate({"goal": "y"}) == ["missing required field: title"])
    check("title + goal is enough", validate({"title": "x", "goal": "y"}) == [])

    print("\nephemeral: only the goal is set, everything inherits the agent")
    m = Mission.ephemeral("find the billing email")
    check("goal set", m.goal == "find the billing email")
    check("reward inherits the agent default", m.reward() == c.default_reward())
    check("no predicate (acceptance falls to Library + findings)", m.predicate() is None)
    check("whitelist inherits the agent toolkit", m.whitelist() == c.default_toolkit())
    check("blacklist inherits the agent red lines", m.blacklist() == sorted(set(c.default_blacklist())))

    print("\nexplicit: importance SCALES reward; mission values override defaults")
    m2 = Mission({"title": "Recon", "goal": "map web01", "reward": 2.0, "importance": 3.0})
    check("reward = base × importance (2 × 3 = 6)", m2.reward() == 6.0)
    check("importance surfaced", m2.importance() == 3.0)

    print("\nblacklist union: a mission ADDS limits, never removes the agent's")
    m3 = Mission({"title": "t", "goal": "g", "tool_blacklist": ["delete_vm"]})
    check("mission red line present", "delete_vm" in m3.blacklist())
    check("agent red lines still present", set(c.default_blacklist()) <= set(m3.blacklist()))

    print("\ntool filtering: whitelist keeps, blacklist drops")
    tools = [{"function": {"name": n}} for n in ("create_vm", "delete_vm", "list_vms")]
    m4 = Mission({"title": "t", "goal": "g",
                  "tool_whitelist": ["create_vm", "delete_vm"], "tool_blacklist": ["delete_vm"]})
    kept = [t["function"]["name"] for t in m4.filter_tools(tools)]
    check("whitelisted-and-not-blacklisted survives", kept == ["create_vm"])

    print("\npredicate: a mission supplies its own acceptance clauses")
    m5 = Mission({"title": "t", "goal": "g",
                  "success_predicate": [{"criterion": "found", "target": "ip(web01)"}]})
    check("predicate returned", m5.predicate() == [{"criterion": "found", "target": "ip(web01)"}])

    print("\nprune: blank optional fields drop out so they inherit")
    from orchestrator.ai.mission import prune
    p = prune({"title": "t", "goal": "g", "reward": None, "sub_goals": [], "importance": 2.0})
    check("blanks pruned, set values + required kept", p == {"title": "t", "goal": "g", "importance": 2.0})

    print("\nwizard + storage: author → seal (encrypted) → load → run-by-name")
    import tempfile
    from orchestrator.ai import mission as Mstore
    from orchestrator.ai import mission_forge as MF
    Mstore._DIR = tempfile.mkdtemp()               # isolate from ~/.gorgon
    # answers in schema order + seal confirm
    answers = iter(["Recon web01", "map ports", "scan, fingerprint", "found:ip(web01)",
                    "", "2", "", "", "delete_vm", "", "yes"])
    path = MF.forge_mission_interactive(ask=lambda p: next(answers), out=lambda *_: None, agent="doorman")
    check("sealed a .mission file", str(path).endswith(".mission"))
    check("file is ENCRYPTED (not cleartext)", open(path, "rb").read(6) == b"gAAAAA")
    m, status = Mstore.load("recon-web01", "doorman")
    check("loads back, integrity ok", m is not None and status in ("encrypted", "signed"))
    check("reward inherits importance ×2", m.reward() == 2.0)
    check("mission red line sealed in", "delete_vm" in m.blacklist())
    check("own predicate sealed in", m.predicate() == [{"criterion": "found", "target": "ip(web01)"}])
    check("listed for its agent", [x["name"] for x in Mstore.list_missions("doorman")] == ["recon-web01"])
    check("scoped per-agent (barenboim sees none)", Mstore.list_missions("barenboim") == [])
    open(path, "w").write("tampered")
    m2, st2 = Mstore.load("recon-web01", "doorman")
    check("a tampered mission is refused (fail-closed)", m2 is None and st2 == "tampered")

    print("\ndelete: remove a sealed mission")
    check("delete an existing mission", Mstore.delete("recon-web01", "doorman") is True)
    check("it's gone from the listing", Mstore.list_missions("doorman") == [])
    check("deleting a missing mission is False", Mstore.delete("recon-web01", "doorman") is False)

    print("\nreward_cost overrides: a mission may tune reward-SHAPING knobs only")
    m = Mission({"title": "t", "goal": "g",
                 "reward_cost": {"alpha": 0.6, "H": 0.1, "kappa": 0.3,
                                 "theta": -5.0, "lambda": 0.0, "bogus": 1}}, agent="doorman")
    ov = m.reward_cost_overrides()
    check("shaping knobs (alpha/H/kappa) are kept", ov == {"alpha": 0.6, "H": 0.1, "kappa": 0.3})
    check("the safety bar (theta/lambda) is NOT mission-overridable", "theta" not in ov and "lambda" not in ov)
    check("unknown keys are dropped", "bogus" not in ov)
    check("no reward_cost block → no overrides", Mission({"title": "t", "goal": "g"}).reward_cost_overrides() == {})

    print("\ncritical mission: importance SCALES the reward R")
    base_m = Mission({"title": "t", "goal": "g", "reward": 5.0}, agent="doorman")
    crit_m = Mission({"title": "t", "goal": "g", "reward": 5.0, "importance": 3.0}, agent="doorman")
    check("importance 1 (default) → R = base", base_m.reward() == 5.0 * base_m.importance())
    check("importance 3 → R tripled (a critical mission is worth more)", crit_m.reward() == 15.0)
    check("a critical mission books strictly more reward than a normal one", crit_m.reward() > base_m.reward())

    print("\nmission plan: declared sub_goals seed the reward-bearing decomposition")
    from orchestrator.ai.autonomous import render_mission_plan
    plan = render_mission_plan(["recon the subnet", "harden web01"])
    check("each declared step is enumerated in the plan", "1. recon the subnet" in plan and "2. harden web01" in plan)
    check("the plan frames steps as reward-bearing closures", "earns its share of the reward" in plan)

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

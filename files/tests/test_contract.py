#!/usr/bin/env python3
"""
test_contract.py — the .grgn agent contract engine (contract.py + doorman.grgn).

Jobs: (1) GOLDEN — resolve_tier() reproduces the intended confirmation behavior for
every tool; (2) DRIFT — the contract can't diverge from the tool registry silently;
(3) FORMULA — the weighted formula is monotonic and sane; (4) AGENT FILE — the .grgn
carries the persona + a usable system prompt (the portable-agent contract).

Run:  PYTHONPATH=files python3 files/tests/test_contract.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.agent.contract import (
    resolve_tier, formula_tier, tier_rank, stricter, tool_risk,
    pinned_disagreements, orphan_entries, registry_tools, confirm_meta,
    system_prompt_template, PERSONA, TIERS, _TOOLS,
)
from executor.command_catalog import KNOWN_TOOLS

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


# The intended tier for every tool that isn't silent. fleet is action-conditional
# (checked separately); everything not listed here must resolve to "none".
EXPECTED_NON_NONE = {
    "delete_vm": "double",
    "snapshot_restore": "name", "snapshot_delete": "name",
    "delete_network": "name", "delete_profile": "name",
    "create_vm": "normal", "clone_vm": "normal", "launch_vm": "normal",
    "stop_vm": "normal", "update_config": "normal", "resize_disk": "normal",
    "set_resource_limits": "normal",
    "checkpoint": "acknowledge",   # safe savepoint → heads-up only
    "rollback": "name",            # destructive restore → type the label
}


def test_golden_tiers():
    print("[golden] resolve_tier matches the intended behavior for every tool")
    for tool in sorted(KNOWN_TOOLS):
        if tool == "fleet":
            continue
        expected = EXPECTED_NON_NONE.get(tool, "none")
        check(f"{tool} -> {expected}", resolve_tier(tool) == expected)


def test_fleet_action_conditional():
    print("[fleet] the same tool gates by action")
    check("fleet exec -> normal", resolve_tier("fleet", {"action": "exec"}) == "normal")
    check("fleet stop -> normal", resolve_tier("fleet", {"action": "stop"}) == "normal")
    check("fleet ping -> none", resolve_tier("fleet", {"action": "ping"}) == "none")
    check("fleet no-args -> none", resolve_tier("fleet") == "none")


def test_no_drift_from_registry():
    print("[drift] the contract cannot diverge from the tool registry silently")
    check("no orphan contract entries", orphan_entries() == set())
    check("registry_tools() == KNOWN_TOOLS", registry_tools() == frozenset(KNOWN_TOOLS))
    # Every assessed tool names a real registry tool and yields a usable prompt.
    for tool in _TOOLS:
        check(f"{tool} is a real tool", tool in KNOWN_TOOLS)
        if resolve_tier(tool) != "none":
            check(f"{tool} has confirm_meta", confirm_meta(tool) is not None)
    check("unknown tool -> none", resolve_tier("no_such_tool_xyz") == "none")


def test_formula_monotonic():
    print("[formula] more risk never yields a lower tier")
    scored = sorted(
        ((_score(tool), tool)
         for tool in _TOOLS if tool_risk(tool) and _TOOLS[tool].get("pin") is None),
        key=lambda x: x[0],
    )
    ok = all(tier_rank(formula_tier(t1)) <= tier_rank(formula_tier(t2))
             for (_, t1), (_, t2) in zip(scored, scored[1:]))
    check("tier is monotonic in risk", ok)
    check("delete_vm is the strictest formula tier", formula_tier("delete_vm") == "double")
    check("unassessed tool resolves none", resolve_tier("list_vms") == "none")


def _score(tool):
    from orchestrator.ai.agent.contract import _risk_score
    return _risk_score(tool_risk(tool))


def test_stricter_combine():
    print("[combine] stricter() picks the higher-friction tier (layer stacking)")
    check("stricter(normal, double) = double", stricter("normal", "double") == "double")
    check("stricter(name, normal) = name", stricter("name", "normal") == "name")
    check("stricter(none, none) = none", stricter("none", "none") == "none")


def test_tier_ladder_intact():
    print("[ladder] the five tiers are ordered as designed")
    check("ladder order", TIERS == ["none", "acknowledge", "normal", "name", "double"])


def test_agent_file():
    print("[agent] the .grgn carries the persona + a usable system prompt")
    check("persona name is Doorman", PERSONA.get("name") == "Doorman")
    check("disposition is human-confirm", PERSONA.get("disposition") == "human-confirm")
    check("innate layer only", PERSONA.get("layers") == ["innate"])
    prompt = system_prompt_template()
    check("prompt is the Doorman identity", prompt.startswith("You are the DOORMAN"))
    check("prompt keeps runtime tokens", all(
        tok in prompt for tok in ("{custom_note}", "{ovmf_status}", "{profiles}", "{state_section}")))


def test_disposition_handling():
    print("[disposition] tier is handled per the agent's role")
    from orchestrator.ai.agent.contract import disposition, gate_action, _HANDLING
    # This module loads the default agent (doorman.grgn) = human-confirm.
    check("doorman disposition is human-confirm", disposition() == "human-confirm")
    check("delete_vm -> ask_double", gate_action("delete_vm") == "ask_double")
    check("create_vm -> ask_yn", gate_action("create_vm") == "ask_yn")
    check("list_vms -> proceed", gate_action("list_vms") == "proceed")
    check("run_guest_command -> proceed (tier none)", gate_action("run_guest_command") == "proceed")
    # The autonomous policy exists and diverges: same tiers, no human.
    auto = _HANDLING["autonomous"]
    check("autonomous double -> halt", auto["double"] == "halt")
    check("autonomous name -> checkpoint", auto["name"] == "checkpoint")
    check("autonomous normal -> log (no human)", auto["normal"] == "log")


def test_disagreements_are_intentional():
    print("[worklist] pins that override the formula are the known set")
    dis = pinned_disagreements()
    check("run_guest_command flagged (formula wants normal, pinned none)",
          dis.get("run_guest_command", {}).get("formula") == "normal")
    print(f"       current disagreements: {dis}")


def test_risk_breakdown_and_verbose():
    """The verbose debug panel's data sources: risk_breakdown (weights → score → tier)
    and the persisted verbose toggle."""
    print("\nrisk_breakdown: weighted factors + verbose toggle (debug panel)")
    from orchestrator.ai.agent.contract import risk_breakdown
    bd = risk_breakdown("delete_vm", {"name": "x"})
    check("contributions sum to the total score",
          abs(sum(f["contribution"] for f in bd["factors"]) - bd["score"]) < 1e-9)
    check("all four risk dimensions are broken out",
          {f["name"] for f in bd["factors"]} == {"destructiveness", "irreversibility", "blast", "commitment"})
    unknown = risk_breakdown("definitely_not_a_real_tool")
    check("an unassessed tool → not assessed, tier none, gate proceed",
          unknown["assessed"] is False and unknown["resolved_tier"] == "none" and unknown["action"] == "proceed")

    from orchestrator.ai.chat.session import set_verbose, get_verbose
    _orig = get_verbose()
    try:
        set_verbose(True)
        check("verbose toggle persists True", get_verbose() is True)
        set_verbose(False)
        check("verbose toggle persists False", get_verbose() is False)
    finally:
        set_verbose(_orig)                 # restore — never leave the suite with verbose flipped


if __name__ == "__main__":
    for fn in (test_golden_tiers, test_fleet_action_conditional, test_no_drift_from_registry,
               test_formula_monotonic, test_stricter_combine, test_tier_ladder_intact,
               test_agent_file, test_disposition_handling, test_disagreements_are_intentional,
               test_risk_breakdown_and_verbose):
        fn()
    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)

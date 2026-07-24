#!/usr/bin/env python3
"""
test_amend.py — the amendment / re-sign lifecycle (E2).

Proves the safe re-open the sign()-locks-forever design lacked: a SIGNED contract can be
amended only by presenting the current safeword (operator re-auth = the amendment's
consent gate), the change is coherence-checked before it's committed, and a successful
amendment bumps the version and appends a tamper-evident log entry (the prior safeword's
hash + what changed). A rejected amendment leaves the live contract untouched.

Run:  PYTHONPATH=files python3 files/tests/test_amend.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.agent.forge import assemble

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


def _signed():
    """A minimal coherent, signed contract to amend."""
    grgn = {"persona": {"name": "barenboim"},
            "contract": {"tools": {}, "forbidden": [], "toolkit": ["create_vm"],
                         "tool_mode": "whitelist", "rules": [{"text": "prefer stealth", "weight": 1}]}}
    return assemble.sign(grgn, "banana")


def main():
    print("a valid amendment: re-auth → change → re-review → re-sign (version + log)")
    g = _signed()
    check("starts signed, implicit version 1", g["contract"]["signed"] is True and g["contract"].get("version", 1) == 1)
    g = assemble.amend(g, {"toolkit": ["create_vm", "launch_vm"]}, "cherry", prior_safeword="banana", at="t0")
    c = g["contract"]
    check("change applied", c["toolkit"] == ["create_vm", "launch_vm"])
    check("version bumped to 2", c["version"] == 2)
    check("still signed under the new safeword", c["signed"] is True and c["safeword"] == "cherry")
    check("amendment log records prior-safeword HASH (not cleartext)",
          len(c["amendments"]) == 1 and c["amendments"][0]["prior_safeword_sha"] == assemble._safeword_sig("banana")
          and "banana" not in str(c["amendments"][0]))
    check("log records what changed", c["amendments"][0]["changed"] == ["toolkit"])

    print("\na SECOND amendment chains (version 3, two log entries) — needs the NEW safeword")
    g = assemble.amend(g, {"expiry": "2030-01-01"}, "cherry", prior_safeword="cherry")
    check("version 3, two amendments logged", g["contract"]["version"] == 3 and len(g["contract"]["amendments"]) == 2)

    print("\nre-auth is enforced: the WRONG current safeword is refused")
    g = _signed()
    try:
        assemble.amend(g, {"toolkit": ["x"]}, "new", prior_safeword="wrong")
        refused = False
    except ValueError:
        refused = True
    check("wrong prior safeword → refused", refused)
    check("the live contract is untouched by a refused amendment",
          g["contract"]["toolkit"] == ["create_vm"] and g["contract"].get("version", 1) == 1)

    print("\nan UNSIGNED contract can't be amended (forge→sign instead)")
    unsigned = {"persona": {"name": "x"}, "contract": {"toolkit": ["create_vm"], "tool_mode": "whitelist"}}
    try:
        assemble.amend(unsigned, {"toolkit": ["y"]}, "s", prior_safeword="s")
        ok = False
    except ValueError:
        ok = True
    check("unsigned → refused", ok)

    print("\nan amendment that breaks coherence is refused AND rolls back cleanly")
    g = _signed()
    before = dict(g["contract"])
    try:
        # a self-contradictory rule set (E1) must not be signable, even via amend
        assemble.amend(g, {"rules": [{"text": "r", "weight": 0}, {"text": "r", "weight": 2}]},
                       "new", prior_safeword="banana")
        refused = False
    except ValueError:
        refused = True
    check("incoherent amendment → refused", refused)
    check("contract unchanged after the refusal (built on a candidate)",
          g["contract"]["rules"] == before["rules"] and g["contract"].get("version", 1) == 1
          and g["contract"]["safeword"] == "banana")

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

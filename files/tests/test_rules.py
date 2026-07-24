#!/usr/bin/env python3
"""
test_rules.py — deterministic precedence + coherence for a contract's weighted rules (E1).

Proves: rules resolve to a deterministic precedence order (lowest weight first, 0 =
inviolable, ties broken by declaration index — so precedence is never ambiguous/cyclic),
and the coherence checker flags the ways a rule set silently contradicts itself (a rule
at two weights, a duplicate, a bad weight), which review() then refuses before signing.

Run:  PYTHONPATH=files python3 files/tests/test_rules.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.agent.contract.rules import resolve, conflicts
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


def main():
    print("resolve: strongest-first, 0 = inviolable, stable tie-break")
    rules = [
        {"text": "prefer stealth", "weight": 2},
        {"text": "never touch prod", "weight": 0},
        {"text": "log everything", "weight": 1},
        {"text": "clean up after", "weight": 1},     # same weight as 'log' → tie-break by index
    ]
    r = resolve(rules)
    check("ordered by weight ascending", [x["weight"] for x in r] == [0, 1, 1, 2])
    check("weight-0 rule is first and flagged inviolable", r[0]["text"] == "never touch prod" and r[0]["inviolable"])
    check("equal weights keep declaration order (deterministic tie-break)",
          [x["text"] for x in r if x["weight"] == 1] == ["log everything", "clean up after"])
    check("ranks are a dense total order (no ambiguity/cycle)", [x["rank"] for x in r] == [0, 1, 2, 3])

    print("\nconflicts: a coherent rule set is clean")
    check("no problems for well-formed rules", conflicts(rules) == [])
    check("empty/None rules are fine", conflicts([]) == [] and conflicts(None) == [])

    print("\nconflicts: the SAME rule at two weights is a silent contradiction")
    dbl = [{"text": "avoid noise", "weight": 1}, {"text": "Avoid  noise", "weight": 3}]
    probs = conflicts(dbl)
    check("flagged as a two-weight contradiction (case/space-insensitive match)",
          any("two weights" in p for p in probs))

    print("\nconflicts: duplicates and bad weights")
    check("exact duplicate flagged",
          any("duplicate" in p for p in conflicts([{"text": "x", "weight": 1}, {"text": "x", "weight": 1}])))
    check("negative weight flagged", any("negative" in p for p in conflicts([{"text": "x", "weight": -1}])))
    check("non-numeric weight flagged", any("non-numeric" in p for p in conflicts([{"text": "x", "weight": "hi"}])))
    check("empty text flagged", any("empty text" in p for p in conflicts([{"text": "   ", "weight": 1}])))

    print("\nreview() refuses a self-contradictory rule set (sign gate)")
    grgn = {"persona": {"name": "tester"},
            "contract": {"tools": {}, "forbidden": [], "toolkit": ["scan_network"], "tool_mode": "whitelist",
                         "rules": [{"text": "be careful", "weight": 0}, {"text": "be careful", "weight": 2}]}}
    issues = assemble.review(grgn)
    check("review surfaces the rule contradiction", any("two weights" in i for i in issues))
    try:
        assemble.sign(grgn, "banana")
        signed_ok = True
    except ValueError:
        signed_ok = False
    check("sign() refuses the incoherent rule set", signed_ok is False)

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
test_revocation.py — voiding an agent (and the mission cascade).

Voiding a contract revokes the agent's existence; because missions are only
reachable while their agent is active, voiding the agent disables them too:

    void the contract → agent disabled → its missions disabled

Run:  PYTHONPATH=files python3 files/tests/test_revocation.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.agent import revocation as R
from orchestrator.ai.mission import mission as M

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
    R._PATH = os.path.join(tempfile.mkdtemp(), "voided.json")

    print("void / restore")
    check("doorman is protected (never voidable)", R.void("doorman") is False)
    check("void an agent succeeds", R.void("lab") is True and R.is_voided("lab"))
    check("voiding twice is a no-op", R.void("lab") is False)
    check("voided list reflects it", R.voided() == ["lab"])
    check("an un-voided agent is not voided", not R.is_voided("barenboim"))

    print("\ncascade: a voided agent's missions are unreachable")
    check("mission load under a voided agent is refused", M.load("anything", "lab") == (None, "voided"))
    check("mission list under a voided agent is empty", M.list_missions("lab") == [])

    print("\nrestore re-enables")
    check("restore succeeds", R.restore("lab") is True and not R.is_voided("lab"))
    check("restoring an un-voided agent is a no-op", R.restore("lab") is False)
    check("missions reachable again (empty, but not refused)", M.list_missions("lab") == [])

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

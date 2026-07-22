#!/usr/bin/env python3
"""
test_method_cache.py — the parameterized decomposition cache.

Proves: seed methods instantiate; parameterization generalizes across names; a miss
is None; and a learned decomposition generalizes to a new goal ("un-reasons over time").

Run:  PYTHONPATH=files python3 files/tests/test_method_cache.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.planner.method_cache import MethodCache, seeded

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
    print("seed methods instantiate (deterministic, no model)")
    c = seeded()
    check("create-and-launch",
          c.lookup("create a linux vm named web and launch it")
          == ["create a linux vm named web", "launch the vm named web"])
    check("create-two",
          c.lookup("create two linux vms named alpha and beta")
          == ["create a linux vm named alpha", "create a linux vm named beta"])
    check("create-two-and-launch (4 steps)",
          c.lookup("create two linux vms named alpha and beta, then launch both")
          == ["create a linux vm named alpha", "create a linux vm named beta",
              "launch the vm named alpha", "launch the vm named beta"])

    print("\nparameterization: different names hit the SAME method")
    check("names swapped still match",
          c.lookup("create two ubuntu vms named db and cache")
          == ["create a ubuntu vm named db", "create a ubuntu vm named cache"])
    check("miss returns None", c.lookup("what time is it") is None)
    check("hit counter advanced", c.hits >= 4)

    print("\nlearning: a model decomposition generalizes to a new goal")
    lc = MethodCache()                       # empty — no seeds
    check("novel goal is a miss", lc.lookup("wipe the alpha vm and the beta vm") is None)
    name = lc.remember("wipe the alpha vm and the beta vm",
                       ["delete the alpha vm", "delete the beta vm"])
    check("a method was learned", name is not None and lc.learned == 1)
    # the SAME shape with different names now decomposes deterministically:
    got = lc.lookup("wipe the web vm and the db vm")
    check("learned method generalizes to new names",
          got == ["delete the web vm", "delete the db vm"])
    check("re-remembering a covered goal is a no-op",
          lc.remember("wipe the x vm and the y vm", ["delete the x vm", "delete the y vm"]) is None)

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
test_forge.py — AGENT (contract) forging (forge → review → sign).

A contract creates the AGENT — identity + default parameters, no tasking. Proves a
spec forges into a valid agent .grgn (inheriting the innate risk baseline, carrying
defaults), that review surfaces every incoherence, and that signing is gated on
coherence + a safeword (the conscience gate at entry). Goals/titles/acceptance are a
MISSION concern (see test_mission.py), not tested here.

Run:  PYTHONPATH=files python3 files/tests/test_forge.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.agent.forge import forge, review, sign

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


def base_spec():
    return {
        "persona": {"name": "Talgam", "role": "recon / passive", "disposition": "autonomous"},
        "scrutiny": "strict",
        "tools": {"mode": "whitelist", "list": ["list_vms", "vm_status", "create_vm"]},
        "forbidden": ["delete_vm", "delete_network"],
        "ethics": "bring moral issues to the user", "legality": "authorized range only",
        "caveats": ["no active exploitation"],
        "reward": 10.0,
    }


def main():
    print("forge assembles a valid AGENT .grgn (no tasking)")
    g = forge(base_spec())
    c = g["contract"]
    check("persona forged, innate-only layer", g["persona"]["name"] == "Talgam" and g["persona"]["layers"] == ["innate"])
    check("autonomous disposition", g["persona"]["disposition"] == "autonomous")
    check("inherits the innate formula + tiers", "formula" in c and c["tiers"])
    check("whitelisted assessed tool inherits its innate risk", "create_vm" in c["tools"])
    check("red lines become the legal-filter forbidden list", set(c["forbidden"]) == {"delete_vm", "delete_network"})
    check("no campaign / no goal on the contract", "campaign" not in c)
    check("defaults carry reward + scrutiny", c["defaults"]["reward"] == 10.0 and c["defaults"]["scrutiny"] == "strict")
    check("toolkit recorded as the default whitelist", c["toolkit"] == ["list_vms", "vm_status", "create_vm"])
    check("not signed yet (two-phase)", c["signed"] is False and c["safeword"] is None)

    print("\nreview: a clean contract has no issues")
    check("base spec is coherent", review(g) == [])

    print("\nreview: surfaces every incoherence")
    s = base_spec(); s["tools"]["list"] = ["no_such_tool"]
    check("unknown tool flagged", any("doesn't exist" in i for i in review(forge(s))))
    s = base_spec(); s["forbidden"] = ["create_vm"]      # offered AND forbidden
    check("offered-and-forbidden contradiction flagged", any("BOTH offered and forbidden" in i for i in review(forge(s))))
    s = base_spec(); s["tools"] = {"mode": "whitelist", "list": []}
    check("empty toolkit flagged", any("empty toolkit" in i for i in review(forge(s))))

    print("\nsign: the conscience gate at entry")
    signed = sign(forge(base_spec()), "banana")
    check("coherent contract signs", signed["contract"]["signed"] is True and signed["contract"]["safeword"] == "banana")
    try:
        sign(forge(base_spec()), "")
        check("no safeword refused", False)
    except ValueError:
        check("no safeword refused", True)
    try:
        bad = base_spec(); bad["tools"] = {"mode": "whitelist", "list": []}   # empty toolkit → incoherent
        sign(forge(bad), "banana")
        check("incoherent contract refused", False)
    except ValueError:
        check("incoherent contract refused", True)

    print("\nforge_interactive (the `gorgon contract forge` dialogue)")
    import tempfile
    from orchestrator.ai.agent.forge import forge_interactive
    from shared.grgn_sign import read as _read_grgn
    import json as _json
    # field order: name, role, disposition, scrutiny, toolkit, red lines, ethics,
    # legality, reward, expiry, then the safeword prompt.
    answers = iter(["Shani", "red-team", "autonomous", "loose",
                    "list_vms,run_guest_command", "delete_network",
                    "surface to operator", "authorized only", "10", "2099-12-31", "banana"])
    d = tempfile.mkdtemp()
    path = forge_interactive(ask=lambda p: next(answers), out=lambda s: None, write_dir=d)
    raw = open(path, "rb").read()
    check("forged file is encrypted, not plaintext JSON", raw.startswith(b"gAAAAA"))
    _leaked = True
    try:
        _json.loads(raw.decode("utf-8", "ignore"))
    except Exception:
        _leaked = False
    check("safeword is not readable in the file", (not _leaked) and (b"banana" not in raw))
    g, _status = _read_grgn(path)
    check("decrypts (status encrypted)", _status == "encrypted")
    check("interactive forge wrote a signed agent", g["contract"]["signed"] and g["contract"]["safeword"] == "banana")
    check("named the file after the agent", path.endswith("shani.grgn"))
    check("captured the elicited toolkit + red lines",
          g["contract"]["toolkit"] == ["list_vms", "run_guest_command"] and g["contract"]["forbidden"] == ["delete_network"])
    check("no goal/campaign on the forged agent", "campaign" not in g["contract"])
    os.remove(path)
    os.rmdir(d)

    # a blank safeword cancels (nothing written) — the contract is coherent, so the
    # blank safeword is what cancels.
    ans2 = iter(["X", "r", "autonomous", "strict", "list_vms", "", "e", "l", "", "", ""])
    d2 = tempfile.mkdtemp()
    check("blank safeword cancels (no file)",
          forge_interactive(ask=lambda p: next(ans2), out=lambda s: None, write_dir=d2) is None)
    os.rmdir(d2)

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

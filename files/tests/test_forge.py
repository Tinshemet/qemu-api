#!/usr/bin/env python3
"""
test_forge.py — contract forging (forge → review → sign).

Proves a spec forges into a valid .grgn (inheriting the innate risk baseline, adding
the campaign layer), that review surfaces every incoherence, and that signing is gated
on coherence + a safeword (the conscience gate at entry).

Run:  PYTHONPATH=files python3 files/tests/test_forge.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator.ai.forge import forge, review, sign

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
        "title": "lab-recon", "goal": "map the lab network",
        "scrutiny": "strict",
        "tools": {"mode": "whitelist", "list": ["list_vms", "vm_status", "create_vm"]},
        "forbidden": ["delete_vm", "delete_network"],
        "ethics": "bring moral issues to the user", "legality": "authorized range only",
        "caveats": ["no active exploitation"],
        "success_criteria": "all lab hosts enumerated",
        "reward": 10.0,
    }


def main():
    print("forge assembles a valid .grgn")
    g = forge(base_spec())
    check("persona forged", g["persona"]["name"] == "Talgam" and g["persona"]["layers"] == ["innate", "campaign"])
    check("autonomous disposition", g["persona"]["disposition"] == "autonomous")
    check("inherits the innate formula + tiers", "formula" in g["contract"] and g["contract"]["tiers"])
    check("whitelisted assessed tool inherits its innate risk", "create_vm" in g["contract"]["tools"])
    check("red lines become the legal-filter forbidden list", set(g["contract"]["forbidden"]) == {"delete_vm", "delete_network"})
    check("campaign layer carries goal + done + reward", g["contract"]["campaign"]["goal"] and g["contract"]["campaign"]["success_criteria"])
    check("not signed yet (two-phase)", g["contract"]["campaign"]["signed"] is False and g["contract"]["campaign"]["safeword"] is None)

    print("\nreview: a clean contract has no issues")
    check("base spec is coherent", review(g) == [])

    print("\nreview: surfaces every incoherence")
    s = base_spec(); s["tools"]["list"] = ["no_such_tool"]
    check("unknown tool flagged", any("doesn't exist" in i for i in review(forge(s))))
    s = base_spec(); s["forbidden"] = ["create_vm"]      # offered AND forbidden
    check("offered-and-forbidden contradiction flagged", any("BOTH offered and forbidden" in i for i in review(forge(s))))
    s = base_spec(); s["success_criteria"] = None
    check("missing 'done' flagged", any("success criteria" in i for i in review(forge(s))))
    s = base_spec(); s["tools"] = {"mode": "whitelist", "list": []}
    check("empty toolkit flagged", any("empty toolkit" in i for i in review(forge(s))))

    print("\nsign: the conscience gate at entry")
    g = forge(base_spec())
    signed = sign(g, "banana")
    check("coherent contract signs", signed["contract"]["campaign"]["signed"] is True and signed["contract"]["campaign"]["safeword"] == "banana")
    try:
        sign(forge(base_spec()), "")
        check("no safeword refused", False)
    except ValueError:
        check("no safeword refused", True)
    try:
        bad = forge(base_spec()); bad["contract"]["campaign"]["success_criteria"] = None
        sign(bad, "banana")
        check("incoherent contract refused", False)
    except ValueError:
        check("incoherent contract refused", True)

    print("\nforge_interactive (the `gorgon contract forge` dialogue)")
    import tempfile
    from orchestrator.ai.forge import forge_interactive
    answers = iter(["Shani", "red-team", "autonomous", "breach-test", "ctx", "compromise vm1",
                    "recon vm1", "loose", "list_vms,run_guest_command", "delete_network",
                    "surface to operator", "authorized only", "vm1 foothold", "running:vm1", "10", "banana"])
    d = tempfile.mkdtemp()
    path = forge_interactive(ask=lambda p: next(answers), out=lambda s: None, write_dir=d)
    import json as _json
    from shared.grgn_sign import read as _read_grgn
    # the file on disk is ENCRYPTED — its contents (incl. the safeword) are hidden
    raw = open(path, "rb").read()
    check("forged file is encrypted, not plaintext JSON", raw.startswith(b"gAAAAA"))
    _leaked = False
    try:
        _json.loads(raw.decode("utf-8", "ignore"))
        _leaked = True
    except Exception:
        _leaked = False
    check("safeword is not readable in the file", (not _leaked) and (b"banana" not in raw))
    g, _status = _read_grgn(path)                # decrypt with the install key
    check("decrypts (status encrypted)", _status == "encrypted")
    check("interactive forge wrote a signed contract", g["contract"]["campaign"]["signed"] and g["contract"]["campaign"]["safeword"] == "banana")
    check("named the file after the agent", path.endswith("shani.grgn"))
    check("captured the elicited toolkit + red lines", g["contract"]["campaign"]["toolkit"] == ["list_vms", "run_guest_command"] and g["contract"]["forbidden"] == ["delete_network"])
    check("parsed the root-gate clause (criterion:target)", g["contract"]["campaign"]["success_predicate"] == [{"criterion": "running", "target": "vm1"}])
    os.remove(path)
    os.rmdir(d)
    # a blank safeword cancels (nothing written)
    ans2 = iter(["X", "r", "autonomous", "t", "", "g", "", "strict", "list_vms", "", "e", "l", "done", "1", ""])
    d2 = tempfile.mkdtemp()
    check("blank safeword cancels (no file)", forge_interactive(ask=lambda p: next(ans2), out=lambda s: None, write_dir=d2) is None)
    os.rmdir(d2)

    print(f"\n{_PASS}/{_PASS + _FAIL} passed")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

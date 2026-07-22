#!/usr/bin/env python3
"""
test_skin.py — the per-agent appearance skin: bundle skin.json overrides the global
defaults (null = inherit); a missing skin inherits everything.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shared.bundle as _bundle
from shared import skin as _skin

_PASS = _FAIL = 0


def check(label, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS {label}")
    else:
        _FAIL += 1
        print(f"  FAIL {label}")


def main():
    _bundle.AGENTS_ROOT = tempfile.mkdtemp()      # isolate bundle storage
    BASE = {"text_color": "#globalfg", "font_size": 13}

    print("missing skin → inherit base")
    check("no bundle → base unchanged", _skin.load_skin("nobody", BASE) == BASE)

    print("\nwrite + load: null inherits, non-null overrides")
    b = _bundle.Bundle("zubin"); b.ensure()
    with open(b.skin_path, "w") as f:
        json.dump({"text_color": "#zubinfg", "font_size": None, "font_family": None, "banner": None}, f)
    eff = _skin.load_skin("zubin", BASE)
    check("text_color overridden by the agent skin", eff["text_color"] == "#zubinfg")
    check("null font_size inherits the global", eff["font_size"] == 13)
    check("base is not mutated", BASE["text_color"] == "#globalfg")

    print("\nwrite_skin scaffolds an inherit template")
    p = _skin.write_skin("meta")
    data = json.load(open(p))
    check("scaffold writes all skin keys", set(data) == set(_skin.SKIN_KEYS))
    check("scaffold defaults to null (inherit)", all(v is None for v in data.values()))
    check("a scaffolded (all-null) skin inherits base", _skin.load_skin("meta", BASE) == BASE)

    print("\nforge scaffolds skin.json in the bundle")
    from orchestrator.ai.agent import forge
    d = tempfile.mkdtemp()
    path, issues = forge.finalize_forge(
        {"persona": {"name": "shani"}, "tools": {"list": ["create_vm"], "mode": "whitelist"}},
        "redrum", write_dir=d)
    check("forge succeeded", path is not None and issues == [])
    check("skin.json scaffolded beside the contract",
          os.path.isfile(os.path.join(os.path.dirname(path), "skin.json")))

    print(f"\n{'='*48}\n  {_PASS}/{_PASS + _FAIL} passed\n{'='*48}")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

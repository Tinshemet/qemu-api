"""
wizard.py — the forge driving flow: elicit → forge → review → sign → write.

finalize_forge() is the shared core (given a completed spec + safeword), and
forge_interactive() is the Doorman-driven dialogue that elicits the spec first.
Every forge frontend (terminal CLI, chat wizard) routes through these.
"""

import os
from typing import Any, Dict

from .assemble import forge, review, sign, render, write_grgn
from .schema import elicit_spec, _load_fields


def finalize_forge(spec: Dict[str, Any], safeword: str, write_dir: str = ".",
                   overwrite: bool = True):
    """forge → review → sign → write, given a completed spec and safeword.

    Returns (path, issues): a written path with no issues on success, or
    (None, issues) if the contract is incoherent, the safeword is blank, or the
    target file exists with overwrite=False. Shared by every forge frontend so
    the forge/review/sign/write core lives in exactly one place.
    """
    g = forge(spec)
    issues = review(g)
    if issues:
        return None, issues
    if not safeword:
        return None, ["a safeword is required to sign (the kill-switch)"]
    sign(g, safeword)
    name = (spec.get("persona", {}).get("name") or "agent").lower()
    # A forged agent is a BUNDLE: write_dir/<name>/<name>.grgn (production passes the
    # bundle root AGENTS_ROOT; a test passes a temp dir). The folder is the agent's
    # self-contained home — missions/findings/skin land beside the contract.
    bundle_dir = os.path.join(write_dir, name)
    os.makedirs(bundle_dir, exist_ok=True)
    path = os.path.join(bundle_dir, f"{name}.grgn")
    if os.path.exists(path) and not overwrite:
        return None, [f"{path} exists — choose a different agent name"]
    write_grgn(g, path)
    return path, []


def forge_interactive(ask, out, write_dir: str = ".", overwrite: bool = True,
                      essential_only: bool = False):
    """The Doorman-driven forging DIALOGUE: elicit the spec, forge → review →
    (negotiate: on issues, abort so the operator can revise) → sign → write.

    `ask(prompt) -> str` supplies answers (console.input in the CLI; scriptable in
    tests); `out(text)` prints (console.print). Returns the written path, or None if
    review found issues. Two-phase: nothing is signed until the safeword is given.
    With ``essential_only`` this is the simpler terminal forge — only the essential
    fields are asked, the rest defaulted. The questions themselves come from the
    field schema, not this function.
    """
    schema = _load_fields()
    out(schema.get("header", "═ Forge a campaign contract ═"))
    spec = elicit_spec(ask, essential_only=essential_only, schema=schema, out=out)
    g = forge(spec)
    issues = review(g)
    if issues:
        out("✗ The contract has issues — revise and re-forge:")
        for i in issues:
            out(f"    - {i}")
        return None
    out(render(g))
    sw = ask(schema.get("safeword_prompt",
                        "Contract is coherent. Sign with a SAFEWORD to seal it (blank to cancel)"))
    if not sw:
        out("  Cancelled — not signed.")
        return None
    path, issues = finalize_forge(spec, sw, write_dir, overwrite)
    if path is None:
        for i in issues:
            out(f"✗ {i}")
        return None
    out(f"  I am thou, thou art I — the contract is sealed.")
    out(f"  ✔ → {path}   ·   run it with  GORGON_AGENT={os.path.basename(path)}")
    return path

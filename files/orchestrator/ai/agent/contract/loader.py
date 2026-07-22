"""
loader.py — resolve + integrity-gate the active .grgn agent.

agent_grgn_path() is the bundle-first resolver; _load_active() applies the full
gate (selection → bundle/code resolution → void/tamper/expiry refusal → doorman
fallback) and returns the loaded (grgn, agent_path, status).
"""

import json
import os
from typing import Any, Dict

from ..fields import ExpiryField
from shared.bundle import resolve_grgn as _resolve_grgn


def _is_expired(grgn: Dict[str, Any]) -> bool:
    """True if the agent's contract carries an expiry date that is already past.
    Delegates to ExpiryField (the field owns the read + legacy fallback)."""
    return ExpiryField().is_expired((grgn or {}).get("contract", {}) or {})


def agent_grgn_path(agent_file: str, code_dir: str) -> str:
    """Resolve an agent selection to its .grgn path — bundle-first, code-dir fallback.
    Thin alias over the shared authority ``shared.bundle.resolve_grgn`` so the loader
    and the contract commands resolve identically."""
    return _resolve_grgn(agent_file, code_dir)


def _load_active() -> "tuple":
    """Resolve + integrity-gate the active .grgn, returning (grgn, agent_path, status).

    agent_select owns the resolution order (GORGON_AGENT env var > persisted
    `gorgon agent` selection > doorman default). A VOIDED, TAMPERED, MISSING, or
    EXPIRED selection is refused (fail-closed) and doorman runs instead; the reason
    is remembered in the status so it can be surfaced (not printed here, since this
    module loads in many contexts).
    """
    try:
        from shared.agent_select import resolve as _resolve_agent
    except Exception:
        _resolve_agent = lambda: "doorman.grgn"                                # type: ignore[assignment]
    agent_file = _resolve_agent()
    here       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # up from contract/ to agent/
    doorman    = os.path.join(here, "doorman.grgn")
    agent_path = agent_grgn_path(agent_file, here)
    if not os.path.isfile(agent_path):
        # A stale selection (deleted file) must not brick startup — fall back safely.
        agent_path = doorman

    # A VOIDED agent is disabled (its contract was revoked): refuse it and run doorman
    # instead. Because an agent's missions are only reachable while it's the active
    # agent, voiding the agent disables every mission it owns — the void cascade.
    try:
        from .. import revocation as _revocation
        voided = _revocation.is_voided(os.path.splitext(os.path.basename(agent_path))[0])
    except Exception:
        voided = False
    if voided and os.path.abspath(agent_path) != os.path.abspath(doorman):
        agent_path = doorman

    # Integrity gate: a TAMPERED agent file (bad Fernet token or bad sidecar) is
    # refused (fail-closed) and we fall back to doorman — a hand-edited/forged-under-a-
    # foreign-key contract must not run. Forged files are encrypted; the built-in
    # templates are plaintext (trust-on-first-use).
    try:
        from shared.grgn_sign import read as _read_grgn
    except Exception:
        _read_grgn = lambda p: (json.load(open(p)), "unsigned")               # type: ignore[assignment]
    loaded, status = _read_grgn(agent_path)
    refuse = (status in ("tampered", "missing") or loaded is None or _is_expired(loaded))
    if refuse and os.path.abspath(agent_path) != os.path.abspath(doorman):
        bad_status = "expired" if (loaded is not None and _is_expired(loaded)) else "tampered"
        agent_path = doorman                       # refuse; run the default agent
        loaded, _  = _read_grgn(agent_path)
        status     = bad_status                    # remember why the selected file was refused
    if voided:
        status = "voided"                          # the selected agent was revoked → doorman runs
    grgn = loaded if loaded is not None else json.load(open(doorman))
    return grgn, agent_path, status

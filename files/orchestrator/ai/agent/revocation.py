"""
revocation.py — voiding an agent (and, by cascade, its missions).

An agent exists because its contract is signed; VOIDING the contract revokes that
existence. A voided agent can't be loaded or run, and because missions live UNDER an
agent and are only reachable while that agent is active, voiding the agent disables
every mission it owns — the cascade the model calls for:

    void the contract → the agent is disabled → so are all its missions

Revocation is a small durable list (~/.gorgon/voided.json) rather than a mutation of
the .grgn, so it's reversible (restore) and works for the read-only built-ins too.
The default gatekeeper (doorman) can't be voided — it's the safe fallback.
"""
import json
import os
from typing import List

_PATH = os.path.expanduser("~/.gorgon/voided.json")
PROTECTED = frozenset({"doorman"})     # the fallback agent is never voidable


def _load() -> List[str]:
    try:
        with open(_PATH) as f:
            data = json.load(f)
        return list(data) if isinstance(data, list) else []
    except Exception:
        return []


def _save(agents: List[str]) -> None:
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    tmp = f"{_PATH}.tmp"
    with open(tmp, "w") as f:
        json.dump(sorted(set(agents)), f, indent=2)
    os.replace(tmp, _PATH)


def is_voided(agent: str) -> bool:
    return bool(agent) and agent in set(_load())


def voided() -> List[str]:
    return sorted(set(_load()))


def void(agent: str) -> bool:
    """Void an agent. Returns False if it's protected (doorman) or already voided."""
    if not agent or agent in PROTECTED:
        return False
    agents = _load()
    if agent in agents:
        return False
    agents.append(agent)
    _save(agents)
    return True


def restore(agent: str) -> bool:
    """Un-void an agent. Returns False if it wasn't voided."""
    agents = _load()
    if agent not in agents:
        return False
    _save([a for a in agents if a != agent])
    return True

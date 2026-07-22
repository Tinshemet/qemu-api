"""
spec.py — pure spec-dict + parse helpers for forging (no forge dependencies).

A forge "spec" is a flat/dotted-key dict of the operator's answers that forge()
transforms into a .grgn. These helpers read/write dotted keys and parse the two
compound answer shapes (CSV lists, criterion:target predicate clauses).
"""

from typing import Any, Dict


def _csv(s: str):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _predicate(s: str):
    """Parse 'present:honeypot, absent:web01' → the structured root-predicate clauses.
    Each 'criterion:target' becomes {'criterion':…, 'target':…}; review() validates them."""
    out = []
    for chunk in _csv(s):
        crit, _, target = chunk.partition(":")
        out.append({"criterion": crit.strip(), "target": target.strip()})
    return out


def _set_dotted(spec: Dict[str, Any], key: str, value: Any) -> None:
    """Set spec[a][b]=value for a dotted key 'a.b', creating dicts as needed."""
    parts = key.split(".")
    d = spec
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


def _get_dotted(spec: Dict[str, Any], key: str) -> Any:
    """Read spec[a][b] for a dotted key 'a.b', or None if any level is missing."""
    d = spec
    for p in key.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(p)
    return d

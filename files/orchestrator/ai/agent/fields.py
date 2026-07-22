"""
fields.py — the agent contract's fields, as classes (the shared field vocabulary).

Each semantic Field knows how to READ itself from a ``.grgn`` ``contract`` dict —
encapsulating the legacy ``campaign.*`` fallbacks so no reader re-encodes them — and
(wired in the forge pass) how to author itself in the wizard. This is the composition
seam the contract refactor is built on: adding a Field subclass extends the contract's
read surface (and, once forge consumes them, the wizard) in one place.

What is NOT a Field: the genuinely cross-field machinery — tier resolution, the risk
formula, the per-tool policy map, disposition/gate — which spans several keys at once
and lives on :class:`Contract` (see contract.py). Fields are the single-key, authorable
parts (toolkit, red-lines, safeword, expiry, the mission defaults).
"""

from typing import Any, Dict, List, Optional


class Field:
    """Base: one named semantic field of the agent contract.

    ``read(contract)`` takes the ``.grgn`` ``contract`` sub-dict and returns the
    field's value, applying whatever legacy fallbacks the field owns. Subclasses
    add field-specific accessors (e.g. ForbiddenField.contains) where the runtime
    asks more than "give me the value".
    """

    key: str = ""

    def read(self, contract: Dict[str, Any]) -> Any:
        raise NotImplementedError


class ToolkitField(Field):
    """The tool WHITELIST — the toolkit the agent may use unless a mission narrows
    it. Empty = 'no explicit whitelist' (all registered tools, subject to red-lines).
    Legacy fallback: ``campaign.toolkit``."""

    key = "toolkit"

    def read(self, contract: Dict[str, Any]) -> List[str]:
        camp = contract.get("campaign") or {}
        return list(contract.get("toolkit") or camp.get("toolkit") or [])


class ForbiddenField(Field):
    """The tool BLACKLIST — the agent's red lines. A mission may add to it, never
    remove from it. The legal filter (is_forbidden) reads membership here."""

    key = "forbidden"

    def read(self, contract: Dict[str, Any]) -> List[str]:
        return list(contract.get("forbidden") or [])

    def contains(self, contract: Dict[str, Any], tool: str) -> bool:
        """True if ``tool`` is a hard red line (categorical, never costed)."""
        return tool in set(contract.get("forbidden", []))


class SafewordField(Field):
    """The operator's kill-switch word. Legacy fallback: ``campaign.safeword``."""

    key = "safeword"

    def read(self, contract: Dict[str, Any]) -> Optional[str]:
        return contract.get("safeword") or (contract.get("campaign") or {}).get("safeword")


class ExpiryField(Field):
    """The agent's credential expiry (ISO date). Legacy fallback: ``campaign.expiry``.
    An expired contract is refused at load (fail-closed) — see is_expired."""

    key = "expiry"

    def read(self, contract: Dict[str, Any]) -> Optional[str]:
        return contract.get("expiry") or (contract.get("campaign") or {}).get("expiry")

    def is_expired(self, contract: Dict[str, Any]) -> bool:
        """True if the expiry date is already past. Unparseable → False (don't brick
        startup)."""
        exp = self.read(contract)
        if not exp:
            return False
        try:
            from datetime import date
            return date.fromisoformat(str(exp)) < date.today()
        except Exception:
            return False


class RewardField(Field):
    """The agent's default payoff R for closing a goal (a mission's `reward` overrides
    it). From ``defaults.reward`` with the legacy ``campaign.reward`` fallback, else 1.0."""

    key = "reward"

    def read(self, contract: Dict[str, Any]) -> float:
        defaults = contract.get("defaults") or {}
        camp     = contract.get("campaign") or {}
        if "reward" in defaults:
            return float(defaults["reward"])
        return float(camp.get("reward", 1.0))


class ScrutinyField(Field):
    """The agent's default scrutiny level (a mission may raise/lower it). From
    ``defaults.scrutiny`` with the legacy ``campaign.scrutiny`` fallback."""

    key = "scrutiny"

    def read(self, contract: Dict[str, Any]):
        defaults = contract.get("defaults") or {}
        camp     = contract.get("campaign") or {}
        if "scrutiny" in defaults:
            return defaults["scrutiny"]
        return camp.get("scrutiny")


class ImportanceField(Field):
    """The agent's default mission importance (a reward multiplier). From
    ``defaults.importance``, else 1.0 (no campaign fallback)."""

    key = "importance"

    def read(self, contract: Dict[str, Any]) -> float:
        return float((contract.get("defaults") or {}).get("importance", 1.0))


class WeightField(Field):
    """The agent's default mission weight (planning/scoring weight). From
    ``defaults.weight``, else 1.0 (no campaign fallback)."""

    key = "weight"

    def read(self, contract: Dict[str, Any]) -> float:
        return float((contract.get("defaults") or {}).get("weight", 1.0))


# The agent contract's field set. Constructing a Contract composes one instance of
# each; adding a new authorable single-key field = add a class here.
CONTRACT_FIELDS = (
    ToolkitField, ForbiddenField, SafewordField, ExpiryField,
    RewardField, ScrutinyField, ImportanceField, WeightField,
)


def build_fields() -> Dict[str, Field]:
    """A fresh ``{key: Field()}`` map of every contract field."""
    return {cls.key: cls() for cls in CONTRACT_FIELDS}

"""
fields.py — the agent contract's fields, as classes (the SSOT for a contract field).

Each Field is the single home for one contract field's two roles:

  * RUNTIME — ``read(contract)`` returns the field's value from a ``.grgn``
    ``contract`` dict, encapsulating the legacy ``campaign.*`` fallbacks so no
    reader re-encodes them; some add accessors (ForbiddenField.contains,
    ExpiryField.is_expired).
  * AUTHORING — the class attributes (``spec_key``/``prompt``/``shape``/``essential``
    /``default``/``ask``…) and :meth:`schema` describe how the field is elicited in
    the forge wizard. ``forge._load_fields()`` builds its field list from
    :func:`forge_schema_fields`, so **adding a Field subclass to FORGE_FIELD_ORDER
    grows the wizard** — no JSON edit. The parse/validate *strategies* by data-shape
    (``shape`` → forge._FIELD_TYPES) stay shared, so mission authoring is unaffected.

What is NOT a Field: the cross-key machinery — tier resolution, the risk formula,
the per-tool policy map, disposition/gate — which spans several keys and lives on
:class:`Contract` (contract.py).
"""

from typing import Any, Dict, List, Optional


class Field:
    """Base: one contract field. Subclasses set the runtime ``key`` (for read) and/or
    the authoring attributes (for the wizard); a field may be runtime-only,
    authoring-only, or both."""

    key: str = ""                      # runtime contract key ("" = not read at runtime)

    # ── authoring metadata ("" / None = not an elicited wizard field) ────────────
    spec_key: Optional[str] = None     # forge spec key (dotted); defaults to `key`
    prompt: Optional[str] = None
    shape: Optional[str] = None        # forge FieldType strategy name (str|csv|toolkit|importance|expiry|…)
    essential: bool = False
    default: Any = None
    ask: bool = True                   # False → a constant set without prompting
    const_value: Any = None            # the value for an ask=False constant field
    conflicts_with: Optional[str] = None
    levels: Optional[dict] = None

    def read(self, contract: Dict[str, Any]) -> Any:
        raise NotImplementedError

    @classmethod
    def schema(cls) -> Dict[str, Any]:
        """Emit this field's forge schema dict — the entry the wizard iterates
        (equivalent to one former forge_fields.json ``fields`` element)."""
        key = cls.spec_key or cls.key
        if cls.ask is False:
            return {"key": key, "ask": False, "value": cls.const_value}
        d: Dict[str, Any] = {"key": key, "prompt": cls.prompt, "parse": cls.shape}
        if cls.essential:
            d["essential"] = True
        if cls.default is not None:
            d["default"] = cls.default
        if cls.conflicts_with:
            d["conflicts_with"] = cls.conflicts_with
        if cls.levels:
            d["levels"] = cls.levels
        return d


# ── runtime + authored fields ────────────────────────────────────────────────────

class ToolkitField(Field):
    """The tool WHITELIST — the toolkit the agent may use unless a mission narrows
    it. Empty = 'no explicit whitelist' (all registered tools, subject to red-lines).
    Legacy fallback: ``campaign.toolkit``. Authored as the ``tools.list`` spec key
    (forge maps it to ``contract.toolkit``)."""

    key       = "toolkit"
    spec_key  = "tools.list"
    prompt    = "Toolkit — whitelist tools (comma-separated)"
    shape     = "toolkit"
    essential = True

    def read(self, contract: Dict[str, Any]) -> List[str]:
        camp = contract.get("campaign") or {}
        return list(contract.get("toolkit") or camp.get("toolkit") or [])


class ToolsModeField(Field):
    """The whitelist/blacklist mode for the toolkit — a constant (whitelist) set
    without prompting; forge reads ``tools.mode`` to decide the toolkit shape."""

    spec_key    = "tools.mode"
    ask         = False
    const_value = "whitelist"


class ForbiddenField(Field):
    """The tool BLACKLIST — the agent's red lines. A mission may add to it, never
    remove from it. The legal filter (is_forbidden) reads membership here."""

    key            = "forbidden"
    spec_key       = "forbidden"
    prompt         = "Red lines — tools to forbid (comma-separated, blank for none)"
    shape          = "toolkit"
    conflicts_with = "tools.list"

    def read(self, contract: Dict[str, Any]) -> List[str]:
        return list(contract.get("forbidden") or [])

    def contains(self, contract: Dict[str, Any], tool: str) -> bool:
        """True if ``tool`` is a hard red line (categorical, never costed)."""
        return tool in set(contract.get("forbidden", []))


class SafewordField(Field):
    """The operator's kill-switch word. Legacy fallback: ``campaign.safeword``.
    Runtime-only: it's set by the signing ceremony, not elicited as a wizard field."""

    key = "safeword"

    def read(self, contract: Dict[str, Any]) -> Optional[str]:
        return contract.get("safeword") or (contract.get("campaign") or {}).get("safeword")


class ExpiryField(Field):
    """The agent's credential expiry (ISO date). Legacy fallback: ``campaign.expiry``.
    An expired contract is refused at load (fail-closed) — see is_expired."""

    key      = "expiry"
    spec_key = "expiry"
    prompt   = "Expiry — when does this agent expire? (YYYY-MM-DD, a duration like 30d, or blank for never)"
    shape    = "expiry"

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
    it). From ``defaults.reward`` with the legacy ``campaign.reward`` fallback, else
    1.0. Authored as an importance word (routine/important/critical → a number)."""

    key      = "reward"
    spec_key = "reward"
    prompt   = "Default reward — how much does a typical mission matter? [routine|important|critical]"
    shape    = "importance"
    default  = "important"
    levels   = {"routine": 3, "important": 10, "critical": 30}

    def read(self, contract: Dict[str, Any]) -> float:
        defaults = contract.get("defaults") or {}
        camp     = contract.get("campaign") or {}
        if "reward" in defaults:
            return float(defaults["reward"])
        return float(camp.get("reward", 1.0))


class ScrutinyField(Field):
    """The agent's default scrutiny level (a mission may raise/lower it). From
    ``defaults.scrutiny`` with the legacy ``campaign.scrutiny`` fallback."""

    key      = "scrutiny"
    spec_key = "scrutiny"
    prompt   = "Default scrutiny [strict|medium|loose]"
    shape    = "str"
    default  = "strict"

    def read(self, contract: Dict[str, Any]):
        defaults = contract.get("defaults") or {}
        camp     = contract.get("campaign") or {}
        if "scrutiny" in defaults:
            return defaults["scrutiny"]
        return camp.get("scrutiny")


class ImportanceField(Field):
    """The agent's default mission importance (a reward multiplier). From
    ``defaults.importance``, else 1.0 (no campaign fallback). Runtime-only."""

    key = "importance"

    def read(self, contract: Dict[str, Any]) -> float:
        return float((contract.get("defaults") or {}).get("importance", 1.0))


class WeightField(Field):
    """The agent's default mission weight (planning/scoring weight). From
    ``defaults.weight``, else 1.0 (no campaign fallback). Runtime-only."""

    key = "weight"

    def read(self, contract: Dict[str, Any]) -> float:
        return float((contract.get("defaults") or {}).get("weight", 1.0))


# ── authoring-only fields (elicited + written to the .grgn, no runtime read) ──────

class PersonaNameField(Field):
    spec_key  = "persona.name"
    prompt    = "Agent name"
    shape     = "str"
    essential = True


class PersonaRoleField(Field):
    spec_key = "persona.role"
    prompt   = "Role"
    shape    = "str"


class PersonaDispositionField(Field):
    spec_key = "persona.disposition"
    prompt   = "Disposition [human-confirm|autonomous]"
    shape    = "str"
    default  = "autonomous"


class EthicsField(Field):
    spec_key = "ethics"
    prompt   = "Ethics"
    shape    = "str"


class LegalityField(Field):
    spec_key = "legality"
    prompt   = "Legality"
    shape    = "str"


# The runtime contract fields (Contract composes one of each via build_fields()).
CONTRACT_FIELDS = (
    ToolkitField, ForbiddenField, SafewordField, ExpiryField,
    RewardField, ScrutinyField, ImportanceField, WeightField,
)

# The forge wizard's fields, IN ELICITATION ORDER. Adding an authored field = add a
# Field subclass here (and to CONTRACT_FIELDS if it's also read at runtime); the
# wizard picks it up via forge._load_fields() → forge_schema_fields().
FORGE_FIELD_ORDER = (
    PersonaNameField, PersonaRoleField, PersonaDispositionField, ScrutinyField,
    ToolkitField, ToolsModeField, ForbiddenField, EthicsField, LegalityField,
    RewardField, ExpiryField,
)


def build_fields() -> Dict[str, Field]:
    """A fresh ``{key: Field()}`` map of every runtime contract field."""
    return {cls.key: cls() for cls in CONTRACT_FIELDS}


def forge_schema_fields() -> List[Dict[str, Any]]:
    """The forge wizard's field list (schema dicts), in elicitation order — the
    single source that replaced forge_fields.json's ``fields`` array."""
    return [cls.schema() for cls in FORGE_FIELD_ORDER]

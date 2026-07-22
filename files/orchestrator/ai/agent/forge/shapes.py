"""
shapes.py — the forge FieldType strategies (parse/validate/format by DATA SHAPE).

A FieldType is how an answer of a given shape (string, csv, toolkit, predicate,
float, importance, expiry…) is parsed into a spec value, validated, and rendered
back. New shape = a new subclass registered in ``_FIELD_TYPES``; field INSTANCES
are declared elsewhere (the contract fields in ../fields.py, mission_fields.json).
These strategies are shared across agent AND mission authoring, so they know only
the data shape, never a specific schema.
"""

from typing import Any, Dict

from .spec import _csv, _predicate, _get_dotted


class FieldType:
    """Strategy for a forge field's TYPE — how to parse an answer into a spec
    value, and how to render that value back for display.

    A new field type is a new SUBCLASS registered in ``_FIELD_TYPES``; field
    INSTANCES stay data-driven (each entry names its ``parse`` type). This is the
    seam that lets new field behaviour arrive by inheritance instead of editing
    elicit_spec()/render().
    """

    def parse(self, raw, field):
        raise NotImplementedError

    def validate(self, value, field, spec=None):
        """Field-specific checks on a parsed value → list of issue strings (empty
        = OK). The base type accepts anything; subclasses tighten. Run per field
        during elicitation (immediate feedback) — see validate_answer(). ``spec``
        is the answers-so-far, for cross-field checks (e.g. a red line that's also
        whitelisted)."""
        return []

    def format(self, value, field):
        """Human-readable rendering of a stored value (for `contract show`)."""
        return "" if value in ("", None) else str(value)


class StrField(FieldType):
    def parse(self, raw, field):
        v = (raw or "").strip()
        if v:
            return v
        d = field.get("default")
        return d if d is not None else ""


class CsvField(FieldType):
    def parse(self, raw, field):
        return _csv(raw)


class ToolkitField(CsvField):
    """A CSV of tool NAMES, validated against the executor registry. This field
    type owns the tools-SSOT check: a name absent from KNOWN_TOOLS is drift. The
    executor is still the real gate (an unknown call is rejected there), so this
    is early, friendly feedback — not the enforcement point. Degrades to no-op if
    the executor package (and thus the registry) isn't importable."""

    def validate(self, value, field, spec=None):
        issues = []
        try:
            from executor.command_catalog import KNOWN_TOOLS
        except ImportError:
            KNOWN_TOOLS = None
        if KNOWN_TOOLS:
            issues += [f"unknown tool '{t}' — not in the executor registry"
                       for t in value if t not in KNOWN_TOOLS]
        # Cross-field: a field may declare `conflicts_with` another field's list
        # (e.g. red lines vs the toolkit) — catch the contradiction inline instead
        # of at review() after every question is answered.
        other_key = field.get("conflicts_with")
        if other_key and spec is not None:
            clash = set(value) & set(_get_dotted(spec, other_key) or [])
            if clash:
                issues.append("already whitelisted, can't also be a red line: "
                              + ", ".join(sorted(clash)))
        return issues


class PredicateField(FieldType):
    _CRITERIA = {"present", "absent", "running", "stopped", "restored", "mesh", "reachable", "probe", "found"}

    def parse(self, raw, field):
        return _predicate(raw)

    def validate(self, value, field, spec=None):
        issues = []
        for clause in value or []:
            crit, target = clause.get("criterion"), clause.get("target")
            if crit not in self._CRITERIA:
                issues.append(f"'{crit}' is not a checkable criterion "
                              f"(use one of {sorted(self._CRITERIA)})")
            elif not target:
                issues.append(f"criterion '{crit}' needs a target, e.g. {crit}:vm1")
        return issues


class FloatField(FieldType):
    def parse(self, raw, field):
        v = (raw or "").strip()
        return float(v) if v else float(field.get("default", 1))


class OptionalFloatField(FieldType):
    """A float that stays UNSET when blank (→ None), rather than falling to a
    default. Used for mission fields that INHERIT the agent's default when omitted
    (reward/importance/weight): a blank answer must mean 'inherit', not '1.0'."""

    def parse(self, raw, field):
        v = (raw or "").strip()
        if not v:
            return None
        try:
            return float(v)
        except ValueError:
            return None

    def format(self, value, field):
        return "" if value in ("", None) else str(value)


class ImportanceField(FieldType):
    """Reward-as-importance: an importance WORD maps to a reward number, so the
    operator answers 'how much does this goal matter?' instead of guessing a
    unitless number. The word→number map is the field's ``levels`` (data-driven).
    Blank → default; a raw number is still accepted; an unknown word → default."""

    def parse(self, raw, field):
        levels = {k.lower(): v for k, v in (field.get("levels") or {}).items()}
        key = (raw or "").strip().lower() or str(field.get("default", "")).lower()
        if key in levels:
            return float(levels[key])
        try:
            return float(raw)                       # an explicit number is fine too
        except (TypeError, ValueError):
            return float(levels.get(str(field.get("default", "")).lower(), 1.0))

    def format(self, value, field):
        if value in ("", None):
            return ""
        for word, num in (field.get("levels") or {}).items():
            try:
                if float(num) == float(value):
                    return f"{word} ({value})"      # e.g. "important (10)"
            except (TypeError, ValueError):
                pass
        return str(value)


class ExpiryField(FieldType):
    """Optional contract expiry — the twin of ToolkitField, showing the same
    'new field type = one subclass' seam. Accepts an ISO date (2026-12-31) or a
    duration (30d / 6w / 3m / 1y), normalized to an absolute ISO date; blank →
    None (never expires). validate() rejects a garbled or already-past date;
    contract.py enforces it at load (an expired contract is refused, fail-closed)."""

    def parse(self, raw, field):
        import re
        from datetime import date, timedelta
        s = (raw or "").strip().lower()
        if not s:
            return None
        m = re.fullmatch(r"(\d+)\s*([dwmy])", s)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            days = {"d": 1, "w": 7, "m": 30, "y": 365}[unit] * n
            return (date.today() + timedelta(days=days)).isoformat()
        try:
            return date.fromisoformat(s).isoformat()
        except ValueError:
            return s                                # keep raw so validate() can flag it

    def validate(self, value, field, spec=None):
        if not value:
            return []
        from datetime import date
        try:
            d = date.fromisoformat(value)
        except ValueError:
            return [f"unparseable expiry {value!r} — use YYYY-MM-DD or a duration like 30d"]
        return [f"expiry {value} is already in the past"] if d < date.today() else []

    def format(self, value, field):
        return "never" if not value else str(value)


# Field-type registry — a field names a type via its ``parse`` key; add a type by
# subclassing FieldType and registering it here (instances live in the field schema).
_FIELD_TYPES = {
    "str":        StrField(),
    "csv":        CsvField(),
    "toolkit":    ToolkitField(),
    "predicate":  PredicateField(),
    "float":      FloatField(),
    "optfloat":   OptionalFloatField(),
    "importance": ImportanceField(),
    "expiry":     ExpiryField(),
}


def _field_type(field: Dict[str, Any]) -> FieldType:
    return _FIELD_TYPES[field["parse"]]

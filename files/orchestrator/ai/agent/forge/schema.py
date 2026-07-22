"""
schema.py — the forge field schema + elicitation.

Loads the wizard's field schema (config blocks from forge_fields.json + the field
list from the contract Field classes) and walks it: asked_fields / default_value /
parse_answer / validate_answer per field, and elicit_spec() to build a whole spec.
The per-field parse/validate go through the shape strategies (shapes._field_type).
"""

import json
import os
from typing import Any, Dict, List

from .shapes import _field_type
from .spec import _set_dotted, _get_dotted

# The code-resident agent dir (agent/), one level up from this forge/ sub-package —
# forge_fields.json lives there beside doorman.grgn.
_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_fields() -> Dict[str, Any]:
    """The forge field schema. The config blocks (header, safeword_prompt, intent,
    wizard) come from forge_fields.json; the ``fields`` list is the SINGLE SOURCE
    built from the contract Field classes (fields.FORGE_FIELD_ORDER) — so adding a
    Field subclass grows the wizard. The parse/validate strategies (shapes) stay
    shared with mission authoring, which loads its own mission_fields.json."""
    from ..fields import forge_schema_fields
    cfg = json.load(open(os.path.join(_AGENT_DIR, "forge_fields.json")))
    cfg["fields"] = forge_schema_fields()
    return cfg


def _reward_render(value) -> str:
    """Render a reward via its field type (ImportanceField shows 'tier (n)')."""
    if value == "" or value is None:
        return ""
    for f in _load_fields()["fields"]:
        if f.get("key") == "reward":
            return _field_type(f).format(value, f)
    return str(value)


def asked_fields(schema: Dict[str, Any], essential_only: bool = False) -> List[Dict[str, Any]]:
    """The fields that get PROMPTED, in order: skip ask=false constants, and —
    in essential_only — skip non-essential fields (they take their default)."""
    return [f for f in schema["fields"]
            if f.get("ask", True) is not False
            and (not essential_only or f.get("essential", False))]


def default_value(field: Dict[str, Any]) -> Any:
    """The value for a field that ISN'T being asked — a constant (ask=false) or
    an unprompted default (parse the empty answer through the field's type)."""
    if field.get("ask", True) is False:
        return field.get("value", field.get("default"))
    return _field_type(field).parse("", field)


def parse_answer(field: Dict[str, Any], raw: str) -> Any:
    """Parse a raw answer for a field through its declared field type."""
    return _field_type(field).parse(raw, field)


def validate_answer(field: Dict[str, Any], value: Any, spec: Dict[str, Any] = None) -> List[str]:
    """Field-type validation for a parsed value (empty list = OK). ``spec`` is the
    answers-so-far, for cross-field checks."""
    return _field_type(field).validate(value, field, spec)


def elicit_spec(ask, *, essential_only: bool = False, schema: Dict[str, Any] = None,
                out=None) -> Dict[str, Any]:
    """Build a contract spec by walking the declarative field schema.

    `ask(prompt) -> str` supplies each answer (console.input in the CLI, one chat
    turn in the wizard, scripted in tests). Fields are visited in schema order —
    that order IS the elicitation order. With ``essential_only`` (the simpler
    terminal forge) non-essential fields take their default without being asked;
    ``ask=false`` fields (e.g. tool_mode) are constants and never prompt. The
    resulting spec is fed to forge(); safeword/signing happen separately.

    If ``out`` is given, each answer is validated through its field type and
    re-asked (with the issues printed) until it passes — immediate per-field
    feedback (e.g. an unknown tool name). Without ``out`` validation is skipped
    here and left to review(), preserving the old parse-only behavior for
    callers that can't re-prompt.
    """
    schema = schema or _load_fields()
    asked = {f["key"] for f in asked_fields(schema, essential_only)}
    spec: Dict[str, Any] = {}
    for field in schema["fields"]:
        if field["key"] not in asked:
            _set_dotted(spec, field["key"], default_value(field))
            continue
        value = parse_answer(field, ask(field["prompt"]))
        if out is not None:
            issues = validate_answer(field, value, spec)
            while issues:
                for i in issues:
                    out(f"  ✗ {i}")
                value = parse_answer(field, ask(field["prompt"]))
                issues = validate_answer(field, value, spec)
        _set_dotted(spec, field["key"], value)
    return spec

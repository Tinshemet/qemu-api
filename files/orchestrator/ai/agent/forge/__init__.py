"""
forge — contract forging: turn a negotiated spec into a signed .grgn agent.

The flow is  elicit a spec → forge() → review() → (negotiate) → sign() → write, and
a forged agent is written as a BUNDLE (write_dir/<name>/<name>.grgn). Split into
focused sub-modules; this facade re-exports the stable public surface so callers
keep importing from ``orchestrator.ai.agent.forge``:

  - spec       dotted-key + parse helpers (_csv/_predicate/_set_dotted/_get_dotted)
  - shapes     the FieldType strategies (parse/validate/format by data shape) + registry
  - schema     the field schema + elicitation (_load_fields/asked_fields/parse_answer/…)
  - assemble   forge()/review()/sign()/render()/write_grgn()
  - wizard     finalize_forge()/forge_interactive() — the driving flow
"""

from .spec import _csv, _predicate, _set_dotted, _get_dotted
from .shapes import (
    FieldType, StrField, CsvField, ToolkitField, PredicateField, FloatField,
    OptionalFloatField, ImportanceField, ExpiryField, _FIELD_TYPES, _field_type,
)
from .schema import (
    _load_fields, _reward_render, asked_fields, default_value, parse_answer,
    validate_answer, elicit_spec,
)
from .assemble import (
    _base_innate, _build_prompt, forge, review, sign, render, write_grgn,
)
from .wizard import finalize_forge, forge_interactive

__all__ = [
    "forge", "review", "sign", "render", "write_grgn",
    "finalize_forge", "forge_interactive",
    "_load_fields", "asked_fields", "default_value", "parse_answer",
    "validate_answer", "elicit_spec", "_reward_render",
    "_set_dotted", "_get_dotted", "_csv", "_predicate",
    "FieldType", "_FIELD_TYPES", "_field_type",
]

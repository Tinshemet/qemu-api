"""
context_gate.py — Pre-Execution Argument Gate

Intercepts tool calls before dispatch and checks whether all
contextually-required arguments are explicitly present. If any are
missing it returns a clarify response, forcing the AI to ask the user
for the missing detail before the tool is allowed to run.

This prevents the AI from hallucinating success or silently filling in
wrong defaults for fields the user must consciously decide.
"""

import json
import pathlib
from typing import Any, Dict, List, Optional, Tuple

_CONFIG_PATH = pathlib.Path(__file__).with_name("context_gate_config.json")

# Keys are tool names; values are lists of [field, question, options] entries.
# Tools with no required args (check_system, scan_isos, list_vms,
# list_profiles, list_networks) and the internal clarify tool are
# intentionally omitted — nothing to gate on.
with _CONFIG_PATH.open() as _f:
    _raw = json.load(_f)

_REQUIRED: Dict[str, List[Tuple[str, str, List[str]]]] = {
    tool: [tuple(entry) for entry in entries]
    for tool, entries in _raw.items()
}


def gate_check(tool_name: str, args: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Returns a clarify dict listing ALL missing required arguments at once,
    or None if all required arguments are present and the tool may proceed.
    """
    required_fields = _REQUIRED.get(tool_name)
    if not required_fields:
        return None

    missing = [
        {"field": field, "question": question, "options": options}
        for field, question, options in required_fields
        if args.get(field) is None or (isinstance(args.get(field), str) and not args[field].strip())
    ]

    if not missing:
        return None

    return {
        "success":             False,
        "clarify":             True,
        "missing":             missing,
        # First missing field kept at top level for backward compat
        "question":            missing[0]["question"],
        "options":             missing[0]["options"],
        "needs_clarification": missing[0]["field"],
        "error":               f"Missing required arguments for {tool_name}: {[m['field'] for m in missing]}",
    }

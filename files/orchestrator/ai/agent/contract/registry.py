"""
registry.py — the executor tool registry (the FACTS source of truth: what tools
exist + their signatures).

Guarded like score.py's import so the contract still loads in orchestrator-only
checkouts without the executor package (tools resolve to ``none`` then).
"""

from typing import Any, Dict

try:
    from executor.command_catalog import TOOL_SPECS as _TOOL_SPECS, TOOL_NAME_ARG as _TOOL_NAME_ARG
except ImportError:                                                    # pragma: no cover
    _TOOL_SPECS: Dict[str, Any] = {}
    _TOOL_NAME_ARG: Dict[str, str] = {}

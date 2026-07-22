"""
_deps.py — the injected contract / registry / findings helpers.

Optional so the engine still imports in orchestrator-only checkouts / pure-unit
tests without the executor package. Kept as ONE all-or-nothing block (as the
original single try/except) so a sparse checkout degrades uniformly — steering
skipped, contract/findings defaults None/no-op — rather than half-wired.
"""

from typing import Dict

# Ledger-aware attach steering (data-driven from the tool registry) + the contract
# and findings helpers the engine falls back to when the caller injects none.
try:
    from executor.command_catalog import POST_CREATE_ATTACH as _POST_CREATE_ATTACH
    from orchestrator.ai.chat.context_assistant import _NARROW_CORE_TOOLS
    from orchestrator.ai.agent.contract import gate_action as _default_gate
    from orchestrator.ai.agent.contract import success_criterion as _default_criterion
    from orchestrator.ai.planner.findings import (yield_fact as _yield_fact, extract_value as _extract_value,
                                           finding_probe_spec as _finding_probe_spec)
    from orchestrator.ai.agent.contract import is_forbidden as _default_legal, consent_verb as _consent_verb
    from orchestrator.ai.agent.contract import tool_risk as _tool_risk
except ImportError:
    _POST_CREATE_ATTACH: Dict[str, Dict[str, str]] = {}
    _NARROW_CORE_TOOLS = frozenset()
    _default_gate = None
    _default_criterion = None
    _yield_fact = lambda *a, **k: None
    _extract_value = lambda r, s: r
    _finding_probe_spec = lambda *a, **k: None
    _default_legal = None
    _consent_verb = lambda t: t
    _tool_risk = lambda t: None

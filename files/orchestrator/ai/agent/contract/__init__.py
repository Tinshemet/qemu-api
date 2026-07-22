"""
contract — the engine that loads a .grgn agent and applies its contract.

Split into focused sub-modules (registry, risk_formula, tool_policy, loader, core);
this facade re-exports the stable public surface so callers keep importing from
``orchestrator.ai.agent.contract`` unchanged, including the module-level default
instance ``ACTIVE`` and the thin ``contract.foo()`` shims over it.
"""

from .core import *              # noqa: F401,F403  — the public functions/classes/constants
# Underscore-prefixed names other modules read as attributes (not covered by *).
from .core import (              # noqa: F401
    _TOOLS, _HANDLING, _HANDLING_FALLBACK, _AGENT_PATH, _AGENT_STATUS, _C,
    _CONTRACT, _PROMPTS, _TIER_RANK, _FORMULA, _WEIGHTS, _BLAST_SCALE, _THRESHOLDS,
    _FLEET_ACTIONS, _risk_score, _risk_to_tier, _defaults, _registry_target_field,
    _is_expired, _load_active,
)

"""
score — the Score: the recursive goal→primitive decomposition engine (+ ledger).

Named per the orchestra theme: a Conductor works from a SCORE — a goal decomposed
into what each instrument (tool) plays, step by step; "keeping score" is the ledger.

The unreason gate, as code: at each node the model gets ONE choice — emit a single
grounded PRIMITIVE tool call (→ a leaf, executed) OR call the `decompose` meta-tool
with an ordered list of sub-goals (→ recurse on each). The model PROPOSES; whether a
node is atomic is decided objectively by WHICH it returned, never self-certified.

Dependencies are INJECTED (call_model / execute / tools) so the engine is fully
testable without Ollama or the live loop.

This package splits the former single score.py into:
  - engine_core.py  the recursive run_score engine (the irreducible closure)
  - ledger_util.py  stateless helpers (_node/_norm/_progress_summary/_attach_steer/
                    _first_tool_call)
  - meta_tools.py   the decompose/alternatives schemas + node constants
  - _deps.py        the optional injected contract/registry/findings helpers
This module re-exports the public surface so existing imports are unchanged.
"""

from .engine_core import run_score
from .ledger_util import _first_tool_call, _progress_summary
from .meta_tools import DECOMPOSE_TOOL, _NODE_SYSTEM

__all__ = [
    "run_score", "_first_tool_call", "_progress_summary",
    "DECOMPOSE_TOOL", "_NODE_SYSTEM",
]

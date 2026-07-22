"""
engine.py — the Engine: one bundle for the planner's policy dependencies.

`run_score` grew ~18 parameters as the reward-cost brain landed (gate, verify,
criterion_of, legal_filter, referendum, watchdog, findings, method_cache, …). That
is real debt: hard to read, hard to thread, easy to mis-wire. The Engine collects the
POLICY deps into one object so `run_score(goal, engine=…)` stays legible and
run_autonomous constructs the bundle once.

Only the POLICY deps live here. The three CORE deps a run can't run without —
`call_model`, `execute`, `tools` — stay explicit on run_score, as do the plan LIMITS
(max_retries/max_depth) and the two grounding hooks (build_context/select_tools).

(The old `is_destructive`/`confirm` backstop is retired — it was superseded by the
split gate: the legal filter (A) + the consent referendum (D).)
"""
from dataclasses import dataclass, fields
from typing import Any, Callable, Dict, Optional


@dataclass
class Engine:
    gate:            Optional[Callable[[str, Dict], str]] = None            # contract → handling action
    verify:          Optional[Callable[[str, str, Dict, Any], bool]] = None  # verified-completion check (leaf)
    verify_goal:     Optional[Callable[[str, list, list], Optional[bool]]] = None  # contract ROOT predicate
    criterion_of:    Optional[Callable[[str], Optional[str]]] = None        # per-tool success criterion
    legal_filter:    Optional[Callable[[str, Dict], bool]] = None           # hard red line (gauntlet A)
    referendum:      Optional[Callable[[str, Dict, str], bool]] = None      # consent surface (gauntlet D)
    watchdog:        Any = None                                             # farming/loop monitor
    killswitch:      Any = None                                             # safeword abort (infrastructural)
    findings:        Any = None                                             # Findings ledger
    findings_schema: Optional[Dict[str, Dict[str, str]]] = None             # per-tool yield-schema
    method_cache:    Any = None                                             # decomposition cache
    decompose_first: bool = False                                           # force the atomicity pre-gate
    estimate:        Optional[Callable[[str, int], Optional[float]]] = None  # per-alternative CE estimate (OR ordering/pruning)
    ce_floor:        float = 0.0                                            # worth-it threshold θ — prune alts with CE ≤ this
    retry_penalty:   float = 0.0                                            # holding cost H per wasted retry (CE-based backtrack-abandon)

    @classmethod
    def from_kwargs(cls, kw: Dict[str, Any]) -> "Engine":
        """Build an Engine from a loose kwargs dict, ignoring anything not a field
        (a transitional shim so legacy `run_score(gate=…, verify=…)` calls still work)."""
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in kw.items() if k in names})

"""
risk_formula.py — the weighted risk→tier scorer.

Turns a tool's risk facts into a [0,1] score and then a tier, using the contract's
``formula`` block (weights / blast_scale / thresholds). Pure cross-field scoring
config — never authored, copied from the innate baseline — so it's a component of
Contract, not an authored Field.
"""

from typing import Any, Dict, List


class RiskFormula:
    """The weighted risk→tier scorer: turns a tool's risk facts into a [0,1] score
    and then a tier, using the contract's ``formula`` block (weights / blast_scale /
    thresholds). Pure cross-field scoring config — never authored, copied from the
    innate baseline — so it lives here, not as an authored Field.
    """

    def __init__(self, formula: Dict[str, Any], tiers: List[str]):
        self.weights     = formula["weights"]
        self.blast_scale = formula["blast_scale"]
        self.thresholds  = formula["thresholds"]        # {tier: min_risk} for non-"none" tiers
        self.reward_cost = dict(formula.get("reward_cost", {}))
        self._tiers      = tiers

    def score(self, risk: Dict[str, Any]) -> float:
        """Weighted risk score in [0, 1] from a tool's risk facts.

        Factors: destructiveness (damage if wrong), irreversibility (can't be undone),
        blast radius (how far the effect spreads), and commitment (resources/side
        effects it locks in even when reversible — why creating a VM warrants a y/n
        though it's undoable).
        """
        dest   = float(risk.get("destructiveness", 0.0))
        irr    = 0.0 if risk.get("reversible", True) else 1.0
        blast  = float(self.blast_scale.get(risk.get("blast", "none"), 0.0))
        commit = float(risk.get("commitment", 0.0))
        return (self.weights["destructiveness"] * dest
                + self.weights["irreversibility"] * irr
                + self.weights["blast"] * blast
                + self.weights["commitment"] * commit)

    def to_tier(self, risk_val: float) -> str:
        """Map a risk score to a tier by walking thresholds high → low."""
        if risk_val >= self.thresholds["double"]:
            return "double"
        if risk_val >= self.thresholds["name"]:
            return "name"
        if risk_val >= self.thresholds["normal"]:
            return "normal"
        if risk_val >= self.thresholds["acknowledge"]:
            return "acknowledge"
        return "none"

    def factors(self, risk: Dict[str, Any]) -> "tuple":
        """(factor rows, blast_label) for the debug breakdown — each factor's raw
        value + weight, so risk_breakdown can show its weighted contribution."""
        dest       = float(risk.get("destructiveness", 0.0))
        irr        = 0.0 if risk.get("reversible", True) else 1.0
        blast_name = risk.get("blast", "none")
        blast      = float(self.blast_scale.get(blast_name, 0.0))
        commit     = float(risk.get("commitment", 0.0))
        rows = [("destructiveness", dest, self.weights["destructiveness"]),
                ("irreversibility", irr, self.weights["irreversibility"]),
                ("blast", blast, self.weights["blast"]),
                ("commitment", commit, self.weights["commitment"])]
        return rows, blast_name

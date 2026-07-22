"""
tool_policy.py — the per-tool contract data + tier resolution.

Holds the ``tools`` map ({risk, verb, verify, pin, field}), the fleet action→tier
map, and the tier resolution over them (pin > formula), plus the registry
cross-checks (orphans, pinned disagreements). Composes a RiskFormula.
"""

from typing import Any, Dict, Optional

from .registry import _TOOL_SPECS, _TOOL_NAME_ARG
from .risk_formula import RiskFormula


class ToolPolicy:
    """The per-tool contract data: the ``tools`` map ({risk, verb, verify, pin, field}),
    the fleet action→tier map, and the tier resolution over them (pin > formula), plus
    the registry-cross-checks (orphans, pinned disagreements). Composes a RiskFormula.
    """

    def __init__(self, tools: Dict[str, Any], fleet_actions: Dict[str, str],
                 formula: RiskFormula):
        self.tools         = tools
        self.fleet_actions = fleet_actions
        self.formula       = formula

    def tool_risk(self, tool: str) -> Optional[Dict[str, Any]]:
        """The tool's risk facts as assessed by the active contract, or None (→ tier
        none). Risk is a contract JUDGMENT (lives in the .grgn), not a registry fact."""
        return (self.tools.get(tool) or {}).get("risk")

    def formula_tier(self, tool: str) -> Optional[str]:
        """The tier the FORMULA computes for a tool from its risk, ignoring any pin.
        'none' for an assessed-risk-free / unassessed tool; None for a tool absent
        from the registry."""
        if tool not in _TOOL_SPECS:
            return None
        risk = self.tool_risk(tool)
        return "none" if not risk else self.formula.to_tier(self.formula.score(risk))

    def resolve_tier(self, tool: str, args: Optional[Dict[str, Any]] = None) -> str:
        """The LIVE confirmation tier for a proposed tool call — the gate's answer.

        Resolution order: ``fleet`` is action-conditional; then a ``pin`` wins if set;
        otherwise the tier is COMPUTED from the contract's risk facts. A tool absent
        from the registry defaults to ``none``.
        """
        if tool == "fleet":
            action = ((args or {}).get("action") or "").strip().lower()
            return self.fleet_actions.get(action, "none")
        if tool not in _TOOL_SPECS:
            return "none"
        pin = (self.tools.get(tool) or {}).get("pin")
        if pin is not None:
            return pin
        risk = self.tool_risk(tool)
        return "none" if not risk else self.formula.to_tier(self.formula.score(risk))

    def success_criterion(self, tool: str) -> Optional[str]:
        """The contract's post-condition for a tool — what "done" means — or None."""
        return (self.tools.get(tool) or {}).get("verify")

    def confirm_meta(self, tool: str):
        """(field, verb) for a confirmable tool, or None. ``field`` names the target
        arg (registry-derived so it tracks the tool signature); ``verb`` is the
        contract's display verb, falling back to a humanized tool name."""
        if tool not in self.tools and tool not in _TOOL_SPECS:
            return None
        attr  = self.tools.get(tool) or {}
        field = attr.get("field") or self._registry_target_field(tool)
        return field, attr.get("verb") or tool.replace("_", " ")

    def _registry_target_field(self, tool: str) -> str:
        """Which arg names the tool's target, from the registry (default 'name')."""
        if tool in _TOOL_NAME_ARG:
            return _TOOL_NAME_ARG[tool]
        req = (_TOOL_SPECS.get(tool) or {}).get("req") or []
        return req[0] if req else "name"

    def orphan_entries(self) -> set:
        """Contract tool entries that name a tool absent from the registry — drift."""
        if not _TOOL_SPECS:
            return set()
        return set(self.tools) - set(_TOOL_SPECS)

    def pinned_disagreements(self) -> Dict[str, Dict[str, str]]:
        """Every pin that overrides the computed tier → {tool: {pin, formula}}."""
        out: Dict[str, Dict[str, str]] = {}
        for tool, attr in self.tools.items():
            pin = attr.get("pin")
            if pin is None:
                continue
            f = self.formula_tier(tool)
            if f is not None and f != pin:
                out[tool] = {"pin": pin, "formula": f}
        return out

"""claim_finding tool — validate + coerce a typed, model-proposed finding.

No-op at the executor beyond validation: the harness records it into the ledger
via the claim_finding yield-schema, and (for a type with an `assertion`) only if
guest_probe confirms it. A type without an assertion is the operator opting into
an unverified claim.
"""
from executor.tool_dispatch.context import console
from executor.tool_dispatch.tools.base import Tool
class ClaimFindingTool(Tool):
    names = ("claim_finding",)
    def run(self, args, ctx):
        try:
            from orchestrator.ai.planner.findings import claim_type as _ct, coerce_value as _cv
            spec = _ct(args.get("type"))
        except Exception:
            spec = None
        if spec is None:
            return {"success": False, "error": f"unknown claim type '{args.get('type')}'"}
        try:
            val = _cv(args.get("value"), spec.get("value_type", "string"))
        except (ValueError, TypeError):
            return {"success": False,
                    "error": f"claim value {args.get('value')!r} is not a {spec.get('value_type')}"}
        grounded = bool(spec.get("assertion"))
        evidence = (args.get("evidence") or "").strip()
        if not grounded and not evidence:
            # No probe CAN confirm this type, so a human must — and can't without
            # knowing where to look. Require the evidence up front.
            return {"success": False,
                    "error": f"'{args.get('type')}' can't be probe-verified — "
                             f"provide `evidence` (where/how you found it) so a human can check it."}
        result = {"success": True, "value": val, "type": args.get("type"),
                  "grounded": grounded, "evidence": evidence or None}
        if not ctx.verbose:
            tag = "pending probe" if grounded else f"UNVERIFIED claim · evidence: {evidence}"
            console.print(f"[dim]claim {args.get('type')}={val} ({tag})[/dim]")
        return result

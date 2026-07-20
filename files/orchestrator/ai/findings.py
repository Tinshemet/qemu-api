"""
findings.py — the Findings ledger: the Active Library's EPISTEMIC twin.

The Library holds what IS (system state); the Findings ledger holds what we've
LEARNED (observations, discoveries). It's populated deterministically from tool
outputs via a per-tool YIELD-SCHEMA (which fact a tool produces + where the value
lives in its result), and it does triple duty for the reward-cost engine:

  1. ACCEPTANCE oracle for info goals — "find web's IP" is done when the ledger
     has ip(web), not when the model says so.
  2. ANTI-REDISCOVERY grounding — the epistemic twin of the state carry-forward:
     if a fact is already known, don't re-run the tool that would learn it (stop
     re-scanning what you already know).
  3. COST repricing — a known fact is ~free, so a cost-aware planner prefers
     "read the cache" over "re-discover" (used once the CE gate lands, step 2).

For state goals the Library is the oracle (verified-completion, already built);
this is the missing half for discovery goals — it lands fully at the Conductor/
recon stage (scan_network, get_vm_ip), where results are observations, not owned
state. The schema is data (sits beside the catalog's `effect`), so a new yielding
tool declares its fact in one place.
"""
from typing import Any, Dict, Optional


# Per-tool yield-schema: {tool: {"fact": <template over args>, "value": <result key>,
#   "when": {<arg>: <val>} (optional gate), "verify": <probe template> (optional)}}.
# A `verify` template ("{name}:port_listening:{port}") makes the finding count only
# if a read-only guest_probe confirms it (deterministic finding-validation). Any fact
# recorded here can accept a goal via a `found:<fact>` root-predicate clause — the
# generic epistemic acceptance that generalizes mesh/reachable beyond connectivity.
DEFAULT_SCHEMA: Dict[str, Dict[str, Any]] = {
    "get_vm_ip":    {"fact": "ip({name})",        "value": "ip"},
    "scan_network": {"fact": "hosts({net_name})", "value": "hosts"},
    "fingerprint_vm": {"fact": "fingerprint({name})", "value": "fingerprint"},
    # Connectivity is an EPISTEMIC result, not owned state — it belongs here so a goal
    # like "make sure they all ping each other" has a checkable fact to accept against.
    # `when` gates the yield to the ping ACTION, so fleet create/add don't record a mesh.
    "fleet":      {"fact": "mesh({label})", "value": "all_reachable", "when": {"action": "ping"}},
    "guest_ping": {"fact": "reachable({name})", "value": "success"},
}


class Findings:
    """A ledger of learned facts: fact-key -> {value, source}."""

    def __init__(self):
        self._f: Dict[str, Dict[str, Any]] = {}

    def record(self, fact: str, value: Any, source: Optional[str] = None) -> None:
        self._f[fact] = {"value": value, "source": source}

    def has(self, fact: str) -> bool:
        return fact in self._f

    def invalidate(self, fact: str) -> None:
        """Drop a fact — it's no longer known/true (e.g. the world changed under it)."""
        self._f.pop(fact, None)

    def invalidate_about(self, entity: str) -> int:
        """Drop every fact MENTIONING `entity` — the staleness fix: after a mutation
        changes an entity, facts learned about it (ip(web), status(web)) are stale, so
        anti-rediscovery won't hand back a wrong answer. Returns how many were dropped."""
        if not entity:
            return 0
        stale = [k for k in self._f if entity in k]
        for k in stale:
            del self._f[k]
        return len(stale)

    def get(self, fact: str) -> Any:
        return (self._f.get(fact) or {}).get("value")

    def facts(self):
        return list(self._f)

    def render(self) -> str:
        """Compact grounding for the planner: what's already known (don't re-learn it)."""
        if not self._f:
            return ""
        items = ", ".join(f"{k}={v['value']}" for k, v in sorted(self._f.items()))
        return f"KNOWN FINDINGS (already learned — do NOT re-discover these): {items}"


def yield_fact(tool: str, args: Dict[str, Any], schema: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """The fact-KEY a tool call would produce per the schema, or None if it yields nothing.

    A `when` clause gates the yield to matching args (e.g. only `fleet` with
    action=ping produces a mesh fact) — so a multi-action tool doesn't record a bogus
    fact (and poison anti-rediscovery) on the actions that learn nothing."""
    spec = (schema or {}).get(tool)
    if not spec:
        return None
    when = spec.get("when")
    if when and any(str(args.get(k)) != str(v) for k, v in when.items()):
        return None
    try:
        return spec["fact"].format(**args)
    except Exception:
        return None


def extract_value(result: Any, spec: Dict[str, str]) -> Any:
    """Pull the yielded VALUE out of a tool result per the schema (falls back to the
    whole result). Deterministic validation is layered ON TOP via finding_probe_spec —
    a finding a tool would record can be required to pass an independent probe first."""
    key = spec.get("value")
    if isinstance(result, dict) and key in result:
        return result[key]
    return result


def finding_probe_spec(tool: str, args: Dict[str, Any],
                       schema: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """The independent-probe spec a tool's finding must pass BEFORE it's recorded —
    "vm:assertion:target" (e.g. "web01:port_listening:443"), formatted from the call
    args via the schema's optional ``verify`` template. None means the tool declares
    no verification, so its finding records as before (backward-compatible).

    This is deterministic finding-validation: a value read from a tool's (possibly
    free-text) output counts only if a read-only guest_probe independently confirms
    it — closing the "trust the extracted value" hole."""
    spec = (schema or {}).get(tool)
    if not spec or not spec.get("verify"):
        return None
    try:
        return spec["verify"].format(**args)
    except Exception:
        return None

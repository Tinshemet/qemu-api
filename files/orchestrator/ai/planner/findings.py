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
import json
import os as _os
import re
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
    # A model-PROPOSED, TYPED finding (see claim_types.json). The fact key and the
    # verify probe are derived from the claim's `type`, not a static template — so
    # yield_fact / finding_probe_spec special-case it below. This entry just tells
    # extract_value where the recorded value lives.
    "claim_finding": {"value": "value"},
}


def _load_claim_types() -> Dict[str, Dict[str, Any]]:
    """The data-driven claim-type registry (name → {value_type, assertion?, operand?})."""
    try:
        with open(_os.path.join(_os.path.dirname(__file__), "claim_types.json")) as f:
            return json.load(f).get("types", {}) or {}
    except Exception:
        return {}


def claim_type(name: str) -> Optional[Dict[str, Any]]:
    return _load_claim_types().get(name)


def coerce_value(raw: Any, value_type: str):
    """Coerce a claimed value to its declared type, or raise ValueError. Keeps the
    ledger typed (balance is an int, not the string '5000')."""
    if value_type in ("string", "str"):   return str(raw)
    if value_type in ("int", "integer"):  return int(raw)
    if value_type == "float":             return float(raw)
    if value_type in ("bool", "boolean"): return str(raw).strip().lower() in ("1", "true", "yes")
    return str(raw)


class _Blank(dict):
    """dict whose missing keys format to '' — so a template with an optional field
    (e.g. {value}) doesn't KeyError when the arg is absent."""
    def __missing__(self, key):
        return ""


def _fmt(template: str, args: Dict[str, Any]) -> str:
    return template.format_map(_Blank(args))


class Findings:
    """A ledger of learned facts: fact-key -> {value, source}."""

    def __init__(self):
        self._f: Dict[str, Dict[str, Any]] = {}

    def record(self, fact: str, value: Any, source: Optional[str] = None,
               evidence: Optional[str] = None) -> None:
        # Don't let a fresh unverified claim clobber a fact a human already CONFIRMED.
        existing = self._f.get(fact)
        if evidence and existing and existing.get("status") == "verified":
            return
        entry: Dict[str, Any] = {"value": value, "source": source}
        # An UNVERIFIED claim (a type with no probe) carries `evidence` — the operator's
        # note on where/how they found it — and enters as `pending`: recorded and visible,
        # but NOT usable to close a goal until a human confirms it (see usable/confirm).
        if evidence:
            entry["evidence"] = evidence
            entry["status"] = "pending"
        self._f[fact] = entry

    def usable(self, fact: str) -> bool:
        """The acceptance / anti-rediscovery gate: known, truthy, AND not an
        unconfirmed claim. A `pending` claim is recorded (so it's visible and not
        re-claimed) but can't satisfy a goal until a human marks it verified."""
        e = self._f.get(fact)
        return bool(e) and bool(e.get("value")) and e.get("status") != "pending"

    def is_pending(self, fact: str) -> bool:
        return (self._f.get(fact) or {}).get("status") == "pending"

    def confirm(self, fact: str) -> bool:
        """A human marks a pending claim TRUE — it becomes usable. Returns False if
        the fact isn't a pending claim."""
        e = self._f.get(fact)
        if not e or e.get("status") != "pending":
            return False
        e["status"] = "verified"
        return True

    def persistable(self) -> Dict[str, Dict[str, Any]]:
        """The claim entries worth carrying across runs — pending (awaiting review)
        and verified (human-confirmed). Probe facts (no status) are NOT persisted:
        they're cheap to re-derive and go stale."""
        return {k: dict(v) for k, v in self._f.items()
                if v.get("status") in ("pending", "verified")}

    def merge(self, entries: Optional[Dict[str, Dict[str, Any]]]) -> None:
        """Seed this ledger from a persisted store — confirmed claims come back
        usable, pending ones stay pending. Never clobbers a fact already present."""
        for k, v in (entries or {}).items():
            if k not in self._f and isinstance(v, dict) and "value" in v:
                self._f[k] = dict(v)

    def has(self, fact: str) -> bool:
        return fact in self._f

    def invalidate(self, fact: str) -> None:
        """Drop a fact — it's no longer known/true (e.g. the world changed under it)."""
        self._f.pop(fact, None)

    def invalidate_about(self, entity: str) -> int:
        """Drop every fact MENTIONING `entity` — the staleness fix: after a mutation
        changes an entity, facts learned about it (ip(web), status(web)) are stale, so
        anti-rediscovery won't hand back a wrong answer. Returns how many were dropped.

        Matches `entity` against a fact's parenthesised operands EXACTLY, not as a
        raw substring — otherwise invalidating `web` would also nuke `ip(web2)` and
        `reachable(webserver)` (and a one-char entity would wipe the whole ledger)."""
        if not entity:
            return 0
        def _mentions(key: str) -> bool:
            m = re.search(r"\(([^)]*)\)", key)
            if not m:
                return False
            return entity in [o.strip() for o in m.group(1).split(",")]
        stale = [k for k in self._f if _mentions(k)]
        for k in stale:
            del self._f[k]
        return len(stale)

    def get(self, fact: str) -> Any:
        return (self._f.get(fact) or {}).get("value")

    def evidence(self, fact: str) -> Optional[str]:
        return (self._f.get(fact) or {}).get("evidence")

    def facts(self):
        return list(self._f)

    def claims_for_review(self):
        """The unverified claims a HUMAN still needs to check — every `pending` fact
        (a machine-unverifiable claim, recorded with the operator's evidence pointer).
        Returns [{fact, value, evidence, source}], sorted, so a run surfaces exactly
        what the machine couldn't confirm and where the claimant said to look."""
        return [
            {"fact": k, "value": v["value"], "evidence": v.get("evidence"), "source": v.get("source")}
            for k, v in sorted(self._f.items()) if v.get("status") == "pending"
        ]

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
    if tool == "claim_finding":                       # typed claim → type(value)
        ct = claim_type(args.get("type"))
        if ct is None or args.get("value") in (None, ""):
            return None
        return f"{args['type']}({args['value']})"
    spec = (schema or {}).get(tool)
    if not spec:
        return None
    when = spec.get("when")
    if when and any(str(args.get(k)) != str(v) for k, v in when.items()):
        return None
    try:
        return _fmt(spec["fact"], args)
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
    if tool == "claim_finding":                       # typed claim → probe from claim_types
        ct = claim_type(args.get("type"))
        if not ct or not ct.get("assertion"):
            return None                                # no probe for this type → unverified claim
        spec_str = f"{args.get('vm')}:{ct['assertion']}:{args.get('value')}"
        if ct.get("operand"):                          # two-operand assertion (e.g. host_reachable)
            spec_str += f":{args.get('operand', '')}"
        return spec_str
    spec = (schema or {}).get(tool)
    if not spec or not spec.get("verify"):
        return None
    try:
        return _fmt(spec["verify"], args)
    except Exception:
        return None

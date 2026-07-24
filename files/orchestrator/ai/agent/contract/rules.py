"""rules.py — deterministic precedence + coherence for a contract's weighted rules.

A signed contract carries prose rules as ``[{text, weight}]`` with weight semantics:
    0        = wildcard / void-if-broken — INVIOLABLE (breaking it voids the contract)
    1        = default
    higher   = weaker (more waivable)

Until now the rules were only RENDERED — never resolved — so a rule set could silently
contradict itself: the same rule at two different weights (which one governs?), a
duplicate, an out-of-range weight. This module gives the rules a deterministic
precedence order (so there is never an unresolved tie — the anti-cycle guarantee) and
reports the coherence problems that ``review()`` must refuse before signing.

Prose SEMANTICS are deliberately not judged (undecidable, and a weak model grading them
would be a second bad draw) — only the STRUCTURE of the weighting is checked.
"""
from typing import Any, Dict, List, Optional


def _norm(text: Any) -> str:
    """Whitespace/case-normalized rule text, so trivially-different duplicates collide."""
    return " ".join(str(text or "").strip().lower().split())


def _weight(rule: Dict[str, Any]) -> Optional[float]:
    """A rule's numeric weight, or None if it isn't a number (a coherence problem)."""
    try:
        return float(rule.get("weight", 1))
    except (TypeError, ValueError):
        return None


def resolve(rules: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """The rules in deterministic PRECEDENCE order — strongest first (lowest weight, 0 =
    inviolable), ties broken by declaration index (stable). Because the order is a total
    order over (weight, index), precedence is never ambiguous and never cyclic. Malformed
    (non-numeric-weight) rules are dropped here — ``conflicts`` reports them. Returns
    ``[{text, weight, rank, inviolable}]`` with rank 0 = highest precedence."""
    scored = []
    for i, r in enumerate(rules or []):
        w = _weight(r)
        if w is None:
            continue
        scored.append((w, i, r))
    scored.sort(key=lambda x: (x[0], x[1]))            # weight asc, then declaration order
    return [{"text": r.get("text", ""), "weight": w, "rank": rank, "inviolable": w == 0}
            for rank, (w, i, r) in enumerate(scored)]


def conflicts(rules: Optional[List[Dict[str, Any]]]) -> List[str]:
    """Every way the weighted rule set silently contradicts itself — the coherence
    problems ``review()`` refuses before signing. Deterministic and structural: a
    non-numeric or negative weight, empty text, a duplicate rule, or (the core silent
    contradiction) the SAME rule declared at two different weights."""
    problems: List[str] = []
    by_text: Dict[str, float] = {}                     # normalized text → its first weight
    for i, r in enumerate(rules or []):
        w = _weight(r)
        text = _norm(r.get("text"))
        if w is None:
            problems.append(f"rule {i} has a non-numeric weight: {r.get('weight')!r}")
            continue
        if w < 0:
            problems.append(f"rule {i} has a negative weight ({w}); weights are ≥ 0 (0 = inviolable)")
        if not text:
            problems.append(f"rule {i} has empty text")
            continue
        if text in by_text:
            if by_text[text] != w:
                problems.append(
                    f"rule declared at two weights ({by_text[text]} and {w}) — "
                    f"which governs is undefined: {r.get('text')!r}")
            else:
                problems.append(f"duplicate rule (same text and weight): {r.get('text')!r}")
        else:
            by_text[text] = w
    return problems

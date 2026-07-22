"""
watchdog.py — the deterministic farming/loop watchdog (reward-cost step 4).

The "farming hack": an agent repeats a rewarded (or just repeatable) action to bank
reward or spin in place. The reward-cost design already makes farming structurally
hard (reward books on branch CLOSURE, never intrinsic to a tool), but this is the
safety net for the cases that slip through — and it is DELIBERATELY not an AI (an AI
watchdog can itself be farmed); it's a plain ledger monitor.

Key insight from the design: key on **zero-progress REPETITION of the same action
signature**, NOT raw frequency. Raw frequency mis-fires on legit bulk work (create 50
VMs, scan 200 hosts) — but each of those has a DISTINCT signature and makes progress
(a new VM, a new finding). A farm/loop repeats the SAME signature while nothing
advances (no acceptance predicate moves, no new finding). After `max_repeats`
no-progress repeats of a signature it is THROTTLED — a REVERSIBLE interim block for
the alert→amendment window; the permanent fix is a human amendment (reset/lift).
"""
from typing import Any, Dict, List, Optional


def _sig(tool: str, args: Dict[str, Any]) -> str:
    inner = ",".join(f"{k}={args[k]}" for k in sorted(args or {}))
    return f"{tool}({inner})"


class Watchdog:
    """Flags a (tool, args) signature that repeats without progress, and throttles it."""

    def __init__(self, max_repeats: int = 2):
        self.max_repeats = max_repeats
        self._seen: set = set()               # signatures ever executed
        self._noprog: Dict[str, int] = {}     # consecutive no-progress repeats per signature
        self._last: Dict[str, Any] = {}       # last result per signature (for result-change progress)
        self._throttled: set = set()
        self.alerts: List[str] = []

    def throttled(self, tool: str, args: Dict[str, Any]) -> bool:
        """Should this call be blocked (its signature is throttled)?"""
        return _sig(tool, args) in self._throttled

    def observe(self, tool: str, args: Dict[str, Any], new_finding: bool = False, result: Any = None) -> bool:
        """Record an execution. Progress = the signature is NEW (first-ever work), OR it
        produced a new finding, OR its RESULT CHANGED from last time (a legit re-read of
        moving state isn't a loop). A no-progress repeat increments the counter; crossing
        `max_repeats` throttles the signature (once) and raises an alert."""
        sig = _sig(tool, args)
        result_changed = (sig in self._last) and (self._last[sig] != result)
        self._last[sig] = result
        progressed = (sig not in self._seen) or new_finding or result_changed
        self._seen.add(sig)
        if progressed:
            self._noprog[sig] = 0
            return False
        self._noprog[sig] = self._noprog.get(sig, 0) + 1
        if self._noprog[sig] >= self.max_repeats and sig not in self._throttled:
            self._throttled.add(sig)
            self.alerts.append(
                f"farming/loop: {sig} repeated {self._noprog[sig]}x with no progress — throttled")
            return True
        return False

    def reset(self, tool: Optional[str] = None, args: Optional[Dict[str, Any]] = None) -> None:
        """Reversible: lift a throttle (a specific signature, or all). The design's
        interim throttle is temporary; a human amendment is the permanent resolution."""
        if tool is None:
            self._throttled.clear()
            self._noprog.clear()
            return
        sig = _sig(tool, args or {})
        self._throttled.discard(sig)
        self._noprog.pop(sig, None)

    def status(self) -> Dict[str, Any]:
        return {"throttled": sorted(self._throttled), "alerts": list(self.alerts)}

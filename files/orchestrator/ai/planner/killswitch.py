"""
killswitch.py — the safeword kill-switch: an INFRASTRUCTURAL abort.

The contract's safeword is the operator's emergency stop. It MUST be enforced by the
harness, never by AI cooperation: a drifted or adversarial agent could ignore a word
it's merely "supposed" to honor, so the run loop checks a plain flag between steps and
force-stops — the agent gets no say. Tripping it suspends the run and PRESERVES the
ledger (the forensic "what had it done?" record), rather than deleting.

Two ways to trip it:
  - `safeword(word)` — trips iff `word` matches the armed safeword (the operator types
    it; the harness, not the model, compares).
  - `abort(reason)`  — unconditional out-of-band kill (a signal, a dead-man's switch,
    a control-plane call) that needs no word.

Trigger wiring (attended vs headless) is provided by `watch_safeword` (a background
stdin reader) and `on_signal` (a POSIX signal handler); both just flip this flag.
"""
import sys
from typing import Any, Optional


class KillSwitch:
    def __init__(self, safeword: Optional[str] = None):
        self._safeword = (safeword or "").strip().lower() or None
        self.tripped = False
        self.reason: Optional[str] = None

    @property
    def armed(self) -> bool:
        return self._safeword is not None

    def arm(self, safeword: Optional[str]) -> None:
        self._safeword = (safeword or "").strip().lower() or None

    def safeword(self, word: Any) -> bool:
        """Trip iff `word` matches the armed safeword (case-insensitive). The harness
        does this compare — the model never sees or gates it."""
        if self._safeword and str(word).strip().lower() == self._safeword:
            self.tripped, self.reason = True, "safeword"
            return True
        return False

    def abort(self, reason: str = "operator") -> None:
        """Unconditional out-of-band kill — no word needed (signal / dead-man / control plane)."""
        self.tripped, self.reason = True, reason

    def reset(self) -> None:
        self.tripped, self.reason = False, None


def watch_safeword(ks: KillSwitch, stream=None):
    """Attended runs: a daemon thread that reads lines and trips `ks` on the safeword.
    Returns the thread (already started). The reader is out-of-band from the agent."""
    import threading
    stream = stream or sys.stdin

    def _loop():
        try:
            for line in stream:
                if ks.safeword(line) or ks.tripped:
                    return
        except Exception:
            return

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t


def on_signal(ks: KillSwitch, sig=None):
    """Headless runs: trip `ks` on a POSIX signal (default SIGUSR1) — an out-of-band
    kill from another process. Returns the previous handler."""
    import signal as _signal
    sig = sig or _signal.SIGUSR1
    return _signal.signal(sig, lambda *_: ks.abort("signal"))

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
        self._on_checkin = None            # a liveness observer (e.g. a dead-man's timer reset)

    @property
    def armed(self) -> bool:
        return self._safeword is not None

    def arm(self, safeword: Optional[str]) -> None:
        self._safeword = (safeword or "").strip().lower() or None

    def on_checkin(self, cb) -> None:
        """Register a liveness observer — called on every checkin(). A DeadMansSwitch uses
        this to reset its timer whenever the run shows a sign of life."""
        self._on_checkin = cb

    def checkin(self) -> None:
        """Signal a sign of life (the run made progress, or the operator is present) —
        resets any armed dead-man's timer. A no-op when nothing is observing."""
        if self._on_checkin is not None:
            self._on_checkin()

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


class DeadMansSwitch:
    """The UNATTENDED counterpart to the safeword (which is the ATTENDED stop): a timer
    that trips `ks` if too long passes without a sign of life, so a Conductor left running
    can't drift forever. Every `checkin()` (the run made progress, or the operator is
    present) resets the countdown; if `timeout` seconds elapse with no check-in, it fires
    `ks.abort("deadman")` out-of-band. It registers itself as the kill-switch's checkin
    observer, so the harness only has to call `ks.checkin()` — it need not know a dead-man
    is armed. Idempotent stop(); best-effort (a timer failure must never brick a run)."""

    def __init__(self, ks: KillSwitch, timeout: float):
        import threading
        self._ks = ks
        self._timeout = float(timeout)
        self._lock = threading.Lock()
        self._timer = None
        self._stopped = False

    def start(self) -> "DeadMansSwitch":
        self._ks.on_checkin(self.checkin)      # route every ks.checkin() to our reset
        self.checkin()                          # arm the first countdown
        return self

    def checkin(self) -> None:
        import threading
        with self._lock:
            if self._stopped:
                return
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._timeout, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self) -> None:
        # No check-in within the window → unattended drift; stop out-of-band (the flag the
        # run loop polls between steps). Preserves the ledger, like any other trip.
        self._ks.abort("deadman")

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

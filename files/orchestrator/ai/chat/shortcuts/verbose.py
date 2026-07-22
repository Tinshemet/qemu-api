"""verbose / debug [on|off] — toggle the per-tool-call debug view."""

from typing import List

from .base import Shortcut
from . import context as ctx


class VerboseShortcut(Shortcut):
    # A literal word set (not a config phrase list): bare word toggles, +on/off sets.
    _WORDS = ("verbose", "debug", "verbose on", "debug on", "verbose off", "debug off")

    def matches(self, ui: str) -> bool:
        return ui in self._WORDS

    def run(self, ui: str, messages: List[dict], runtime_drift_count: int, verbose: bool) -> None:
        want = ctx.get_verbose() if ui in ("verbose", "debug") else ui.endswith("on")  # bare word = toggle
        if ui in ("verbose", "debug"):
            want = not want
        ctx.set_verbose(want)
        ctx.console.print(f"[dim]Verbose/debug view {'ENABLED' if want else 'disabled'} "
                          f"— per tool call: risk weights, scrutiny tier, reward-cost knobs.[/dim]")

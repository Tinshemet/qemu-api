"""auto-clear on|off — toggle clearing the session on next start."""

from typing import List

from .base import Shortcut
from . import context as ctx


class AutoClearOnShortcut(Shortcut):
    config_key = "auto_clear_on"

    def run(self, ui: str, messages: List[dict], runtime_drift_count: int, verbose: bool) -> None:
        ctx.set_auto_clear(True)
        ctx.console.print("[dim]Auto-clear enabled — session will be cleared on next start.[/dim]")


class AutoClearOffShortcut(Shortcut):
    config_key = "auto_clear_off"

    def run(self, ui: str, messages: List[dict], runtime_drift_count: int, verbose: bool) -> None:
        ctx.set_auto_clear(False)
        ctx.console.print("[dim]Auto-clear disabled.[/dim]")

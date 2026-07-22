"""clear-session — wipe the saved AI session and the live message history."""

from typing import List

from .base import Shortcut
from . import context as ctx


class ClearSessionShortcut(Shortcut):
    config_key = "clear_session"

    def run(self, ui: str, messages: List[dict], runtime_drift_count: int, verbose: bool) -> None:
        ctx.clear_session()
        messages.clear()
        ctx.console.print("[dim]Session cleared.[/dim]")

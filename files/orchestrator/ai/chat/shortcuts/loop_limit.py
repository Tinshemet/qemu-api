"""loop-limit — show/set/reset the per-turn agentic tool-loop cap."""

from typing import List

from .base import Shortcut
from . import context as ctx


class LoopLimitShortcut(Shortcut):
    config_key = "loop_limit"

    def matches(self, ui: str) -> bool:
        return any(ui == s or ui.startswith(s + " ") for s in ctx.SHORTCUTS["loop_limit"])

    def run(self, ui: str, messages: List[dict], runtime_drift_count: int, verbose: bool) -> None:
        # _LOOP_MAX is the live cap on cli (chat_loop reads it, the test harness patches
        # it). cli imports this package, so import cli lazily here to avoid a cycle.
        import orchestrator.ai.chat.cli as cli
        matched = next((s for s in ctx.SHORTCUTS["loop_limit"] if ui == s or ui.startswith(s + " ")), None)
        inline  = ui[len(matched):].strip()
        if inline:
            new = inline
        else:
            ctx.console.print(f"[dim]Current tool loop limit: [bold]{cli._LOOP_MAX}[/bold] "
                              f"(default: {ctx.CFG['chat']['tool_loop_max']})[/dim]")
            ctx.console.print("[dim]Enter a number to set a new limit, or press Enter to clear the override.[/dim]")
            try:
                new = ctx.console.input("[bold cyan]New limit:[/bold cyan] ").strip()
            except (KeyboardInterrupt, EOFError):
                return
        if new == "":
            ctx.set_loop_max(None)
            cli._LOOP_MAX = ctx.CFG["chat"]["tool_loop_max"]
            ctx.console.print(f"[dim]Loop limit reset to default ({cli._LOOP_MAX}).[/dim]")
        elif new.isdigit() and int(new) > 0:
            cli._LOOP_MAX = int(new)
            ctx.set_loop_max(cli._LOOP_MAX)
            ctx.console.print(f"[dim]Loop limit set to {cli._LOOP_MAX}.[/dim]")
        else:
            ctx.console.print("[dim]Invalid input — loop limit unchanged.[/dim]")

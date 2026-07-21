"""commands/setup_done.py — gorgon setup-done <vm> (mark stealth setup complete)."""

from client.cli.commands.base import Command
from client.cli.commands.context import _require_manager, console, manager


class SetupDoneCommand(Command):
    names = ("setup-done",)
    min_args = 1

    def run(self, cmd, rest, verbose):
        _require_manager()
        r     = manager.mark_stealth_done(rest[0])
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

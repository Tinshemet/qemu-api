"""commands/clone.py — gorgon clone <src> <dst>."""

from client.cli.commands.base import Command
from client.cli.commands.context import _require_manager, console, manager


class CloneCommand(Command):
    names = ("clone",)
    min_args = 2

    def run(self, cmd, rest, verbose):
        _require_manager()
        r     = manager.clone_vm(rest[0], rest[1])
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

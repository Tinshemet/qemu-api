"""commands/resize.py — gorgon resize <vm> <gb>."""

from client.cli.commands.base import Command
from client.cli.commands.context import _require_manager, console, manager


class ResizeCommand(Command):
    names = ("resize",)
    min_args = 2

    def run(self, cmd, rest, verbose):
        _require_manager()
        r     = manager.resize_disk(rest[0], 0, int(rest[1]))
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

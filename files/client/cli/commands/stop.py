"""commands/stop.py — gorgon stop <vm>."""

from client.cli.commands.base import Command
from client.cli.commands.context import _require_manager, console, manager, pp


class StopCommand(Command):
    names = ("stop",)
    min_args = 1

    def run(self, cmd, rest, verbose):
        _require_manager()
        r     = manager.stop_vm(rest[0])
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")
        pp(r, verbose)

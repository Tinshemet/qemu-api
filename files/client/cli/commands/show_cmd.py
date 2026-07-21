"""commands/show_cmd.py — gorgon show-cmd <vm> (print the QEMU command)."""

from client.cli.commands.base import Command
from client.cli.commands.context import _require_manager, console, manager, Panel


class ShowCmdCommand(Command):
    names = ("show-cmd",)
    min_args = 1

    def run(self, cmd, rest, verbose):
        _require_manager()
        r = manager.print_command(rest[0])
        if r.get("success"):
            console.print(Panel(r["command"], title="QEMU Command", border_style="cyan"))
        else:
            console.print(f"[error]{r.get('error', 'Unknown error')}[/error]")

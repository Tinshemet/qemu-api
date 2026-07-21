"""commands/isos.py — gorgon isos (scan common ISO locations)."""

from client.cli.commands.base import Command
from client.cli.commands.context import _require_manager, box, console, manager, Table


class IsosCommand(Command):
    names = ("isos",)

    def run(self, cmd, rest, verbose):
        _require_manager()
        isos = manager.scan_isos()
        if isos:
            t = Table(box=box.ROUNDED, border_style="cyan")
            t.add_column("File")
            t.add_column("Size")
            t.add_column("Path", style="dim")
            for iso in isos:
                t.add_row(iso["name"], f"{iso['size_gb']}GB", iso["path"])
            console.print(t)
        else:
            console.print("[bold yellow]No ISOs found in common locations.[/bold yellow]")

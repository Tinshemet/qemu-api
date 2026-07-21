"""commands/delete.py — gorgon delete <vm> (confirms first)."""

from client.cli.commands.base import Command
from client.cli.commands.context import _require_manager, console, manager


class DeleteCommand(Command):
    names = ("delete",)
    min_args = 1

    def run(self, cmd, rest, verbose):
        _require_manager()
        confirm = console.input(
            f"[bold yellow]⚠ Delete '{rest[0]}'? This cannot be undone.[/bold yellow] [y/N]: "
        ).strip().lower()
        if confirm in ("y", "yes"):
            r     = manager.delete_vm(rest[0])
            style = "success" if r.get("success") else "error"
            console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")
        else:
            console.print("[dim]Cancelled.[/dim]")

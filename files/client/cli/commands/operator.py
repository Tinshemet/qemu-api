"""commands/operator.py — gorgon operator add|list|remove <username>."""

import getpass

from client.cli.commands.base import Command
from client.cli.commands.context import _auth_store, console


class OperatorCommand(Command):
    names = ("operator",)
    min_args = 1

    def run(self, cmd, rest, verbose):
        if _auth_store is None:
            console.print("[bold red]Auth package unavailable on this checkout.[/bold red]")
            return
        sub = rest[0]
        if sub == "add" and len(rest) >= 2:
            pw = getpass.getpass("Password: ")
            r  = _auth_store.create_operator(rest[1], pw)
            console.print(f"[green]Operator '{rest[1]}' created.[/green]" if r.get("success")
                          else f"[bold red]{r.get('error')}[/bold red]")
        elif sub == "list":
            for u in _auth_store.list_operators():
                console.print(f"  {u}")
        elif sub == "remove" and len(rest) >= 2:
            r = _auth_store.delete_operator(rest[1])
            console.print(f"[green]Operator '{rest[1]}' removed.[/green]" if r.get("success")
                          else f"[bold red]{r.get('error')}[/bold red]")
        else:
            console.print("[yellow]Usage: gorgon operator add|list|remove <username>[/yellow]")

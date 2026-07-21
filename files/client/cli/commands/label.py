"""commands/label.py — gorgon label add|remove <vm> <label> | label list."""

from client.cli.commands.base import Command
from client.cli.commands.context import _require_manager, console, manager, pp, render_fleets


class LabelCommand(Command):
    names = ("label",)

    def run(self, cmd, rest, verbose):
        # label add <vm> <label> · label remove <vm> <label> · label list
        _require_manager()
        sub = rest[0] if rest else None
        if sub in ("add", "remove") and len(rest) >= 3:
            r = (manager.add_label if sub == "add" else manager.remove_label)(rest[1], rest[2])
            style = "green" if r.get("success") else "red"
            console.print(f"[bold {style}]{r.get('message', r.get('error', 'unknown error'))}[/bold {style}]")
            pp(r, verbose)
        elif sub == "list":
            render_fleets(manager.list_labels().get("usage", {}))
        else:
            console.print("[bold red]Usage: gorgon label add|remove <vm> <label>  |  "
                          "gorgon label list[/bold red]")

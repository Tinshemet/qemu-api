"""commands/help_cmd.py — gorgon help / --help / -h."""

from client.cli.commands.base import Command
from client.cli.commands.context import _allowed_tools, console, Panel


class HelpCommand(Command):
    names = ("help", "--help", "-h")

    def run(self, cmd, rest, verbose):
        from shared.command_help import load_local_catalog, render_terminal_panel
        catalog, order = load_local_catalog()
        if catalog is None:
            console.print("[dim]Command list unavailable — the executor package "
                          "could not be loaded.[/dim]")
        else:
            body = render_terminal_panel(catalog, _allowed_tools(), order)
            body += (
                "\n\n[bold cyan]Flags[/bold cyan]\n"
                "  -v                             Verbose / raw JSON output\n"
                "  -cu                            Custom mode: skip product verification\n"
                "  -cs                            Clear the saved session first"
            )
            console.print(Panel(body, title="gorgon help", border_style="cyan"))

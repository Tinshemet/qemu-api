"""commands/guest_setup.py — gorgon guest-setup <vm> (generate + serve the setup script)."""

from client.cli.commands.base import Command
from client.cli.commands.context import (
    _require_manager, _show_stealth_popup, console, manager, Panel,
)


class GuestSetupCommand(Command):
    names = ("guest-setup",)
    min_args = 1

    def run(self, cmd, rest, verbose):
        _require_manager()
        r = manager.generate_guest_setup(rest[0])
        if not r.get("success"):
            console.print(f"[error]{r['error']}[/error]")
            return
        console.print(Panel(
            f"[bold]Script generated:[/bold] {r['path']}\n\n"
            f"[dim]Copy this to the VM and run it, or use:[/dim]\n"
            f"[bold]gorgon launch {rest[0]}[/bold]  (auto-serves on first boot)",
            title="Guest Setup", border_style="cyan",
        ))
        _show_stealth_popup(rest[0], r.get("setup_cmd", ""))

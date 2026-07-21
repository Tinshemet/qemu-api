"""commands/fleet.py — gorgon fleet [<label> [exec|ping|status|stop|launch ...]]."""

from client.cli.commands.base import Command
from client.cli.commands.context import _require_manager, console, manager, pp, render_fleet, render_fleets


class FleetCommand(Command):
    names = ("fleet",)

    def run(self, cmd, rest, verbose):
        # fleet                       → list current fleets (labels → member VMs)
        # fleet <label>               → preview members of one fleet
        # fleet <label> exec <cmd...> → run a command on every member
        # fleet <label> stop|launch|ping|status → broadcast that action
        _require_manager()
        if not rest:
            render_fleets(manager.list_labels().get("usage", {}))
            return
        label  = rest[0]
        action = rest[1] if len(rest) > 1 else None
        if action is None:
            r = manager.fleet(label, "status")
            if r.get("results"):
                render_fleet(r)
            else:
                console.print(f"[yellow]{r.get('error', 'No members.')}[/yellow]")
            return
        if action == "exec":
            if len(rest) < 3:
                console.print("[bold red]Usage: gorgon fleet <label> exec <command>[/bold red]")
                return
            r = manager.fleet(label, "exec", command=" ".join(rest[2:]))
        elif action in ("ping", "status", "stop", "launch"):
            r = manager.fleet(label, action)
        else:
            console.print(f"[bold red]Unknown fleet action '{action}'. "
                          f"Use: exec, ping, status, stop, launch.[/bold red]")
            return
        render_fleet(r)
        pp(r, verbose)

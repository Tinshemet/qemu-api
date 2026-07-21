"""commands/monitor.py — gorgon monitor [vm|all]."""

from client.cli.commands.base import Command
from client.cli.commands.context import _require_manager, manager, pp, render_monitor


class MonitorCommand(Command):
    names = ("monitor",)

    def run(self, cmd, rest, verbose):
        _require_manager()
        name = rest[0] if rest else "all"
        r    = manager.monitor_all() if name == "all" else manager.monitor_vm(name)
        if isinstance(r, dict) and "state" in r:
            render_monitor(r)
        else:
            for v in (r.values() if isinstance(r, dict) else [r]):
                render_monitor(v)
        pp(r, verbose)

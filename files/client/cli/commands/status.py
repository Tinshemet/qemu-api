"""commands/status.py — gorgon status <vm>."""

from client.cli.commands.base import Command
from client.cli.commands.context import _require_manager, manager, pp, render_status


class StatusCommand(Command):
    names = ("status",)
    min_args = 1

    def run(self, cmd, rest, verbose):
        _require_manager()
        r = manager.vm_status(rest[0])
        render_status(r)
        pp(r, verbose)

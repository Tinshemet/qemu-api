"""commands/list_vms.py — gorgon list."""

from client.cli.commands.base import Command
from client.cli.commands.context import _require_manager, manager, pp, render_vm_list


class ListCommand(Command):
    names = ("list",)

    def run(self, cmd, rest, verbose):
        _require_manager()
        vms = manager.list_vms()
        render_vm_list(vms)
        pp(vms, verbose)

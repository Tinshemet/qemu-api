"""commands/network.py — gorgon network list|create|delete|add [name] [vm]."""

import json

from client.cli.commands.base import Command
from client.cli.commands.context import _require_manager, console, manager


class NetworkCommand(Command):
    names = ("network",)

    def run(self, cmd, rest, verbose):
        _require_manager()
        sub = rest[0] if rest else "list"
        if sub == "list":
            console.print_json(json.dumps(manager.list_networks(), default=str))
        elif sub == "create" and len(rest) >= 2:
            console.print_json(json.dumps(manager.create_network(rest[1]), default=str))
        elif sub == "delete" and len(rest) >= 2:
            console.print_json(json.dumps(manager.delete_network(rest[1]), default=str))
        elif sub == "add" and len(rest) >= 3:
            console.print_json(json.dumps(manager.add_vm_to_network(rest[1], rest[2]), default=str))
        else:
            console.print("[dim]Usage: network list|create|delete|add [name] [vm][/dim]")

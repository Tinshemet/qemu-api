"""commands/config.py — gorgon config <vm> (show VM config JSON)."""

import json

from client.cli.commands.base import Command
from client.cli.commands.context import _require_manager, console, manager


class ConfigCommand(Command):
    names = ("config",)
    min_args = 1

    def run(self, cmd, rest, verbose):
        _require_manager()
        r = manager.show_config(rest[0])
        if r.get("success"):
            console.print_json(json.dumps(r["config"], default=str))
        else:
            console.print(f"[error]{r['error']}[/error]")

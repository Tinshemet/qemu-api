"""commands/snapshot.py — gorgon snapshot list|create|restore|delete <vm> [tag]."""

from client.cli.commands.base import Command
from client.cli.commands.context import _require_manager, console, manager, render_snapshots


class SnapshotCommand(Command):
    names = ("snapshot",)
    min_args = 2

    def run(self, cmd, rest, verbose):
        _require_manager()
        sub = rest[0]
        if sub == "list" and len(rest) >= 2:
            r = manager.snapshot_list(rest[1])
            render_snapshots(r)
        elif sub == "create" and len(rest) >= 3:
            r = manager.snapshot_create(rest[1], rest[2])
            style = "success" if r.get("success") else "error"
            console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")
        elif sub == "restore" and len(rest) >= 3:
            r = manager.snapshot_restore(rest[1], rest[2])
            style = "success" if r.get("success") else "error"
            console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")
        elif sub == "delete" and len(rest) >= 3:
            r = manager.snapshot_delete(rest[1], rest[2])
            style = "success" if r.get("success") else "error"
            console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")
        else:
            console.print("[dim]Usage: snapshot list|create|restore|delete <vm> [tag][/dim]")

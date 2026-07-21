"""admin/commands/kill.py — force-kill a VM (SIGKILL). Same as stop with force=True."""

from admin.commands.stop import StopCommand


class KillCommand(StopCommand):
    names = ("kill",)
    force = True

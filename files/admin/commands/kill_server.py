"""admin/commands/kill_server.py — SIGKILL the local orchestrator."""

import os
import signal

from admin import server_control
from admin.commands.base import Command, Result


class KillServerCommand(Command):
    names = ("kill-server",)

    def run(self, args: list) -> Result:
        pid = server_control.local_pid()
        if pid:
            os.kill(pid, signal.SIGKILL)
            return Result(f"SIGKILL → pid {pid}")
        return Result("orchestrator not found on this machine")

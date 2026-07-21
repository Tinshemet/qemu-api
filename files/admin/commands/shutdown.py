"""admin/commands/shutdown.py — SIGTERM the local orchestrator."""

import os
import signal

from admin import server_control
from admin.commands.base import Command, Result


class ShutdownCommand(Command):
    names = ("shutdown", "shutdown-server")

    def run(self, args: list) -> Result:
        pid = server_control.local_pid()
        if pid:
            os.kill(pid, signal.SIGTERM)
            return Result(f"SIGTERM → pid {pid}")
        return Result("orchestrator not found on this machine")

"""admin/commands/start_server.py — start the orchestrator on this machine."""

from admin import config, server_control
from admin.commands.base import Command, Result


class StartServerCommand(Command):
    names = ("start-server",)

    def run(self, args: list) -> Result:
        pid = server_control.local_pid()
        if pid:
            return Result(f"already running locally (pid {pid})")
        p = server_control.spawn_server()
        return Result(f"server started (pid {p})  logs: {config.LOG_PATH}" if p
                      else f"may have failed — check {config.LOG_PATH}")

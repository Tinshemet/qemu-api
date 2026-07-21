"""admin/commands/restart.py — restart the local orchestrator (stop + start)."""

import os
import signal
import time

from admin import config, server_control
from admin.commands.base import Command, Result


class RestartCommand(Command):
    names = ("restart", "restart-server")

    def run(self, args: list) -> Result:
        pid = server_control.local_pid()
        if pid:
            os.kill(pid, signal.SIGTERM)               # stop the running one first
            deadline = time.time() + config.RESTART_GRACE_S
            while server_control.local_pid() and time.time() < deadline:
                time.sleep(config.RESTART_POLL_INTERVAL_S)
            leftover = server_control.local_pid()
            if leftover:                               # didn't exit → force it
                os.kill(leftover, signal.SIGKILL)
                time.sleep(config.RESTART_KILL_WAIT_S)
        p = server_control.spawn_server()
        return Result(f"restarted (pid {p})  logs: {config.LOG_PATH}" if p
                      else f"restart failed — check {config.LOG_PATH}")

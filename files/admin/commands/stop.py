"""admin/commands/stop.py — graceful VM stop."""

from admin import api_client, config
from admin.commands.base import Command, Result


class StopCommand(Command):
    names = ("stop",)
    needs_arg = True
    force = False   # KillCommand flips this to True

    def run(self, args: list) -> Result:
        r = api_client.exec_tool(config.TOOL_STOP, {"name": args[0], "force": self.force})
        return Result(r.get("message") or r.get("error", "done"))

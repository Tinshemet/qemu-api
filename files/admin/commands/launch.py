"""admin/commands/launch.py — start a VM."""

from admin import api_client, config
from admin.commands.base import Command, Result


class LaunchCommand(Command):
    names = ("start", "launch")
    needs_arg = True

    def run(self, args: list) -> Result:
        r = api_client.exec_tool(config.TOOL_LAUNCH, {"name": args[0]})
        return Result(r.get("message") or r.get("error", "done"))

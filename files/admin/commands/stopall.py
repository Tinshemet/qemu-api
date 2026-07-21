"""admin/commands/stopall.py — stop every running VM."""

from admin import api_client, config
from admin.commands.base import Command, Result


class StopAllCommand(Command):
    names = ("stopall",)

    def run(self, args: list) -> Result:
        vms = api_client.vm_list(api_client.exec_tool(config.TOOL_LIST))
        stopped = []
        for v in vms:
            if v.get("status") == "running":
                sr = api_client.exec_tool(config.TOOL_STOP, {"name": v["name"]})
                if not sr.get("error"):
                    stopped.append(v["name"])
        return Result(f"stopped: {', '.join(stopped)}" if stopped else "no running VMs")

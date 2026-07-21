"""admin/commands/status.py — orchestrator reachability + VM counts."""

from admin import api_client, config
from admin.commands.base import Command, Result


class StatusCommand(Command):
    names = ("status",)

    def run(self, args: list) -> Result:
        online  = api_client.server_online()
        vms     = api_client.vm_list(api_client.exec_tool(config.TOOL_LIST)) if online else []
        running = sum(1 for v in vms if v.get("status") == "running")
        status  = "online" if online else "unreachable"
        return Result(f"orchestrator={status}  vms={len(vms)}  running={running}")

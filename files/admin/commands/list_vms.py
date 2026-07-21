"""admin/commands/list_vms.py — print VM names in the status line."""

from admin import api_client, config
from admin.commands.base import Command, Result


class ListCommand(Command):
    names = ("list",)

    def run(self, args: list) -> Result:
        vms = api_client.vm_list(api_client.exec_tool(config.TOOL_LIST))
        return Result("  ".join(v.get("name", "") for v in vms) or "(none)")

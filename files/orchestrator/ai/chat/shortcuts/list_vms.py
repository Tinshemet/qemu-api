"""list — list VMs (optionally filtered by a label after a list/vms/ls prefix)."""

from typing import List

from .base import Shortcut
from . import context as ctx


class ListShortcut(Shortcut):
    config_key = "list"
    _PREFIXES  = ("list ", "vms ", "ls ")

    def matches(self, ui: str) -> bool:
        return ui in ctx.SHORTCUTS["list"] or any(ui.startswith(p) for p in self._PREFIXES)

    def run(self, ui: str, messages: List[dict], runtime_drift_count: int, verbose: bool) -> None:
        pfx   = next((p for p in self._PREFIXES if ui.startswith(p)), None)
        label = ui[len(pfx):].strip() if (pfx and ui not in ctx.SHORTCUTS["list"]) else ""
        ctx.execute_tool("list_vms", {"label": label} if label else {}, verbose)

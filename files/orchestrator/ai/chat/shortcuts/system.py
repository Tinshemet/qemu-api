"""system — show host virtualization capabilities."""

from typing import List

from .base import Shortcut
from . import context as ctx


class SystemShortcut(Shortcut):
    config_key = "system"

    def run(self, ui: str, messages: List[dict], runtime_drift_count: int, verbose: bool) -> None:
        ctx.execute_tool("check_system", {}, verbose)

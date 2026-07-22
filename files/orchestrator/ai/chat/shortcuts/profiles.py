"""profiles — list available hardware profiles."""

from typing import List

from .base import Shortcut
from . import context as ctx


class ProfilesShortcut(Shortcut):
    config_key = "profiles"

    def run(self, ui: str, messages: List[dict], runtime_drift_count: int, verbose: bool) -> None:
        ctx.execute_tool("list_profiles", {}, verbose)

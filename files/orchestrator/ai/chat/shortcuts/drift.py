"""drift — show the session drift report (orphaned turns, poisoning signal)."""

from typing import List

from .base import Shortcut
from . import context as ctx


class DriftShortcut(Shortcut):
    config_key = "drift"

    def run(self, ui: str, messages: List[dict], runtime_drift_count: int, verbose: bool) -> None:
        ctx.show_drift_report(messages, runtime_drift_count)

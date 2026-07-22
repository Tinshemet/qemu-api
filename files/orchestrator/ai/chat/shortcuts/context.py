"""
context.py — shared machinery for the REPL shortcut package.

Shortcuts reference these as module attributes (``ctx.execute_tool``, ``ctx.console``,
…) so a test patches them in one place. The phrase sets come from chat/config.json
(``shortcut_commands``), the same file cli.py reads.
"""

import json
import os

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from shared.display import console
from orchestrator.executor_client import execute_tool

from ..session import (
    clear_session, detect_drift, set_auto_clear, set_verbose, get_verbose,
    set_loop_max,
)

_CFG      = json.load(open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")))
SHORTCUTS = _CFG["shortcut_commands"]
CFG       = _CFG


def show_drift_report(messages: list, runtime_drift_count: int) -> None:
    """Render the session drift report (orphaned turns, consecutive no-tool count)."""
    user_count      = sum(1 for m in messages if m.get("role") == "user")
    assistant_count = sum(1 for m in messages if m.get("role") == "assistant")
    orphan_count    = user_count - assistant_count
    orphan_pct      = int(orphan_count / user_count * 100) if user_count else 0

    max_consec, consec = 0, 0
    for m in messages:
        if m.get("role") == "user":
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 0

    drift_result = detect_drift(messages)
    if drift_result:
        level, _ = drift_result
        if level == "critical":
            status_text = Text("✖ CRITICAL — model likely poisoned", style="bold red")
        else:
            status_text = Text("⚠ WARNING — early drift signal", style="bold yellow")
    else:
        status_text = Text("✓ HEALTHY", style="bold green")

    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("key",   style="dim")
    t.add_column("value", style="bold")

    t.add_row("Status",                  status_text)
    t.add_row("Session messages",        str(len(messages)))
    t.add_row("User turns",              str(user_count))
    t.add_row("Verified responses",      str(assistant_count))
    t.add_row("Orphaned turns",          f"{orphan_count}  ({orphan_pct}%)")
    t.add_row("Max consecutive orphans", str(max_consec))
    t.add_row("Runtime drift (turns)",   str(runtime_drift_count))

    if drift_result:
        _, msg = drift_result
        t.add_row("Advice", Text(msg, style="yellow" if drift_result[0] == "warn" else "red"))

    console.print(Panel(t, title="Session Drift Report", border_style="cyan"))

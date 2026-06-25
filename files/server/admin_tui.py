"""
admin_tui.py — Real-time server admin CLI (TUI)

Shows a live dashboard with:
  - VM table (name, status, CPU cores, RAM)
  - Recent event feed (tool calls, outcomes, durations)
  - Server stats (uptime, event count)

Usage:
  qemu-api-admin
"""

import sys
import os

# Running this file directly adds files/server/ to sys.path, which makes
# files/server/http/ shadow the stdlib http module. Remove it.
_here = os.path.dirname(os.path.abspath(__file__))
if _here in sys.path:
    sys.path.remove(_here)

import time

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

_REFRESH = 2  # seconds between updates


def _vm_table(vms: list) -> Table:
    t = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1))
    t.add_column("VM",     style="bold white", no_wrap=True)
    t.add_column("Status", no_wrap=True)
    t.add_column("CPU",    justify="right")
    t.add_column("RAM",    justify="right")
    t.add_column("OS",     style="dim")
    for v in vms:
        status = v.get("status", "?")
        color  = "green" if status == "running" else "dim"
        dot    = "●" if status == "running" else "○"
        t.add_row(
            v.get("name", "?"),
            Text(f"{dot} {status}", style=color),
            str(v.get("cpu_cores", "?")),
            f"{v.get('memory_mb', 0) // 1024}GB",
            v.get("os", ""),
        )
    return t


def _event_table(events: list) -> Table:
    t = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 1))
    t.add_column("Time",     style="dim",        no_wrap=True)
    t.add_column("Tool",     style="bold white",  no_wrap=True)
    t.add_column("Target",   style="cyan",        no_wrap=True)
    t.add_column("Outcome",  no_wrap=True)
    t.add_column("ms",       justify="right", style="dim")
    for e in reversed(events[-20:]):
        ts      = e.get("ts", "")[:19].replace("T", " ")
        tool    = e.get("tool", "")
        args    = e.get("args", {})
        outcome = e.get("outcome", "")
        ms      = str(int(e.get("duration_ms", 0)))
        target  = args.get("name") or args.get("profile") or args.get("network") or ""
        color   = "green" if outcome == "ok" else ("yellow" if outcome == "already_running" else "red")
        t.add_row(ts, tool, target, Text(outcome, style=color), ms)
    return t


def _build_layout(vms: list, events: list, uptime_s: float) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=1),
    )
    layout["body"].split_row(
        Layout(name="vms",    ratio=2),
        Layout(name="events", ratio=3),
    )

    uptime = f"{int(uptime_s // 3600):02d}:{int((uptime_s % 3600) // 60):02d}:{int(uptime_s % 60):02d}"
    layout["header"].update(Panel(
        f"[bold cyan]qemu-api server admin[/bold cyan]   uptime [dim]{uptime}[/dim]   "
        f"events [dim]{len(events)}[/dim]   vms [dim]{len(vms)}[/dim]",
        style="bold", border_style="cyan",
    ))
    layout["vms"].update(Panel(_vm_table(vms),    title="[bold]VMs[/bold]",    border_style="dim"))
    layout["events"].update(Panel(_event_table(events), title="[bold]Recent Events[/bold]", border_style="dim"))
    layout["footer"].update(Text("  [q] quit   refreshes every 2s", style="dim", justify="left"))
    return layout


def _run_local():
    from server.event_log import read_events
    from shared.executioner.tool_executor import manager

    start = time.monotonic()
    console = Console()

    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            try:
                raw = manager.list_vms()
                vms = raw if isinstance(raw, list) else raw.get("vms", [])
            except Exception:
                vms = []
            events  = read_events(limit=200)
            uptime  = time.monotonic() - start
            live.update(_build_layout(vms, events, uptime))
            time.sleep(_REFRESH)


def main():
    _run_local()


if __name__ == "__main__":
    main()

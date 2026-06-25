"""
admin_tui.py — Real-time server admin CLI (TUI)

Shows a live dashboard with:
  - VM table (name, status, CPU cores, RAM)
  - Recent event feed (tool calls, outcomes, durations)
  - Command input line at the bottom

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

import select
import termios
import threading
import time
import tty

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

_REFRESH = 1  # seconds between updates


# ── keyboard input ────────────────────────────────────────────────────────────

_cmd_buf   = ""
_cmd_msg   = ""   # feedback line after executing a command
_quit      = threading.Event()


def _read_keys():
    """Background thread: read keypresses without blocking the Live loop."""
    global _cmd_buf, _cmd_msg
    fd   = sys.stdin.fileno()
    old  = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while not _quit.is_set():
            if select.select([sys.stdin], [], [], 0.1)[0]:
                ch = sys.stdin.read(1)
                if ch in ("\x03", "\x1b"):   # Ctrl-C or Escape
                    _quit.set()
                elif ch == "q" and not _cmd_buf:
                    _quit.set()
                elif ch in ("\r", "\n"):
                    _dispatch(_cmd_buf.strip())
                    _cmd_buf = ""
                elif ch == "\x7f":           # backspace
                    _cmd_buf = _cmd_buf[:-1]
                else:
                    _cmd_buf += ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _dispatch(cmd: str):
    """Execute a typed command."""
    global _cmd_msg
    if not cmd:
        return
    from shared.executioner.tool_executor import manager
    parts = cmd.split()
    verb  = parts[0].lower()
    name  = parts[1] if len(parts) > 1 else ""
    try:
        if verb in ("stop", "kill") and name:
            r = manager.stop_vm(name, force=(verb == "kill"))
            _cmd_msg = r.get("message") or r.get("error", "")
        elif verb in ("start", "launch") and name:
            r = manager.launch_vm(name)
            _cmd_msg = r.get("message") or r.get("error", "")
        elif verb == "list":
            raw = manager.list_vms()
            vms = raw if isinstance(raw, list) else raw.get("vms", [])
            _cmd_msg = "  ".join(v.get("name", "") for v in vms)
        else:
            _cmd_msg = f"unknown command: {cmd}  (stop/launch/list <vm>)"
    except Exception as e:
        _cmd_msg = str(e)


# ── rendering ─────────────────────────────────────────────────────────────────

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
    t.add_column("Time",    style="dim",       no_wrap=True)
    t.add_column("Tool",    style="bold white", no_wrap=True)
    t.add_column("Target",  style="cyan",       no_wrap=True)
    t.add_column("Outcome", no_wrap=True)
    t.add_column("ms",      justify="right", style="dim")
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
    global _cmd_buf, _cmd_msg
    layout = Layout()
    layout.split_column(
        Layout(name="header",  size=3),
        Layout(name="body"),
        Layout(name="cmdline", size=3),
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
    layout["vms"].update(Panel(_vm_table(vms),         title="[bold]VMs[/bold]",           border_style="dim"))
    layout["events"].update(Panel(_event_table(events), title="[bold]Recent Events[/bold]", border_style="dim"))

    prompt = f"[bold cyan]>[/bold cyan] {_cmd_buf}[blink]▌[/blink]"
    feedback = f"\n[dim]{_cmd_msg}[/dim]" if _cmd_msg else ""
    layout["cmdline"].update(Panel(
        Text.from_markup(prompt + feedback),
        title="[dim]command  (stop/launch/list <vm>)   q = quit[/dim]",
        border_style="dim",
    ))
    return layout


# ── main ──────────────────────────────────────────────────────────────────────

def _run_local():
    from server.event_log import read_events
    from shared.executioner.tool_executor import manager

    start  = time.monotonic()
    console = Console()

    key_thread = threading.Thread(target=_read_keys, daemon=True)
    key_thread.start()

    with Live(console=console, refresh_per_second=2, screen=True) as live:
        while not _quit.is_set():
            try:
                raw = manager.list_vms()
                vms = raw if isinstance(raw, list) else raw.get("vms", [])
            except Exception:
                vms = []
            events = read_events(limit=200)
            uptime = time.monotonic() - start
            live.update(_build_layout(vms, events, uptime))
            time.sleep(_REFRESH)


def main():
    _run_local()


if __name__ == "__main__":
    main()

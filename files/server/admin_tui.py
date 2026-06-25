"""
admin_tui.py — Real-time server admin CLI (TUI)

Fullscreen dashboard: VM table, event feed, command prompt.
Run on the server: qemu-api-admin
"""

import sys
import os

# Prevent files/server/http/ from shadowing stdlib http
_here = os.path.dirname(os.path.abspath(__file__))
if _here in sys.path:
    sys.path.remove(_here)

import select
import termios
import threading
import time
import tty

from rich.console import Console
from rich.live   import Live
from rich.panel  import Panel
from rich.table  import Table
from rich.text   import Text
from rich        import box as rbox

_REFRESH = 1

# ── keyboard ──────────────────────────────────────────────────────────────────

_cmd_buf = ""
_cmd_msg = ""
_quit    = threading.Event()


def _read_keys():
    global _cmd_buf, _cmd_msg
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while not _quit.is_set():
            if select.select([sys.stdin], [], [], 0.1)[0]:
                ch = sys.stdin.read(1)
                if ch in ("\x03", "\x1b"):
                    _quit.set()
                elif ch == "q" and not _cmd_buf:
                    _quit.set()
                elif ch in ("\r", "\n"):
                    _dispatch(_cmd_buf.strip())
                    _cmd_buf = ""
                elif ch == "\x7f":
                    _cmd_buf = _cmd_buf[:-1]
                else:
                    _cmd_buf += ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _dispatch(cmd: str):
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
            raw  = manager.list_vms()
            vms  = raw if isinstance(raw, list) else raw.get("vms", [])
            _cmd_msg = "  ".join(v.get("name", "") for v in vms)
        else:
            _cmd_msg = f"unknown: {cmd}  (stop/launch/list <vm>)"
    except Exception as e:
        _cmd_msg = str(e)


# ── rendering ─────────────────────────────────────────────────────────────────

def _render(vms: list, events: list, uptime_s: float, height: int) -> Table:
    uptime = f"{int(uptime_s//3600):02d}:{int((uptime_s%3600)//60):02d}:{int(uptime_s%60):02d}"

    # Budget: 3 header + 4 cmdline + 3 borders + 2 table headers = 12 fixed rows
    body = max(4, height - 12)
    max_vms = min(len(vms), max(1, body // 3))
    max_ev  = max(1, body - max_vms)

    # ── VM table ──────────────────────────────────────────────────────────────
    vm_t = Table(show_header=True, header_style="bold cyan",
                 box=rbox.SIMPLE, padding=(0, 1), expand=True)
    vm_t.add_column("VM",     style="bold white", no_wrap=True)
    vm_t.add_column("Status", no_wrap=True)
    vm_t.add_column("CPU",  justify="right")
    vm_t.add_column("RAM",  justify="right")
    vm_t.add_column("OS",   style="dim")
    for v in vms[:max_vms]:
        status = v.get("status", "?")
        dot    = "●" if status == "running" else "○"
        color  = "green" if status == "running" else "dim"
        vm_t.add_row(
            v.get("name", ""),
            Text(f"{dot} {status}", style=color),
            str(v.get("cpu_cores", "")),
            f"{v.get('memory_mb',0)//1024}GB",
            v.get("os", ""),
        )

    # ── event table ───────────────────────────────────────────────────────────
    ev_t = Table(show_header=True, header_style="bold cyan",
                 box=rbox.SIMPLE, padding=(0, 1), expand=True)
    ev_t.add_column("Time",   style="dim",        no_wrap=True)
    ev_t.add_column("Tool",   style="bold white",  no_wrap=True)
    ev_t.add_column("Target", style="cyan",        no_wrap=True)
    ev_t.add_column("Result", no_wrap=True)
    ev_t.add_column("ms",     justify="right", style="dim")
    for e in events[-max_ev:]:
        ts      = e.get("ts", "")[:19].replace("T", " ")
        outcome = e.get("outcome", "")
        color   = "green" if outcome == "ok" else ("yellow" if outcome == "already_running" else "red")
        args    = e.get("args", {})
        target  = args.get("name") or args.get("profile") or ""
        ev_t.add_row(ts, e.get("tool",""), target, Text(outcome, style=color),
                     str(int(e.get("duration_ms", 0))))

    # ── compose ───────────────────────────────────────────────────────────────
    prompt   = f"[bold cyan]>[/bold cyan] {_cmd_buf}[blink]▌[/blink]"
    feedback = f"  [dim]{_cmd_msg}[/dim]" if _cmd_msg else ""

    outer = Table.grid(expand=True)
    outer.add_row(Panel(
        f"[bold cyan]qemu-api admin[/bold cyan]   uptime [dim]{uptime}[/dim]   "
        f"vms [dim]{len(vms)}[/dim]   events [dim]{len(events)}[/dim]",
        border_style="cyan",
    ))
    outer.add_row(Panel(vm_t,  title="[bold]VMs[/bold]",           border_style="dim"))
    outer.add_row(Panel(ev_t,  title="[bold]Recent Events[/bold]", border_style="dim"))
    outer.add_row(Panel(
        Text.from_markup(prompt + feedback),
        title="[dim]stop/launch/list <vm>   q=quit[/dim]",
        border_style="dim",
    ))
    return outer


# ── main ──────────────────────────────────────────────────────────────────────

def _run_local():
    from server.event_log import read_events
    from shared.executioner.tool_executor import manager

    # Request terminal resize to minimum comfortable size
    sys.stdout.write("\033[8;52;200t")
    sys.stdout.flush()
    time.sleep(0.1)  # give the terminal time to resize before measuring

    start   = time.monotonic()
    console = Console()

    if sys.stdin.isatty():
        threading.Thread(target=_read_keys, daemon=True).start()

    try:
        with Live(console=console, refresh_per_second=4, screen=False,
                  vertical_overflow="crop") as live:
            while not _quit.is_set():
                try:
                    raw = manager.list_vms()
                    vms = raw if isinstance(raw, list) else raw.get("vms", [])
                except Exception:
                    vms = []
                events = read_events(limit=200)
                uptime = time.monotonic() - start
                live.update(_render(vms, events, uptime, console.size.height))
                time.sleep(_REFRESH)
    except Exception as e:
        print(f"\n[admin_tui error] {e}", file=sys.stderr)
        import traceback; traceback.print_exc()


def main():
    _run_local()


if __name__ == "__main__":
    main()

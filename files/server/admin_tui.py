"""
admin_tui.py — Real-time server admin TUI

Fullscreen dashboard: VM table, event feed, command prompt.
Run on the server: qemu-api-admin
"""

import sys
import os

# Prevent files/server/http/ from shadowing stdlib http
_here = os.path.dirname(os.path.abspath(__file__))
if _here in sys.path:
    sys.path.remove(_here)

import curses
import threading
import time


_REFRESH = 1.0


# ── state shared between keyboard thread and draw loop ────────────────────────

_cmd_buf = ""
_cmd_msg = ""
_quit    = threading.Event()
_lock    = threading.Lock()


# ── curses colour pairs ───────────────────────────────────────────────────────

C_NORMAL  = 0
C_HEADER  = 1   # white on blue
C_CYAN    = 2
C_GREEN   = 3
C_RED     = 4
C_DIM     = 5
C_YELLOW  = 6


def _init_colours():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_HEADER, curses.COLOR_WHITE,  curses.COLOR_BLUE)
    curses.init_pair(C_CYAN,   curses.COLOR_CYAN,   -1)
    curses.init_pair(C_GREEN,  curses.COLOR_GREEN,  -1)
    curses.init_pair(C_RED,    curses.COLOR_RED,    -1)
    curses.init_pair(C_DIM,    curses.COLOR_BLACK+8 if hasattr(curses, 'COLOR_BLACK') else 0, -1)
    curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, -1)


def _cp(n):
    return curses.color_pair(n)


# ── draw ──────────────────────────────────────────────────────────────────────

def _hline(stdscr, row, w, label=""):
    if row < 0:
        return
    if label:
        left  = max(0, (w - len(label) - 2) // 2)
        right = max(0, w - left - len(label) - 2)
        try:
            stdscr.addstr(row, 0,    "─" * left,       _cp(C_DIM))
            stdscr.addstr(row, left, f" {label} ",      _cp(C_CYAN) | curses.A_BOLD)
            stdscr.addstr(row, left + len(label) + 2, "─" * right, _cp(C_DIM))
        except curses.error:
            pass
    else:
        try:
            stdscr.addstr(row, 0, "─" * (w - 1), _cp(C_DIM))
        except curses.error:
            pass


def _draw(stdscr, vms: list, events: list, uptime_s: float):
    global _cmd_buf, _cmd_msg
    h, w = stdscr.getmaxyx()

    stdscr.erase()

    # ── header bar ────────────────────────────────────────────────────────────
    uptime  = f"{int(uptime_s//3600):02d}:{int((uptime_s%3600)//60):02d}:{int(uptime_s%60):02d}"
    hdr     = f"  qemu-api admin   uptime {uptime}   vms {len(vms)}   events {len(events)}  "
    try:
        stdscr.addstr(0, 0, hdr.ljust(w - 1), _cp(C_HEADER) | curses.A_BOLD)
    except curses.error:
        pass

    # ── VM section ────────────────────────────────────────────────────────────
    # fixed rows: hdr=1, vm_hline=1, vm_col_hdr=1, vm_sep=1 → 4 rows overhead before vm rows
    # bottom: ev_hline=1, ev_col_hdr=1, ev_sep=1, cmd_hline=1, cmd_row=1, hint=1 → 6 rows
    body_h   = h - 4 - 6
    max_vms  = min(len(vms), max(1, body_h // 3))
    max_evs  = max(1, body_h - max_vms)

    row = 1
    _hline(stdscr, row, w, "VMs"); row += 1

    # column headers
    cols_vm = f"  {'VM':<24} {'STATUS':<16} {'CPU':>4}  {'RAM':>5}  {'OS'}"
    try:
        stdscr.addstr(row, 0, cols_vm[:w-1], _cp(C_CYAN) | curses.A_BOLD)
    except curses.error:
        pass
    row += 1
    try:
        stdscr.addstr(row, 0, "  " + "─" * (w - 3), _cp(C_DIM))
    except curses.error:
        pass
    row += 1

    for v in vms[:max_vms]:
        if row >= h - 7:
            break
        status = v.get("status", "?")
        dot    = "● " if status == "running" else "○ "
        color  = _cp(C_GREEN) if status == "running" else _cp(C_DIM)
        name   = v.get("name", "")[:23]
        os_s   = v.get("os", "")[:20]
        ram    = f"{v.get('memory_mb',0)//1024}GB"
        cpu    = str(v.get("cpu_cores", ""))
        try:
            stdscr.addstr(row, 2, f"{name:<24} ", _cp(C_NORMAL))
            stdscr.addstr(row, 27, f"{dot}{status:<14}", color)
            stdscr.addstr(row, 43, f"{cpu:>4}  {ram:>5}  {os_s}", _cp(C_NORMAL))
        except curses.error:
            pass
        row += 1

    # ── Events section ────────────────────────────────────────────────────────
    _hline(stdscr, row, w, "Recent Events"); row += 1

    cols_ev = f"  {'TIME':<20} {'TOOL':<24} {'TARGET':<15} {'RESULT':<28} {'MS':>6}"
    try:
        stdscr.addstr(row, 0, cols_ev[:w-1], _cp(C_CYAN) | curses.A_BOLD)
    except curses.error:
        pass
    row += 1
    try:
        stdscr.addstr(row, 0, "  " + "─" * (w - 3), _cp(C_DIM))
    except curses.error:
        pass
    row += 1

    for e in events[-max_evs:]:
        if row >= h - 3:
            break
        ts      = e.get("ts", "")[:19].replace("T", " ")
        outcome = e.get("outcome", "")
        args    = e.get("args", {})
        target  = (args.get("name") or args.get("profile") or "")[:14]
        ms      = str(int(e.get("duration_ms", 0)))
        tool    = e.get("tool", "")[:23]

        if outcome == "ok":
            res_color = _cp(C_GREEN)
        elif outcome in ("already_running",):
            res_color = _cp(C_YELLOW)
        else:
            res_color = _cp(C_RED)

        try:
            stdscr.addstr(row, 2, f"{ts:<20} {tool:<24} {target:<15} ", _cp(C_NORMAL))
            stdscr.addstr(row, 2 + 20 + 1 + 24 + 1 + 15 + 1, f"{outcome:<28}", res_color)
            stdscr.addstr(row, 2 + 20 + 1 + 24 + 1 + 15 + 1 + 29, f"{ms:>6}", _cp(C_DIM))
        except curses.error:
            pass
        row += 1

    # ── command line ──────────────────────────────────────────────────────────
    _hline(stdscr, h - 3, w)
    with _lock:
        prompt = f" > {_cmd_buf}"
        msg    = f"   {_cmd_msg}" if _cmd_msg else ""
    try:
        stdscr.addstr(h - 2, 0, prompt[:w-1], _cp(C_CYAN) | curses.A_BOLD)
        if msg:
            p_end = min(len(prompt), w - 2)
            stdscr.addstr(h - 2, p_end, msg[:w - p_end - 1], _cp(C_DIM))
    except curses.error:
        pass
    try:
        stdscr.addstr(h - 1, 0,
                      "  stop/launch/list <vm>   q=quit"[:w-1], _cp(C_DIM))
    except curses.error:
        pass

    stdscr.refresh()


# ── keyboard ──────────────────────────────────────────────────────────────────

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
            _cmd_msg = r.get("message") or r.get("error", "done")
        elif verb in ("start", "launch") and name:
            r = manager.launch_vm(name)
            _cmd_msg = r.get("message") or r.get("error", "done")
        elif verb == "list":
            raw  = manager.list_vms()
            vms  = raw if isinstance(raw, list) else raw.get("vms", [])
            _cmd_msg = "  ".join(v.get("name", "") for v in vms)
        else:
            _cmd_msg = f"unknown: {cmd}"
    except Exception as e:
        _cmd_msg = str(e)[:80]


def _handle_input(stdscr):
    global _cmd_buf, _cmd_msg
    while not _quit.is_set():
        try:
            ch = stdscr.get_wch()
        except curses.error:
            time.sleep(0.05)
            continue

        with _lock:
            if ch in (3, "\x03", 27, "\x1b"):      # Ctrl-C / Esc
                _quit.set()
            elif ch == "q" and not _cmd_buf:
                _quit.set()
            elif ch in ("\n", "\r", curses.KEY_ENTER):
                cmd = _cmd_buf.strip()
                _cmd_buf = ""
                threading.Thread(target=_dispatch, args=(cmd,), daemon=True).start()
            elif ch in (curses.KEY_BACKSPACE, "\x7f", 8):
                _cmd_buf = _cmd_buf[:-1]
                _cmd_msg = ""
            elif isinstance(ch, str) and ch.isprintable():
                _cmd_buf += ch
                _cmd_msg = ""


# ── main ──────────────────────────────────────────────────────────────────────

def _run(stdscr):
    from server.event_log import read_events
    from shared.executioner.tool_executor import manager

    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.timeout(200)          # getch blocks for 200ms max → ~5fps keyboard polling
    _init_colours()

    # Resize terminal window to comfortable minimum
    sys.stdout.write("\033[8;52;200t")
    sys.stdout.flush()
    time.sleep(0.12)
    curses.resizeterm(*stdscr.getmaxyx())

    start = time.monotonic()
    threading.Thread(target=_handle_input, args=(stdscr,), daemon=True).start()

    last_fetch = 0.0
    vms, events = [], []

    while not _quit.is_set():
        now = time.monotonic()
        if now - last_fetch >= _REFRESH:
            try:
                raw  = manager.list_vms()
                vms  = raw if isinstance(raw, list) else raw.get("vms", [])
            except Exception:
                vms  = []
            events     = read_events(limit=200)
            last_fetch = now

        _draw(stdscr, vms, events, now - start)
        time.sleep(0.05)


def main():
    try:
        curses.wrapper(_run)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

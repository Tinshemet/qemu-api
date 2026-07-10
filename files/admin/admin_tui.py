"""
admin_tui.py — Real-time admin TUI for qemu-api

Fullscreen dashboard: VM table, event feed, command prompt.
Connects to the orchestrator over HTTP — run from any machine that can reach it.
Configure in admin/connection_config.json.
"""

import sys
import os
import curses
import subprocess
import threading
import time
import json as _json

# ── config ────────────────────────────────────────────────────────────────────

_here = os.path.dirname(os.path.abspath(__file__))


def _load_json(path: str) -> dict:
    try:
        return _json.load(open(path))
    except Exception:
        return {}


_CONN_CFG  = _load_json(os.path.join(_here, "connection_config.json"))
_ADMIN_CFG = _load_json(os.path.join(_here, "admin_config.json"))

_ORCH_URL     = os.environ.get("SERVER_URL",  _CONN_CFG.get("orchestrator_url", "http://localhost:8080"))
_REFRESH      = _ADMIN_CFG.get("refresh_rate_s",       1.0)
_DEFAULT_PORT = _ADMIN_CFG.get("default_port",         8080)
_LOG_PATH     = _ADMIN_CFG.get("log_path",             "/tmp/qemu-api-server.log")
_EVENTS_LIMIT = _ADMIN_CFG.get("events_display_limit", 200)


def _token() -> str:
    t = os.environ.get("API_TOKEN") or _CONN_CFG.get("token", "")
    if t:
        return t
    try:
        with open(os.path.expanduser("~/.qemu-api.token")) as f:
            return f.read().strip()
    except Exception:
        return ""


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _post(path: str, body: dict) -> dict:
    import requests
    try:
        r = requests.post(
            f"{_ORCH_URL}{path}",
            json=body,
            headers={"Authorization": f"Bearer {_token()}"},
            timeout=5,
        )
        return r.json() if r.ok else {"success": False, "error": r.text[:120]}
    except Exception as e:
        return {"success": False, "error": str(e)[:80]}


def _get(path: str, params: dict = None) -> dict:
    import requests
    try:
        r = requests.get(
            f"{_ORCH_URL}{path}",
            params=params or {},
            headers={"Authorization": f"Bearer {_token()}"},
            timeout=5,
        )
        return r.json() if r.ok else {}
    except Exception:
        return {}


def _exec(tool_name: str, args: dict = None) -> dict:
    return _post("/execute", {"tool_name": tool_name, "args": args or {}})


def _get_events(limit: int = 200) -> list:
    return _get("/events", {"limit": limit}).get("events", [])


# ── health check (cached 2s) ──────────────────────────────────────────────────

_health_cache: tuple = (0.0, False)


def _server_online() -> bool:
    global _health_cache
    now = time.monotonic()
    if now - _health_cache[0] < 2.0:
        return _health_cache[1]
    import requests
    try:
        result = requests.get(f"{_ORCH_URL}/health", timeout=2).ok
    except Exception:
        result = False
    _health_cache = (now, result)
    return result


# ── local process helpers (only useful when admin runs on orchestrator machine) ─

def _local_pid() -> int | None:
    try:
        out = subprocess.check_output(["pgrep", "-f", "api_server"], text=True).strip()
        pids = [int(p) for p in out.splitlines() if p.strip()]
        return pids[0] if pids else None
    except Exception:
        return None


# ── curses state ──────────────────────────────────────────────────────────────

_cmd_buf   = ""
_cmd_msg   = ""
_quit      = threading.Event()
_lock      = threading.Lock()
_help_mode = False

C_NORMAL = 0
C_HEADER = 1
C_CYAN   = 2
C_GREEN  = 3
C_RED    = 4
C_DIM    = 5
C_YELLOW = 6

_CUSTOM_COLOR_SLOT = 16


def _hex_to_curses(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (667, 667, 667)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (r * 1000 // 255, g * 1000 // 255, b * 1000 // 255)


def _init_colours(color_hex: str = "#aaaaaa"):
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_HEADER, curses.COLOR_WHITE,  curses.COLOR_BLUE)
    curses.init_pair(C_CYAN,   curses.COLOR_CYAN,   -1)
    curses.init_pair(C_GREEN,  curses.COLOR_GREEN,  -1)
    curses.init_pair(C_RED,    curses.COLOR_RED,    -1)
    curses.init_pair(C_YELLOW, curses.COLOR_YELLOW, -1)
    if curses.can_change_color():
        r, g, b = _hex_to_curses(color_hex)
        curses.init_color(_CUSTOM_COLOR_SLOT, r, g, b)
        curses.init_pair(C_DIM, _CUSTOM_COLOR_SLOT, -1)
    else:
        curses.init_pair(C_DIM, 8, -1)


def _cp(n):
    return curses.color_pair(n)


# ── draw ──────────────────────────────────────────────────────────────────────

def _hline(stdscr, row, w, label=""):
    if row < 0:
        return
    try:
        if label:
            left  = max(0, (w - len(label) - 2) // 2)
            right = max(0, w - left - len(label) - 2)
            stdscr.addstr(row, 0,    "─" * left,                   _cp(C_DIM))
            stdscr.addstr(row, left, f" {label} ",                  _cp(C_CYAN) | curses.A_BOLD)
            stdscr.addstr(row, left + len(label) + 2, "─" * right, _cp(C_DIM))
        else:
            stdscr.addstr(row, 0, "─" * (w - 1), _cp(C_DIM))
    except curses.error:
        pass  # addstr past the screen edge — skip the section rule


def _draw(stdscr, vms: list, events: list, uptime_s: float):
    global _cmd_buf, _cmd_msg
    h, w = stdscr.getmaxyx()
    stdscr.erase()

    if _help_mode:
        _draw_help(stdscr, h, w)
        stdscr.refresh()
        return

    uptime  = f"{int(uptime_s//3600):02d}:{int((uptime_s%3600)//60):02d}:{int(uptime_s%60):02d}"
    online  = _server_online()
    srv_str = f"online @ {_ORCH_URL}" if online else f"unreachable @ {_ORCH_URL}"
    hdr     = f"  qemu-api admin   uptime {uptime}   vms {len(vms)}   events {len(events)}   {srv_str}  "
    try:
        stdscr.addstr(0, 0, hdr.ljust(w - 1), _cp(C_HEADER) | curses.A_BOLD)
    except curses.error:
        pass  # addstr past the screen edge — skip the header

    body_h  = h - 4 - 6
    max_vms = min(len(vms), max(1, body_h // 3))
    max_evs = max(1, body_h - max_vms)

    row = 1
    _hline(stdscr, row, w, "VMs"); row += 1

    cols_vm = f"  {'VM':<24} {'STATUS':<16} {'CPU':>4}  {'RAM':>5}  {'OS'}"
    try:
        stdscr.addstr(row, 0, cols_vm[:w-1], _cp(C_CYAN) | curses.A_BOLD)
    except curses.error:
        pass  # addstr past the screen edge — skip the VM column header
    row += 1
    try:
        stdscr.addstr(row, 0, "  " + "─" * (w - 3), _cp(C_DIM))
    except curses.error:
        pass  # addstr past the screen edge — skip the rule
    row += 1

    for v in vms[:max_vms]:
        if row >= h - 7:
            break
        status = v.get("status", "?")
        dot    = "● " if status == "running" else "○ "
        color  = _cp(C_GREEN) if status == "running" else _cp(C_DIM)
        name   = v.get("name", "")[:23]
        os_s   = v.get("os", "")[:20]
        ram    = f"{v.get('memory_mb', 0) // 1024}GB"
        cpu    = str(v.get("cpu_cores", ""))
        try:
            stdscr.addstr(row, 2,  f"{name:<24} ",              _cp(C_NORMAL))
            stdscr.addstr(row, 27, f"{dot}{status:<14}",        color)
            stdscr.addstr(row, 43, f"{cpu:>4}  {ram:>5}  {os_s}", _cp(C_NORMAL))
        except curses.error:
            pass  # addstr past the screen edge — skip this VM row
        row += 1

    _hline(stdscr, row, w, "Recent Events"); row += 1

    cols_ev = f"  {'TIME':<20} {'TOOL':<24} {'TARGET':<15} {'RESULT':<28} {'MS':>6}"
    try:
        stdscr.addstr(row, 0, cols_ev[:w-1], _cp(C_CYAN) | curses.A_BOLD)
    except curses.error:
        pass  # addstr past the screen edge — skip the events column header
    row += 1
    try:
        stdscr.addstr(row, 0, "  " + "─" * (w - 3), _cp(C_DIM))
    except curses.error:
        pass  # addstr past the screen edge — skip the rule
    row += 1

    for e in events[-max_evs:]:
        if row >= h - 3:
            break
        ts        = e.get("ts", "")[:19].replace("T", " ")
        outcome   = e.get("outcome", "")
        args      = e.get("args", {})
        target    = (args.get("name") or args.get("profile") or "")[:14]
        ms        = str(int(e.get("duration_ms", 0)))
        tool      = e.get("tool", "")[:23]
        outcome_s = outcome[:27]
        res_color = (_cp(C_GREEN)  if outcome == "ok"
                     else _cp(C_YELLOW) if outcome == "already_running"
                     else _cp(C_RED))
        try:
            stdscr.addstr(row, 2,  f"{ts:<20} {tool:<24} {target:<15} ", _cp(C_NORMAL))
            stdscr.addstr(row, 64, f"{outcome_s:<28}",                    res_color)
            stdscr.addstr(row, 93, f"{ms:>6}",                            _cp(C_DIM))
        except curses.error:
            pass  # addstr past the screen edge — skip this event row
        row += 1

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
        pass  # addstr past the screen edge — skip the prompt line
    try:
        _hint = "  stop/kill/launch/list/stopall <vm>   start-server  shutdown  status  help  q=quit"
        stdscr.addstr(h - 1, 0, _hint[:w-1], _cp(C_DIM))
    except curses.error:
        pass  # addstr past the screen edge — skip the hint line

    stdscr.refresh()


def _draw_help(stdscr, h, w):
    SECTIONS = [
        ("VM Commands", [
            ("launch <vm>",  "Start a VM"),
            ("stop <vm>",    "Graceful stop (SIGTERM)"),
            ("kill <vm>",    "Force-kill (SIGKILL)"),
            ("stopall",      "Stop all running VMs"),
            ("list",         "Print VM names in status line"),
        ]),
        ("Server Commands (local only)", [
            ("start-server", "Start the orchestrator on this machine"),
            ("shutdown",     "SIGTERM the orchestrator on this machine"),
            ("kill-server",  "SIGKILL the orchestrator on this machine"),
            ("status",       "Show orchestrator reachability + VM counts"),
        ]),
        ("Navigation", [
            ("help",         "Show this overlay  (any key to close)"),
            ("q / Esc",      "Quit the admin TUI"),
        ]),
    ]

    total_rows = sum(1 + len(cmds) for _, cmds in SECTIONS) + len(SECTIONS) + 3
    box_h = min(total_rows + 2, h - 4)
    box_w = min(64, w - 4)
    y     = max(0, (h - box_h) // 2)
    x     = max(0, (w - box_w) // 2)

    try:
        win = curses.newwin(box_h, box_w, y, x)
        win.border()
        win.addstr(0, 2, " Help ", _cp(C_CYAN) | curses.A_BOLD)
        row = 1
        for section, cmds in SECTIONS:
            if row >= box_h - 2:
                break
            win.addstr(row, 2, section, _cp(C_CYAN) | curses.A_BOLD)
            row += 1
            for cmd, desc in cmds:
                if row >= box_h - 2:
                    break
                win.addstr(row, 4, f"{cmd:<20}", curses.A_BOLD)
                win.addstr(row, 25, desc[:box_w - 27], _cp(C_DIM))
                row += 1
            row += 1
        if row < box_h - 1:
            win.addstr(box_h - 1, 2, "any key to close", _cp(C_DIM))
        win.refresh()
    except curses.error:
        pass  # addstr past the popup edge — stop drawing the box


# ── command dispatch ──────────────────────────────────────────────────────────

def _dispatch(cmd: str):
    if not cmd:
        return
    import signal as _signal
    parts = cmd.split()
    verb  = parts[0].lower()
    name  = parts[1] if len(parts) > 1 else ""
    new_msg       = ""
    new_help_mode = False

    try:
        if verb in ("stop", "kill") and name:
            r = _exec("stop_vm", {"name": name, "force": (verb == "kill")})
            new_msg = r.get("message") or r.get("error", "done")

        elif verb in ("start", "launch") and name:
            r = _exec("launch_vm", {"name": name})
            new_msg = r.get("message") or r.get("error", "done")

        elif verb == "list":
            r   = _exec("list_vms")
            vms = r.get("vms", [])
            new_msg = "  ".join(v.get("name", "") for v in vms) or "(none)"

        elif verb == "stopall":
            r   = _exec("list_vms")
            vms = r.get("vms", [])
            stopped = []
            for v in vms:
                if v.get("status") == "running":
                    sr = _exec("stop_vm", {"name": v["name"]})
                    if not sr.get("error"):
                        stopped.append(v["name"])
            new_msg = f"stopped: {', '.join(stopped)}" if stopped else "no running VMs"

        elif verb == "start-server":
            pid = _local_pid()
            if pid:
                new_msg = f"already running locally (pid {pid})"
            else:
                files_dir = os.path.dirname(_here)
                env = os.environ.copy()
                env["PYTHONPATH"] = files_dir
                try:
                    with open(os.path.expanduser("~/.qemu-api.token")) as f:
                        env["API_TOKEN"] = f.read().strip()
                except Exception:
                    pass  # no token file — run the admin server without an API token
                with open(_LOG_PATH, "w") as log_fh:
                    proc = subprocess.Popen(
                        [sys.executable, "-m", "uvicorn",
                         "orchestrator.http.api_server:app",
                         "--host", "0.0.0.0", "--port", str(_DEFAULT_PORT),
                         "--log-level", "warning"],
                        cwd=files_dir, env=env,
                        start_new_session=True,
                        stdout=log_fh,
                        stderr=subprocess.STDOUT,
                    )
                time.sleep(0.5)
                if _local_pid():
                    new_msg = f"server started (pid {proc.pid})  logs: {_LOG_PATH}"
                else:
                    new_msg = f"may have failed — check {_LOG_PATH}"

        elif verb in ("shutdown", "shutdown-server"):
            pid = _local_pid()
            if pid:
                os.kill(pid, _signal.SIGTERM)
                new_msg = f"SIGTERM → pid {pid}"
            else:
                new_msg = "orchestrator not found on this machine"

        elif verb == "kill-server":
            pid = _local_pid()
            if pid:
                os.kill(pid, _signal.SIGKILL)
                new_msg = f"SIGKILL → pid {pid}"
            else:
                new_msg = "orchestrator not found on this machine"

        elif verb == "status":
            online  = _server_online()
            r       = _exec("list_vms") if online else {}
            vms     = r.get("vms", [])
            running = sum(1 for v in vms if v.get("status") == "running")
            status  = "online" if online else "unreachable"
            new_msg = f"orchestrator={status}  vms={len(vms)}  running={running}"

        elif verb == "help":
            new_help_mode = True

        else:
            new_msg = f"unknown: {cmd}  (type 'help')"

    except Exception as e:
        new_msg = str(e)[:80]

    global _cmd_msg, _help_mode
    with _lock:
        _cmd_msg   = new_msg
        _help_mode = new_help_mode or _help_mode


# ── keyboard ──────────────────────────────────────────────────────────────────

def _handle_input(stdscr):
    global _cmd_buf, _cmd_msg, _help_mode
    while not _quit.is_set():
        try:
            ch = stdscr.get_wch()
        except curses.error:
            time.sleep(0.05)
            continue

        with _lock:
            if _help_mode:
                _help_mode = False
            elif ch in (3, "\x03", 27, "\x1b"):
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


# ── main loop ─────────────────────────────────────────────────────────────────

def _run(stdscr):
    cfg       = _load_json(os.path.join(_here, "admin_config.json"))
    color_hex = cfg.get("text_color", "#aaaaaa")
    font_size = int(cfg.get("font_size", 13))

    sys.stdout.write(f"\033]50;xft:Monospace:size={font_size}\007")
    sys.stdout.write("\033[8;52;200t")
    sys.stdout.flush()
    time.sleep(0.12)

    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.timeout(200)
    _init_colours(color_hex)
    curses.resizeterm(*stdscr.getmaxyx())

    start = time.monotonic()
    threading.Thread(target=_handle_input, args=(stdscr,), daemon=True).start()

    last_fetch = 0.0
    vms, events = [], []

    while not _quit.is_set():
        now = time.monotonic()
        if now - last_fetch >= _REFRESH:
            try:
                raw = _exec("list_vms")
                vms = raw.get("vms", [])
            except Exception:
                vms = []
            events     = _get_events(limit=_EVENTS_LIMIT)
            last_fetch = now

        _draw(stdscr, vms, events, now - start)
        time.sleep(0.05)


def main():
    try:
        curses.wrapper(_run)
    except KeyboardInterrupt:
        pass  # Ctrl-C — exit the admin TUI cleanly


if __name__ == "__main__":
    main()

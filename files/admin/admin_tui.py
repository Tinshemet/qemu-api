"""
admin_tui.py — Real-time admin TUI for gorgon

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
    """Load a JSON file, returning an empty dict on any error."""
    try:
        return _json.load(open(path))
    except Exception:
        return {}


_CONN_CFG  = _load_json(os.path.join(_here, "connection_config.json"))
_ADMIN_CFG = _load_json(os.path.join(_here, "admin_config.json"))

_ORCH_URL     = os.environ.get("SERVER_URL",  _CONN_CFG.get("orchestrator_url", "http://localhost:8080"))
_REFRESH      = _ADMIN_CFG.get("refresh_rate_s",       1.0)
_DEFAULT_PORT = _ADMIN_CFG.get("default_port",         8080)
_LOG_PATH     = _ADMIN_CFG.get("log_path",             "/tmp/gorgon-server.log")
_EVENTS_LIMIT = _ADMIN_CFG.get("events_display_limit", 200)


def _token() -> str:
    """Return the admin API token from the environment or config."""
    t = os.environ.get("API_TOKEN") or _CONN_CFG.get("token", "")
    if t:
        return t
    try:
        with open(os.path.expanduser("~/.gorgon.token")) as f:
            return f.read().strip()
    except Exception:
        return ""


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _post(path: str, body: dict) -> dict:
    """POST to an admin-server path; return the parsed JSON (or an error dict)."""
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
    """GET an admin-server path; return the parsed JSON (or an error dict)."""
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


def _exec(tool_name: str, args: dict = None, log: bool = True) -> dict:
    """Run a tool via the admin server's /execute endpoint.

    Unwraps the {"ok": bool, "result": ...} envelope so callers get the tool's
    actual result directly. Falls back to the raw response on a transport-level
    failure (_post's own {"success": False, "error": ...} on a network error),
    since that shape has no "result" key to unwrap.

    Passes verbose=True — the admin TUI is a machine caller polling once a second;
    without it the server prints a full Rich-rendered table to its own console/log
    on every single poll tick.

    log=False skips the server's persistent event log for this call — use it only
    for the dashboard's own automatic background refresh, never for a command the
    operator actually typed, otherwise the "list_vms" poll drowns the event feed.
    """
    r = _post("/execute", {"tool_name": tool_name, "args": args or {}, "verbose": True, "log": log})
    return r.get("result", r) if isinstance(r, dict) else r


def _get_events(limit: int = 200) -> list:
    """Return the most recent event-log entries from the server."""
    return _get("/events", {"limit": limit}).get("events", [])


# ── health check (cached 2s) ──────────────────────────────────────────────────

_health_cache: tuple = (0.0, False)


def _server_online() -> bool:
    """Return True if the admin server is reachable (result cached briefly)."""
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
    """Return the PID of a locally running server process, or None."""
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
    """Convert a #RRGGBB hex colour to a curses 0-1000 RGB triple."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (667, 667, 667)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (r * 1000 // 255, g * 1000 // 255, b * 1000 // 255)


def _init_colours(color_hex: str = "#aaaaaa") -> None:
    """Initialise curses colour pairs from the configured accent hex."""
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


def _cp(n: int) -> int:
    """Return the curses attribute for colour-pair number ``n``."""
    return curses.color_pair(n)


# ── draw ──────────────────────────────────────────────────────────────────────

def _hline(stdscr: "curses.window", row: int, w: int, label: str="") -> None:
    """Draw a horizontal rule at ``row``, optionally labelled."""
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


def _draw(stdscr: "curses.window", vms: list, events: list, uptime_s: float) -> None:
    """Redraw the dashboard — header, VM table, event table, and input line.

    stdscr.erase() only runs in the dashboard branch below, never while help
    is showing. Confirmed with a pty + raw-byte capture: erasing stdscr every
    tick even though it's never refreshed while help is open caused curses to
    emit a stray full-screen clear (`ESC[H ESC[J`) roughly one tick after the
    help box was drawn, wiping it with nothing to replace it. help_mode itself
    was never the problem — it stayed True throughout, confirmed by tracing.
    """
    global _cmd_buf, _cmd_msg
    h, w = stdscr.getmaxyx()

    if _help_mode:
        _draw_help(stdscr, h, w)
        return

    _reset_help_cache()
    stdscr.erase()

    uptime  = f"{int(uptime_s//3600):02d}:{int((uptime_s%3600)//60):02d}:{int(uptime_s%60):02d}"
    online  = _server_online()
    srv_str = f"online @ {_ORCH_URL}" if online else f"unreachable @ {_ORCH_URL}"
    hdr     = f"  gorgon admin   uptime {uptime}   vms {len(vms)}   events {len(events)}   {srv_str}  "
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


_help_win_key = None


def _reset_help_cache() -> None:
    """Force the next help open to redraw fresh (call whenever the dashboard,
    not help, is on screen — the physical screen has since been overwritten)."""
    global _help_win_key
    _help_win_key = None


def _allowed_tools() -> "set | None":
    """Return the executor's allowed-tools set for help filtering, or None if unrestricted.

    Mirrors client/cli/commands.py's _allowed_tools() exactly — same direct-import approach,
    same graceful fallback. Dispatch already enforces this allowlist for free (admin's _exec()
    goes through /execute, which executor_client.execute_tool() gates); this is purely about
    making the help overlay stop listing commands that would actually be rejected.
    """
    try:
        from orchestrator.executor_client import _ALLOWED_TOOLS
        return set(_ALLOWED_TOOLS) or None
    except Exception:
        return None


def _draw_help(stdscr: "curses.window", h: int, w: int) -> None:
    """Draw the help overlay listing the admin commands.

    Reuses the same curses window across frames instead of creating a fresh
    one every ~50ms tick. Measured with a pty harness: recreating the window
    every tick sent ~120KB/3s of terminal traffic for fully static content
    (~27x the dashboard's idle traffic, which reuses stdscr and lets curses'
    diffing correctly send almost nothing for an unchanged frame). That write
    volume, not a rendering-technique guess, was the confirmed cause of the
    help-only flicker.
    """
    global _help_win_key
    key = (h, w)
    if _help_win_key == key:
        return  # already drawn at this size — nothing has changed
    _help_win_key = key

    # Third element is the tool(s) required to actually run the command, or None for
    # admin-only actions (server control, navigation) that never go through /execute at
    # all and so are never allowlist-gated. Dispatch already enforces the allowlist
    # (admin's _exec() goes through /execute -> executor_client.execute_tool()) — this
    # filtering only keeps the help text honest about what would actually work.
    _RAW_SECTIONS = [
        ("VM Commands", [
            ("launch <vm>",  "Start a VM",                    ["launch_vm"]),
            ("stop <vm>",    "Graceful stop (SIGTERM)",        ["stop_vm"]),
            ("kill <vm>",    "Force-kill (SIGKILL)",           ["stop_vm"]),
            ("stopall",      "Stop all running VMs",           ["stop_vm"]),
            ("list",         "Print VM names in status line",  ["list_vms"]),
        ]),
        ("Server Commands (local only)", [
            ("start-server", "Start the orchestrator on this machine", None),
            ("shutdown",     "SIGTERM the orchestrator on this machine", None),
            ("kill-server",  "SIGKILL the orchestrator on this machine", None),
            ("status",       "Show orchestrator reachability + VM counts", None),
        ]),
        ("Navigation", [
            ("help",         "Show this overlay  (any key to close)", None),
            ("q / Esc",      "Quit the admin TUI", None),
        ]),
    ]

    allowed = _allowed_tools()

    def _cmd_visible(tools) -> bool:
        return not tools or allowed is None or any(t in allowed for t in tools)

    SECTIONS = [
        (section, [(cmd, desc) for cmd, desc, tools in cmds if _cmd_visible(tools)])
        for section, cmds in _RAW_SECTIONS
    ]
    SECTIONS = [(section, cmds) for section, cmds in SECTIONS if cmds]

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

def _dispatch(cmd: str) -> None:
    """Parse and run one admin command (stop/kill/launch/start-server/…)."""
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
            vms = r if isinstance(r, list) else []
            new_msg = "  ".join(v.get("name", "") for v in vms) or "(none)"

        elif verb == "stopall":
            r   = _exec("list_vms")
            vms = r if isinstance(r, list) else []
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
                    with open(os.path.expanduser("~/.gorgon.token")) as f:
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
            r       = _exec("list_vms") if online else []
            vms     = r if isinstance(r, list) else []
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

def _handle_key(ch) -> None:
    """Apply one keypress to the command buffer / help mode.

    Called only from the main loop in `_run()` — never spawn this on a
    background thread. curses/ncurses isn't safe to call concurrently from
    multiple threads against the same window; a prior version read keys on a
    separate thread while `_run()` drew from the main thread, and the two
    unsynchronized streams of curses calls caused visible corruption — most
    noticeable in the help overlay, which repaints a fresh window every tick.
    `_dispatch()` itself still runs on its own thread (it may block on a
    network call) but it never touches curses, only shared Python state
    under `_lock`, so that stays safe.
    """
    global _cmd_buf, _cmd_msg, _help_mode
    # A "real" keypress — as opposed to a non-key curses event (KEY_RESIZE, etc.)
    # that also flows through get_wch(). Only these should be able to dismiss
    # help; treating literally any event as "close" let a stray non-key event
    # slam it shut immediately after opening.
    is_real_key = isinstance(ch, str) or ch in (curses.KEY_ENTER, curses.KEY_BACKSPACE)
    with _lock:
        if _help_mode:
            if is_real_key:
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

def _run(stdscr: "curses.window") -> None:
    """curses main loop — poll server state, read input, and draw.

    All curses calls (get_wch + erase/addstr/refresh) happen from this one
    thread. See `_handle_key()` for why: reading input on a separate thread
    while this loop draws is what caused the visible corruption/flicker.
    """
    cfg       = _load_json(os.path.join(_here, "admin_config.json"))
    color_hex = cfg.get("text_color", "#aaaaaa")
    font_size = int(cfg.get("font_size", 13))

    sys.stdout.write(f"\033]50;xft:Monospace:size={font_size}\007")
    sys.stdout.write("\033[8;52;200t")
    sys.stdout.flush()
    time.sleep(0.12)

    curses.curs_set(0)
    curses.nonl()
    stdscr.nodelay(True)
    _init_colours(color_hex)
    curses.resizeterm(*stdscr.getmaxyx())

    start = time.monotonic()

    last_fetch = 0.0
    vms, events = [], []

    while not _quit.is_set():
        # Drain every buffered key immediately (nodelay(True) never blocks) —
        # relying on a curses read timeout to pace the loop made the redraw
        # rate depend on terminal/curses-build timing behavior, which made
        # the flicker worse. The explicit sleep below is what paces frames.
        while True:
            try:
                ch = stdscr.get_wch()
            except curses.error:
                break
            _handle_key(ch)

        now = time.monotonic()
        if now - last_fetch >= _REFRESH:
            try:
                raw = _exec("list_vms", log=False)
                vms = raw if isinstance(raw, list) else []
            except Exception:
                vms = []
            events     = _get_events(limit=_EVENTS_LIMIT)
            last_fetch = now

        _draw(stdscr, vms, events, now - start)
        time.sleep(0.05)


def main() -> None:
    """Entry point — run the admin TUI until the user exits."""
    try:
        curses.wrapper(_run)
    except KeyboardInterrupt:
        pass  # Ctrl-C — exit the admin TUI cleanly


if __name__ == "__main__":
    main()

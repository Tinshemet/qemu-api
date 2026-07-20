"""
chat_client.py — Curses AI Chat Client

Full-screen TUI that sends messages to the gorgon server's /chat endpoint.
Mirrors the admin TUI visual style: header bar, scrollable chat area,
command input at bottom.
"""

import curses
import json
import os
import queue
import sys
import textwrap
import threading
import time
import uuid
from typing import Optional

import requests

try:
    # Same defensive-import reasoning as client/cli/commands.py — this module
    # runs on client-only checkouts too, where orchestrator/ may be absent.
    from orchestrator.auth import store as _auth_store, sessions as _auth_sessions
except ImportError:
    _auth_store    = None                                          # type: ignore[assignment]
    _auth_sessions = None                                          # type: ignore[assignment]

# ── Connection config ─────────────────────────────────────────────────────────

_CFG_PATH  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "connection_config.json")
_CFG       = json.load(open(_CFG_PATH))
_UI_CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "CLI_config.json")
_UI_CFG      = json.load(open(_UI_CFG_PATH)) if os.path.exists(_UI_CFG_PATH) else {}

_WRAP_WIDTH         = _UI_CFG.get("wrap_width",              120)
_AUTOSTART_POLLS    = _UI_CFG.get("autostart_poll_count",     20)
_AUTOSTART_INTERVAL = _UI_CFG.get("autostart_poll_interval_s", 0.5)
_ISO_DISTRO_KEYWORDS = [
    (pair[0], pair[1]) for pair in _UI_CFG.get("iso_distro_keywords", [])
]

SERVER_URL = os.environ.get("SERVER_URL",   _CFG.get("server_url", "http://localhost:8080"))
_TOKEN     = os.environ.get("API_TOKEN",    _CFG.get("token",      ""))
_TIMEOUT   = int(os.environ.get("API_TIMEOUT", _CFG.get("timeout", 120)))
_CA_CERT   = os.environ.get("API_CA_CERT", _CFG.get("ca_cert") or None)
_VERIFY    = (
    False if os.environ.get("API_VERIFY_SSL", "1") == "0"
    else (_CA_CERT or _CFG.get("verify_ssl", True))
)
# Prefer this box's logged-in operator session over the static API_TOKEN —
# /chat now requires a session specifically once an operator account exists
# (see orchestrator/http/api_server.py's _require_operator_auth), so the
# static token alone would otherwise get every message rejected even for a
# legitimately logged-in operator.
_SESSION_TOKEN = _auth_sessions.read_current_session() if _auth_sessions else None
_EFFECTIVE_TOKEN = _SESSION_TOKEN or _TOKEN
_HEADERS   = {"Authorization": f"Bearer {_EFFECTIVE_TOKEN}"} if _EFFECTIVE_TOKEN else {}

# ── Session persistence ───────────────────────────────────────────────────────

_SESSION_FILE = os.path.expanduser("~/.qemu_vms/.chat_session_id")


def _load_session_id() -> str:
    """Return the persisted chat session id, generating and saving one if absent."""
    try:
        return open(_SESSION_FILE).read().strip()
    except FileNotFoundError:
        return ""


def _save_session_id(sid: str) -> None:
    """Persist the chat session id to the local session file."""
    os.makedirs(os.path.dirname(_SESSION_FILE), exist_ok=True)
    with open(_SESSION_FILE, "w") as f:
        f.write(sid)


# ── Colour pairs ──────────────────────────────────────────────────────────────

C_HEADER = 1
C_CYAN   = 2
C_GREEN  = 3
C_RED    = 4
C_DIM    = 5
C_YELLOW = 6
C_BOLD   = 7


_CUSTOM_COLOR_SLOT = 16   # first free slot above the standard 8+8


def _hex_to_curses(hex_color: str) -> tuple:
    """Parse a ``#RRGGBB`` hex string to (r, g, b) scaled 0-1000 for curses.

    Args:
        hex_color: Hex color string, with or without leading ``#``.

    Returns:
        ``(r, g, b)`` each in the range [0, 1000] as required by
        ``curses.init_color()``. Returns ``(667, 667, 667)`` on bad input.

    Example::

        _hex_to_curses("#7355a3")  # → (451, 333, 639)
        _hex_to_curses("#ffffff")  # → (1000, 1000, 1000)
        _hex_to_curses("bad")      # → (667, 667, 667)
    """
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (667, 667, 667)   # fallback ~gray
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
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
    curses.init_pair(C_BOLD,   curses.COLOR_WHITE,  -1)

    if curses.can_change_color():
        r, g, b = _hex_to_curses(color_hex)
        curses.init_color(_CUSTOM_COLOR_SLOT, r, g, b)
        curses.init_pair(C_DIM, _CUSTOM_COLOR_SLOT, -1)
    else:
        # Terminal can't redefine colors — fall back to nearest standard
        curses.init_pair(C_DIM, 8, -1)  # bright-black (gray)


def _cp(n: int) -> int:
    """Return the curses attribute for colour-pair number ``n``."""
    return curses.color_pair(n)


# ── Shared state ──────────────────────────────────────────────────────────────

_history:       list  = []        # (curses_attr, text) tuples
_lock           = threading.Lock()
_resp_q         = queue.Queue()   # HTTP worker puts results here
_quit           = threading.Event()
_waiting        = False           # True while HTTP call is in flight
_needs_confirm  = False           # True when server returned needs_input
_is_confirm     = False           # whether pending confirm is auto_confirm
_is_password    = False           # True when the server asked for a masked password (forge wizard)
_allow_empty    = False           # True when the server's prompt accepts an empty answer (skip optional field)
_pending_kill   = ""              # VM name waiting for force-kill confirmation
_pending_claim  = None            # (action, fact) waiting for operator password (claim confirm/reject)
_session_id     = ""

# sync data
_REMOTE_VMS:        list = []
_REMOTE_PROFILES:   list = []
_COMMANDS:          list = []          # command catalog from /sync (help source of truth)
_ALLOWED_TOOLS:     set  = set()       # executor allowed-tools list for help filtering
_SC_LIST      = {"list", "vms", "ls"}
_SC_SYSTEM    = {"system"}
_SC_PROFILES  = {"profiles"}
_SC_TEMPLATES = {"templates"}
_SC_DRIFT     = {"drift"}
_SC_CLEAR     = {"clear session", "forget", "/clear"}
_SC_HELP     = {"help", "?", "/help"}
_EXIT_CMDS   = {"exit", "quit", "q", "bye"}


# ── History helpers ───────────────────────────────────────────────────────────

def _add(text: str, attr: int = 0, wrap: int = 0) -> None:
    """Append a line to the scrollback buffer (thread-safe), optionally wrapping."""
    with _lock:
        if wrap:
            for line in textwrap.wrap(text, wrap) or [""]:
                _history.append((attr, line))
        else:
            _history.append((attr, text))


def _add_sep() -> None:
    """Append a dim horizontal separator to the scrollback."""
    _add("  " + "─" * 62, _cp(C_DIM))


# ── Draw ──────────────────────────────────────────────────────────────────────

def _draw(stdscr: "curses.window", input_buf: str) -> None:
    """Redraw the full TUI — scrollback, separators, and the input/status line."""
    h, w = stdscr.getmaxyx()
    stdscr.erase()

    # Header
    spin_chars = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    spin  = f" {spin_chars[int(time.time() * 5) % len(spin_chars)]}" if _waiting else "  "
    with _lock:
        vm_parts = [
            ("● " if v.get("status") == "running" else "○ ") + v.get("name", "")
            for v in _REMOTE_VMS[:6]
        ]
    vm_str = "   ".join(vm_parts)
    hdr    = f" gorgon{spin} {SERVER_URL}   {vm_str}"
    try:
        stdscr.addstr(0, 0, hdr[:w - 1].ljust(w - 1), _cp(C_HEADER) | curses.A_BOLD)
    except curses.error:
        pass  # addstr fails past the screen edge — skip drawing the header

    # Separator
    try:
        stdscr.addstr(1, 0, "─" * (w - 1), _cp(C_DIM))
    except curses.error:
        pass  # addstr fails past the screen edge — skip the separator

    # Chat history (rows 2 .. h-5)
    chat_rows = max(1, h - 6)
    with _lock:
        visible = list(_history[-chat_rows:])
    for i, (attr, text) in enumerate(visible):
        row = 2 + i
        if row >= h - 4:
            break
        try:
            stdscr.addstr(row, 0, text[:w - 1], attr)
        except curses.error:
            pass  # addstr fails past the screen edge — skip this message row

    # Input separator
    try:
        stdscr.addstr(h - 4, 0, "─" * (w - 1), _cp(C_DIM))
    except curses.error:
        pass  # addstr fails past the screen edge — skip the input separator

    # Input / waiting line
    if _waiting:
        try:
            stdscr.addstr(h - 3, 0, f" ⟳ waiting for response...", _cp(C_DIM))
        except curses.error:
            pass  # addstr fails past the screen edge — skip the waiting line
    else:
        _shown = ("•" * len(input_buf)) if _is_password else input_buf
        prompt = f" > {_shown}"
        try:
            stdscr.addstr(h - 3, 0, prompt[:w - 1], _cp(C_CYAN) | curses.A_BOLD)
        except curses.error:
            pass  # addstr fails past the screen edge — skip the prompt

    # Hint line
    try:
        stdscr.addstr(h - 2, 0,
                      "  list  system  profiles  templates  drift  /clear  help  q=quit"[:w - 1],
                      _cp(C_DIM))
    except curses.error:
        pass  # addstr fails past the screen edge — skip the hint line

    stdscr.refresh()


# ── VNC helpers ───────────────────────────────────────────────────────────────

def _vnc_host() -> str:
    """Return the host to point a VNC viewer at (derived from the server URL)."""
    from urllib.parse import urlparse
    parsed = urlparse(SERVER_URL)
    host   = parsed.hostname or "localhost"
    return "localhost" if host in ("localhost", "127.0.0.1", "::1") else host


def _try_open_vnc(port: int) -> Optional[str]:
    """Try each configured VNC viewer; return the one that launched, or None."""
    import subprocess as _sp
    host = _vnc_host()
    for viewer in ("vncviewer", "tigervncviewer", "xtigervncviewer", "gvncviewer", "vinagre"):
        try:
            _sp.Popen([viewer, f"{host}:{port}"],
                      stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            return viewer
        except FileNotFoundError:
            continue
    return None


# ── Tool result rendering ─────────────────────────────────────────────────────


def _iso_distro_hint(iso_name: str) -> str:
    """Return the distro name implied by the ISO filename.

    Args:
        iso_name: ISO filename or path (only the basename is examined).

    Returns:
        Lowercase distro name (e.g. ``"ubuntu"``, ``"windows"``), or
        ``''`` if no keyword matched.

    Example::

        _iso_distro_hint("ubuntu-22.04-desktop-amd64.iso")  # → "ubuntu"
        _iso_distro_hint("Win11_23H2_English_x64.iso")       # → "windows"
        _iso_distro_hint("unknown.iso")                       # → ""
    """
    s = iso_name.lower()
    for keyword, distro in _ISO_DISTRO_KEYWORDS:
        if keyword in s:
            return distro
    return ""

def _render_tool_result(tool: str, result: dict) -> None:
    """Render a tool result into the scrollback, formatted per tool type."""
    if tool == "list_vms":
        vms = result if isinstance(result, list) else result.get("vms", [])
        if not vms:
            _add("  (no VMs)", _cp(C_DIM))
            return
        for v in vms:
            status = v.get("status", "?")
            dot    = "● " if status == "running" else "○ "
            color  = _cp(C_GREEN) if status == "running" else _cp(C_DIM)
            ram    = f"{v.get('memory_mb', 0) // 1024}GB"
            cpu    = v.get("cpu_cores", "")
            os_s   = v.get("os", "")[:18]
            name   = v.get("name", "")[:22]
            flags  = v.get("flags") or []
            labels = v.get("labels") or []
            tags   = f"  [{' '.join(flags)}]" if flags else ""
            if labels:
                tags += "  " + " ".join(f"#{l}" for l in labels)
            _add(f"  {dot}{name:<22} {status:<12} {cpu}cpu  {ram:<6} {os_s:<18}{tags}", color)

    elif tool == "launch_vm":
        if result.get("success") or result.get("already_running"):
            port   = result.get("vnc_port", 5900)
            host   = _vnc_host()
            viewer = _try_open_vnc(port)
            msg    = f"  ✓ VNC: {host}:{port}"
            if viewer:
                msg += f"  (opened {viewer})"
            else:
                msg += f"  —  run: vncviewer {host}:{port}"
            _add(msg, _cp(C_GREEN) | curses.A_BOLD)
        else:
            _add(f"  ✖ {result.get('error', 'launch failed')}", _cp(C_RED))

    elif tool == "check_system":
        caps = result
        kvm  = caps.get("kvm_available") and caps.get("kvm_readable")
        virt = caps.get("vmx") or caps.get("svm")
        ovmf = caps.get("ovmf") or {}
        rows = [
            ("CPU",        f"{caps.get('host_cpu_cores', '?')} cores  ({caps.get('host_cpu', '?')})"),
            ("RAM",        f"{caps.get('host_memory_mb', 0) // 1024} GB"),
            ("Disk free",  f"{caps.get('home_free_gb', '?')} GB"),
            ("Arch",       caps.get("host_arch", "?")),
            ("KVM",        "✓" if kvm  else "✗"),
            ("VT-x/AMD-V", "✓" if virt else "✗"),
        ]
        qemu = caps.get("qemu_version", "")
        if qemu:
            rows.append(("QEMU", qemu[:70]))
        if caps.get("qemu_arm_installed"):
            rows.append(("qemu-arm", "✓"))
        if ovmf.get("code"):
            rows.append(("OVMF", ovmf["code"]))
        for label, value in rows:
            if value in ("✓", "✗"):
                attr = _cp(C_GREEN) if value == "✓" else _cp(C_RED)
            else:
                attr = _cp(C_DIM)
            _add(f"    {label:<16} {value}", attr)

    elif tool == "create_vm":
        if result.get("success"):
            _vm_msg = result.get("message") or f"VM '{result.get('name', '')}' created."
            _add(f"  ✓ {_vm_msg}", _cp(C_GREEN))
            iso_name = (result.get("iso_name") or "").lower()
            os_name  = (result.get("os_name")  or "").lower()
            iso_distro = _iso_distro_hint(iso_name)
            if iso_distro and os_name and iso_distro not in os_name and os_name not in iso_distro:
                _add(f"  ⚠ ISO ({result['iso_name']}) looks like {iso_distro}"
                     f" but OS declared as '{result['os_name']}' — may be wrong.",
                     _cp(C_YELLOW) | curses.A_BOLD)
                _add( "    To fix: delete the VM and recreate, specifying the correct OS name.",
                     _cp(C_DIM))
            elif iso_distro and not os_name:
                _add(f"  ℹ ISO suggests distro: {iso_distro}", _cp(C_DIM))
        else:
            _add(f"  ✖ {result.get('error', 'create_vm failed')}", _cp(C_RED))

    elif tool in ("list_profiles",):
        profiles = result if isinstance(result, list) else result.get("profiles", [])
        for p in profiles:
            name = (p.get("name", "") if isinstance(p, dict) else str(p))
            desc = (p.get("description", "") if isinstance(p, dict) else "")
            _add(f"  {name:<28} {desc}", _cp(C_DIM))
        if not profiles:
            _add("  (no profiles)", _cp(C_DIM))

    elif tool == "list_templates":
        templates = result if isinstance(result, list) else result.get("templates", [])
        for t in templates:
            name    = t.get("name", "") if isinstance(t, dict) else str(t)
            os_type = t.get("os_type", "") if isinstance(t, dict) else ""
            disks   = t.get("disks", "") if isinstance(t, dict) else ""
            _add(f"  {name:<28} {os_type:<10} {disks} disk(s)", _cp(C_DIM))
        if not templates:
            _add("  (no templates)", _cp(C_DIM))
        # mark_as_template / remove_template need no dedicated branch — both return a
        # plain {"success", "message"/"error"} dict already handled by the generic
        # fallback further down.

    elif tool in ("vm_status", "monitor_vm"):
        status = result.get("status", "?")
        color  = _cp(C_GREEN) if status == "running" else _cp(C_DIM)
        _add(f"  {result.get('name', '')}  status={status}  "
             f"cpu={result.get('cpu', '?')}%  mem={result.get('memory', '?')}", color)

    elif tool == "list_snapshots":
        snaps = result if isinstance(result, list) else result.get("snapshots", [])
        for s in snaps:
            _add(f"  {s.get('name', ''):<24} {s.get('date', '')}", _cp(C_DIM))
        if not snaps:
            _add("  (no snapshots)", _cp(C_DIM))

    elif result.get("setup_cmd"):
        setup_cmd = result["setup_cmd"]
        vm_name   = result.get("name", "")
        is_win    = setup_cmd.startswith("irm ")
        dest      = "PowerShell inside the VM" if is_win else "a terminal inside the VM (then reboot)"
        _add(f"  ▶ Stealth setup required. Open {dest} and run:", _cp(C_YELLOW) | curses.A_BOLD)
        _add(f"      {setup_cmd}", _cp(C_CYAN))
        _add(f"  When done:  setup-done {vm_name}", _cp(C_DIM))

    elif result.get("vnc_connect_cmd"):
        _add(f"  VNC: {result['vnc_connect_cmd']}", _cp(C_CYAN))

    elif not result.get("success") and result.get("error"):
        _add(f"  ✖ {result['error']}", _cp(C_RED))

    elif result.get("success") and result.get("message"):
        _add(f"  ✓ {result['message']}", _cp(C_GREEN))


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _post_chat(message: str, session_id: str,
               auto_confirm: bool = False, verbose: bool = False) -> dict:
    """POST a message to the server's /chat endpoint; return the parsed response."""
    payload = {
        "message":      message,
        "session_id":   session_id,
        "auto_confirm": auto_confirm,
        "verbose":      verbose,
    }
    try:
        resp = requests.post(
            f"{SERVER_URL}/chat",
            json=payload, headers=_HEADERS,
            timeout=_TIMEOUT, verify=_VERIFY,
        )
    except requests.ConnectionError:
        return {"error": f"Cannot connect to {SERVER_URL}"}
    except Exception as e:
        return {"error": str(e)}

    if resp.status_code == 401:
        return {"error": "Server rejected token (401) — check API_TOKEN"}
    if not resp.ok:
        return {"error": f"Server error {resp.status_code}"}

    try:
        return resp.json()
    except Exception as e:
        return {"error": f"Invalid JSON from server: {e}"}


def _execute(tool_name: str, args: dict | None = None) -> dict:
    """POST a direct tool call to the server's /execute endpoint; return the result."""
    if args is None:
        args = {}
    try:
        resp = requests.post(
            f"{SERVER_URL}/execute",
            json={"tool_name": tool_name, "args": args, "verbose": False},
            headers=_HEADERS, timeout=_TIMEOUT, verify=_VERIFY,
        )
        if not resp.ok:
            try:
                body = resp.json()
                msg = body.get("result", {}).get("error") or body.get("detail") or f"Server error {resp.status_code}"
            except Exception:
                msg = f"Server error {resp.status_code}"
            return {"success": False, "error": msg}
        return resp.json().get("result", {})
    except requests.ConnectionError:
        return {"success": False, "error": f"Cannot connect to {SERVER_URL}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Auto-start server (localhost only) ───────────────────────────────────────

def _is_localhost() -> bool:
    """Return True when the configured server URL points at the local machine."""
    from urllib.parse import urlparse
    host = urlparse(SERVER_URL).hostname or "localhost"
    return host in ("localhost", "127.0.0.1", "::1")


def _server_reachable() -> bool:
    """Return True if the server's /health endpoint answers."""
    try:
        r = requests.get(f"{SERVER_URL}/health", timeout=2, verify=_VERIFY)
        return r.ok
    except Exception:
        return False


def _autostart_server(stdscr: "curses.window") -> bool:
    """Launch the orchestrator if its files are present alongside the client. Returns True when ready."""
    _client_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _files_dir  = os.path.dirname(_client_dir)
    _orch_mod   = os.path.join(_files_dir, "orchestrator", "http", "api_server.py")

    if not os.path.exists(_orch_mod):
        return False

    from urllib.parse import urlparse
    port = urlparse(SERVER_URL).port or 8080

    env = os.environ.copy()
    env["PYTHONPATH"] = _files_dir
    try:
        token = open(os.path.expanduser("~/.gorgon.token")).read().strip()
        env["API_TOKEN"] = token
    except Exception:
        pass  # no token file — run without an API token (orchestrator may allow it)

    _log_path = _UI_CFG.get("log_path", "/tmp/gorgon-orchestrator.log")
    import subprocess as _sp
    _sp.Popen(
        [sys.executable, "-m", "uvicorn",
         "orchestrator.http.api_server:app",
         "--host", "0.0.0.0", f"--port", str(port),
         "--log-level", "warning"],
        cwd=_files_dir, env=env,
        start_new_session=True,
        stdout=open(_log_path, "w"),
        stderr=_sp.STDOUT,
    )

    for _ in range(_AUTOSTART_POLLS):
        time.sleep(_AUTOSTART_INTERVAL)
        _draw(stdscr, "")
        if _server_reachable():
            return True

    return False


# ── Sync ──────────────────────────────────────────────────────────────────────

def _sync_from_server() -> bool:
    """Refresh the cached remote VM/profile lists from the server; return success."""
    global _REMOTE_VMS, _REMOTE_PROFILES, _COMMANDS, _ALLOWED_TOOLS
    global _SC_LIST, _SC_SYSTEM, _SC_PROFILES, _SC_TEMPLATES, _SC_DRIFT, _SC_CLEAR
    try:
        resp = requests.get(f"{SERVER_URL}/sync",
                            headers=_HEADERS, timeout=10, verify=_VERIFY)
        if not resp.ok:
            return False
        data = resp.json()
    except Exception:
        return False

    sc = data.get("shortcut_commands", {})
    if sc.get("list"):          _SC_LIST      = set(sc["list"])
    if sc.get("system"):        _SC_SYSTEM    = set(sc["system"])
    if sc.get("profiles"):      _SC_PROFILES  = set(sc["profiles"])
    if sc.get("templates"):     _SC_TEMPLATES = set(sc["templates"])
    if sc.get("drift"):         _SC_DRIFT     = set(sc["drift"])
    if sc.get("clear_session"): _SC_CLEAR     = set(sc["clear_session"]) | {"/clear"}

    _REMOTE_VMS      = data.get("vms", [])
    _REMOTE_PROFILES = data.get("profiles", [])
    _COMMANDS        = data.get("commands", []) or _COMMANDS
    _ALLOWED_TOOLS   = set(data.get("allowed_remote_tools", []))
    return True


# ── Response processing ───────────────────────────────────────────────────────

def _process_response(result: dict, verbose: bool = False) -> None:
    """Apply a /chat response — render text/tools and update confirm state."""
    global _session_id, _needs_confirm, _is_confirm, _is_password, _allow_empty

    if result.get("error"):
        _add(f"  ✖ {result['error']}", _cp(C_RED))
        return

    sid = result.get("session_id", _session_id)
    if sid:
        _session_id = sid
        _save_session_id(sid)

    for tr in result.get("tool_results", []):
        tool = tr.get("tool", "")
        res  = tr.get("result", {})
        if tool:
            _add(f"  [{tool}]", _cp(C_DIM))
        _render_tool_result(tool, res)

    text = result.get("text", "").strip()
    if text:
        _add(f" AI:", _cp(C_CYAN) | curses.A_BOLD)
        # Preserve the server's line breaks — wrap each line on its own so a
        # multi-line reply (issue lists, the rendered contract box) keeps its
        # structure instead of collapsing into one re-wrapped paragraph.
        for para in text.split("\n"):
            for line in textwrap.wrap(para, _WRAP_WIDTH) or [""]:
                _add(f"    {line}", _cp(C_CYAN))

    ni = result.get("needs_input")
    if ni:
        _needs_confirm = True
        ni_type  = ni.get("type", "clarify")
        question = ni.get("question", "Confirm?")
        opts     = ni.get("options", [])
        proposed = ni.get("proposed", "")
        _is_confirm = ni_type in ("confirm_yn", "confirm_name", "confirm_critical", "preflight")
        _is_password = ni_type == "password"
        # 'prompt' is a free-text wizard answer — the question is already in the AI
        # text, so don't re-render a ▶ line; allow_empty lets a blank Enter through.
        _allow_empty = ni_type == "prompt" and bool(ni.get("allow_empty"))
        if ni_type != "prompt":
            color = _cp(C_RED) if ni_type == "confirm_critical" else _cp(C_YELLOW)
            _add(f"  ▶ {question}", color | curses.A_BOLD)
            if proposed:
                _add(f"    Type exactly: {proposed}", _cp(C_RED))
            if opts:
                _add(f"    Options: {' / '.join(opts)}", _cp(C_DIM))
    else:
        _needs_confirm = False
        _is_confirm    = False
        _is_password   = False
        _allow_empty   = False


# ── Help ──────────────────────────────────────────────────────────────────────

def _show_help() -> None:
    """Print the in-TUI command help into the scrollback, rendered from the command catalog.

    Shows every command available to the user (filtered to the allowed-tools list) plus
    a short example prompt per command for the AI. Both the catalog and the allow-list
    come from /sync; falls back to the local catalog when the server list is empty.
    """
    from shared.command_help import visible_commands, load_local_catalog
    catalog = _COMMANDS
    if not catalog:
        catalog, _ = load_local_catalog()
    _add_sep()

    if not catalog:
        _add("  Command list unavailable (sync the server to load it).", _cp(C_DIM))
        _add_sep()
        return

    entries = visible_commands(catalog, _ALLOWED_TOOLS or None)
    _add("  Commands (type the word, or just ask in plain language):",
         _cp(C_CYAN) | curses.A_BOLD)
    for e in entries:
        verb = e["command"] + (" " + e["args"] if e["args"] else "")
        _add(f"    {verb:<28} {e['desc']}", _cp(C_DIM))

    _add("  Example prompts for the AI:", _cp(C_CYAN) | curses.A_BOLD)
    for e in entries:
        ex = e.get("ai_example")
        if ex:
            _add(f"    {ex}", _cp(C_DIM))

    _add("  Built-in: drift · clear/clear session · help/? · q/quit/exit", _cp(C_DIM))
    _add("  Operator: mission [list|run <name>|\"<goal>\"] · claim [list|confirm <fact>|reject <fact>]",
         _cp(C_DIM))
    _add_sep()


# ── HTTP worker ───────────────────────────────────────────────────────────────

def _http_worker(message: str, auto_confirm: bool, verbose: bool) -> None:
    """Background thread body — send one message and queue the response."""
    result = _post_chat(message, _session_id, auto_confirm, verbose)
    _resp_q.put(result)


# ── mission / claim (operator verbs, wired into the chat) ─────────────────────

def _mission_worker(goal, mission_name, verbose) -> None:
    """Background thread body — run an autonomous mission and queue its result. The
    autonomous loop is long-running, so it can't block the curses draw loop; the
    result is rendered on the main thread from the drain (see _render_mission_result)."""
    try:
        from orchestrator.ai.autonomous import run_autonomous_live
        from orchestrator.ai import mission as M
        if mission_name:
            mobj, st = M.load(mission_name)
            if not mobj:
                _resp_q.put({"_mission": {"goal": mission_name, "error": f"no mission '{mission_name}' ({st})"}})
                return
            goal = mobj.goal
        else:
            mobj = M.Mission.ephemeral(goal)
        result = run_autonomous_live(goal, mission=mobj)
        _resp_q.put({"_mission": {"goal": goal, "result": result}})
    except Exception as e:                                   # never let a worker crash take down the UI
        _resp_q.put({"_mission": {"goal": goal or mission_name, "error": str(e)}})


def _render_mission_result(m: dict) -> None:
    """Render a queued mission result on the main thread."""
    if m.get("error"):
        _add(f"  ✖ mission failed: {m['error']}", _cp(C_RED))
        return
    r = m.get("result", {})
    s = r.get("summary", {}) or {}
    ok = r.get("ok")
    _add(f"  {'✔' if ok else '✖'} mission '{m['goal']}' — status={s.get('status')} "
         f"executed={s.get('executed')}", _cp(C_GREEN if ok else C_YELLOW) | curses.A_BOLD)
    econ = r.get("economics")
    if econ:
        _add(f"    economics: μ={econ.get('mu')} ce={econ.get('ce')} "
             f"cost={econ.get('cost')} reward={econ.get('reward')}", _cp(C_DIM))
    review = r.get("claims_for_review") or []
    if review:
        _add(f"    ⚠ {len(review)} claim(s) need confirmation — claim confirm <fact>:", _cp(C_YELLOW))
        for c in review:
            _add(f"      {c['fact']} = {c['value']!r}   ← {c.get('evidence') or '—'}", _cp(C_DIM))


def _apply_claim(action: str, fact: str) -> None:
    """Confirm/reject a claim in the active agent's store (post operator re-auth)."""
    from orchestrator.ai import findings_store as store
    from orchestrator.ai.contract import active_agent_key
    key = active_agent_key()
    ok = store.confirm(key, fact) if action == "confirm" else store.reject(key, fact)
    if ok:
        _add(f"  ✔ {action}ed {fact}", _cp(C_GREEN))
    else:
        _add(f"  ✖ no {'pending ' if action == 'confirm' else ''}claim '{fact}'", _cp(C_RED))


def _handle_claim(arg: str) -> None:
    """`claim [list] | confirm <fact> | reject <fact>` inside the chat."""
    global _pending_claim, _is_password
    from orchestrator.ai import findings_store as store
    from orchestrator.ai.contract import active_agent_key
    key = active_agent_key()
    parts = arg.split()
    sub = parts[0] if parts else "list"
    if sub == "list" or not parts:
        data = store.listing(key)
        _add(f"  Claims for agent {key}", _cp(C_CYAN) | curses.A_BOLD)
        if not data["pending"] and not data["verified"]:
            _add("    none", _cp(C_DIM))
        for e in data["pending"]:
            _add(f"    [pending] {e['fact']} = {e['value']!r}", _cp(C_YELLOW))
            _add(f"        evidence: {e.get('evidence') or '—'}", _cp(C_DIM))
        for e in data["verified"]:
            _add(f"    [verified] {e['fact']} = {e['value']!r}", _cp(C_GREEN))
    elif sub in ("confirm", "reject") and len(parts) >= 2:
        fact = " ".join(parts[1:])
        if _auth_store is not None and _auth_store.operators_exist():
            _pending_claim = (sub, fact)        # main loop captures the masked password next
            _is_password = True
            _add(f"  Operator password to {sub} {fact}:", _cp(C_YELLOW) | curses.A_BOLD)
        else:
            _apply_claim(sub, fact)             # pre-bootstrap / no operators → no gate
    else:
        _add("  Usage: claim [list] | confirm <fact> | reject <fact>", _cp(C_DIM))


def _handle_mission(arg: str, verbose: bool) -> None:
    """`mission [list] | run <name> | new | "<goal>"` inside the chat."""
    global _waiting
    from orchestrator.ai import mission as M
    from orchestrator.ai.contract import active_agent_key
    parts = arg.split()
    sub = parts[0] if parts else "list"
    if sub == "list" or not parts:
        ms = M.list_missions()
        _add(f"  Missions for agent {active_agent_key()}", _cp(C_CYAN) | curses.A_BOLD)
        if not ms:
            _add("    none — author one in a terminal: gorgon mission new", _cp(C_DIM))
        for m in ms:
            _add(f"    {m['name']:<22} {m['title']}  ({m['status']})", _cp(C_DIM))
    elif sub == "new":
        _add("  The mission wizard needs a full terminal prompt — run: gorgon mission new", _cp(C_YELLOW))
    elif sub == "run" and len(parts) >= 2:
        _add(f"  ▶ running mission {parts[1]}…", _cp(C_CYAN))
        _waiting = True
        threading.Thread(target=_mission_worker, args=(None, parts[1], verbose), daemon=True).start()
    elif sub == "run":
        _add("  Usage: mission run <name>", _cp(C_DIM))
    else:                                       # bare goal → quick ephemeral mission
        _add(f"  ▶ mission: {arg}…", _cp(C_CYAN))
        _waiting = True
        threading.Thread(target=_mission_worker, args=(arg, None, verbose), daemon=True).start()


# ── Dispatch ──────────────────────────────────────────────────────────────────

def _dispatch(cmd: str, verbose: bool) -> bool:
    """Handle a built-in shortcut. Returns True if handled (no HTTP needed)."""
    global _waiting, _pending_kill, _needs_confirm, _is_confirm, _session_id, _is_password

    low = cmd.lower().strip()

    if low in _EXIT_CMDS:
        _quit.set()
        return True

    if low in _SC_CLEAR:
        try:
            requests.delete(f"{SERVER_URL}/sessions/{_session_id}",
                            headers=_HEADERS, timeout=10, verify=_VERIFY)
        except Exception:
            pass  # best-effort server-side session clear — ignore network errors
        _session_id = str(uuid.uuid4())
        _save_session_id(_session_id)
        _add("  Session cleared.", _cp(C_DIM))
        _needs_confirm = False
        _is_confirm    = False
        _is_password   = False
        return True

    if low in _SC_HELP:
        _show_help()
        return True

    # Operator verbs, wired into the chat (mirror the terminal `gorgon claim/mission`).
    if low == "claim" or low.startswith("claim "):
        _handle_claim(cmd.strip()[len("claim"):].strip())
        return True
    if low == "mission" or low.startswith("mission "):
        _handle_mission(cmd.strip()[len("mission"):].strip(), verbose)
        return True

    # list / list <label> — an optional trailing flag or user label filters the list
    _list_pfx = next((p for p in ("list ", "vms ", "ls ") if low.startswith(p)), None)
    if low in _SC_LIST or _list_pfx:
        label = cmd.strip()[len(_list_pfx):].strip() if (_list_pfx and low not in _SC_LIST) else ""
        result = _execute("list_vms", {"label": label} if label else {})
        _render_tool_result("list_vms", result)
        return True

    if low in _SC_SYSTEM:
        result = _execute("check_system")
        _render_tool_result("check_system", result)
        return True

    if low in _SC_PROFILES:
        result = _execute("list_profiles")
        _render_tool_result("list_profiles", result)
        return True

    if low in _SC_TEMPLATES:
        result = _execute("list_templates")
        _render_tool_result("list_templates", result)
        return True

    if low in _SC_DRIFT:
        result = _execute("check_drift")
        if result.get("drifted"):
            _add("  Drift detected:", _cp(C_YELLOW) | curses.A_BOLD)
            for k, v in result.items():
                if k != "drifted":
                    _add(f"    {k}: {v}", _cp(C_DIM))
        else:
            _add("  ✓ No drift detected.", _cp(C_GREEN))
        return True

    # kill <name> shortcut
    for pfx in ("kill ", "force stop ", "force kill ", "hard stop "):
        if low.startswith(pfx):
            name = cmd[len(pfx):].strip()
            if name:
                _pending_kill = name
                _add(f"  Force-kill (SIGKILL) VM: {name}?  [yes / cancel]",
                     _cp(C_YELLOW) | curses.A_BOLD)
                return True

    return False


# ── Main TUI loop ─────────────────────────────────────────────────────────────

def _run(stdscr: "curses.window", verbose: bool = False, color_hex: str = "#aaaaaa", font_size: int = 13) -> None:
    """curses main loop — draw, read keys, and dispatch commands until quit."""
    global _waiting, _session_id, _needs_confirm, _is_confirm, _pending_kill, _is_password, _allow_empty, _pending_claim

    curses.curs_set(0)
    stdscr.timeout(100)
    _init_colours(color_hex)

    # Resize terminal and set font size (best-effort; xterm-compatible terminals)
    sys.stdout.write(f"\033]50;xft:Monospace:size={font_size}\007")
    sys.stdout.write("\033[8;44;200t")
    sys.stdout.flush()
    time.sleep(0.12)

    _session_id = _load_session_id() or str(uuid.uuid4())
    _save_session_id(_session_id)

    _add(f"  Connecting to {SERVER_URL}...", _cp(C_DIM))
    _draw(stdscr, "")

    if _is_localhost() and not _server_reachable():
        _add("  Server not running — starting it...", _cp(C_YELLOW))
        _draw(stdscr, "")
        started = _autostart_server(stdscr)
        if started:
            _add("  Server ready.", _cp(C_GREEN))
        else:
            _add("  Could not start server. Check /tmp/gorgon-orchestrator.log", _cp(C_RED))
        _draw(stdscr, "")

    ok = _sync_from_server()

    with _lock:
        _history.clear()

    _add(f"  gorgon  →  {SERVER_URL}", _cp(C_GREEN) | curses.A_BOLD)
    if not ok:
        _add(f"  ⚠ Could not reach server. Check connection.", _cp(C_YELLOW))
    elif _REMOTE_VMS:
        vm_summary = "  ".join(
            ("● " if v.get("status") == "running" else "○ ") + v.get("name", "")
            for v in _REMOTE_VMS
        )
        _add(f"  VMs:  {vm_summary}", _cp(C_DIM))
    if _REMOTE_PROFILES:
        _pnames = ',  '.join(
            str(p) if not isinstance(p, dict) else p.get('name', '')
            for p in _REMOTE_PROFILES[:8]
        )
        _add(f"  Profiles:  {_pnames}", _cp(C_DIM))
    _add("", 0)
    _add('  Type a message or ask the AI anything. Type "help" for shortcuts.', _cp(C_DIM))
    _add("", 0)

    input_buf = ""

    while not _quit.is_set():
        # Drain HTTP response queue
        try:
            result = _resp_q.get_nowait()
            _waiting = False
            if isinstance(result, dict) and "_mission" in result:
                _render_mission_result(result["_mission"])   # locally-run mission, not a /chat reply
            else:
                _process_response(result, verbose)
        except queue.Empty:
            pass  # no response queued this tick — nothing to drain

        _draw(stdscr, input_buf if not _waiting else "")

        if _waiting:
            time.sleep(0.05)
            continue

        try:
            ch = stdscr.get_wch()
        except curses.error:
            continue

        if ch in (3, "\x03"):          # Ctrl-C
            _quit.set()
            break

        if ch in ("\n", "\r", curses.KEY_ENTER):
            cmd = input_buf.strip()
            input_buf = ""
            if not cmd and not _allow_empty:      # empty Enter is a real answer when a wizard field allows blank
                continue

            # Pending claim confirm/reject — the next line is the operator password.
            if _pending_claim:
                action, fact = _pending_claim
                _pending_claim = None
                _is_password = False
                _add("  You: " + "•" * len(cmd), _cp(C_BOLD) | curses.A_BOLD)
                user = _auth_sessions.current_username() if _auth_sessions else None
                if user and _auth_store is not None and _auth_store.verify_password(user, cmd):
                    _apply_claim(action, fact)
                else:
                    _add("  Password incorrect — aborted.", _cp(C_RED))
                continue

            # Pending kill confirmation
            if _pending_kill:
                vm = _pending_kill
                _pending_kill = ""
                _add(f"  You: {cmd}", _cp(C_BOLD) | curses.A_BOLD)
                if cmd.lower() in ("y", "yes"):
                    result = _execute("stop_vm", {"name": vm, "force": True})
                    if result.get("success"):
                        _add(f"  ✓ {vm} force-stopped.", _cp(C_GREEN))
                    else:
                        _add(f"  ✖ {result.get('error', 'failed')}", _cp(C_RED))
                else:
                    _add("  Cancelled.", _cp(C_DIM))
                continue

            _disp = ("•" * len(cmd)) if _is_password else cmd
            _add(f"  You: {_disp}", _cp(C_BOLD) | curses.A_BOLD)

            # Built-in shortcuts
            if not _needs_confirm and _dispatch(cmd, verbose):
                continue

            # Send to AI via HTTP worker thread
            auto_confirm = _is_confirm if _needs_confirm else False
            _needs_confirm = False
            _is_confirm    = False
            _is_password   = False
            _allow_empty   = False
            _waiting = True
            threading.Thread(
                target=_http_worker,
                args=(cmd, auto_confirm, verbose),
                daemon=True,
            ).start()

        elif ch in (curses.KEY_BACKSPACE, "\x7f", 8):
            input_buf = input_buf[:-1]

        elif isinstance(ch, str) and ch.isprintable():
            input_buf += ch


# ── Public entry point ────────────────────────────────────────────────────────

def chat_loop(verbose: bool = False, color_hex: str = "#aaaaaa", font_size: int = 13) -> None:
    """Entry point — run the curses chat client until the user exits.

    Same gate as client/cli/commands.py's _operator_gate_ok and
    orchestrator/ai/cli.py's chat_loop — bare `gorgon` (no args) routes here
    via client_wrapper.py, a THIRD entry point that needed this independently
    (none of the others cover it; see the dual-CLI-dispatch memory note).
    """
    if (_auth_store is not None and _auth_store.operators_exist()
            and _auth_sessions.current_username() is None):
        print("Login required. Run `gorgon login` first.")
        return
    try:
        curses.wrapper(lambda s: _run(s, verbose, color_hex, font_size))
    except KeyboardInterrupt:
        pass  # Ctrl-C — exit the TUI cleanly

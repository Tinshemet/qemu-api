"""
chat_client.py — Remote AI Chat Client

Sends messages to the qemu-api server's /chat endpoint and renders responses
with the same Rich panels used by the server-side interactive loop.

The server owns the AI (Ollama) and the QEMU engine.  This client is a thin
UI that requires no Ollama installation locally.
"""

import json
import os
import sys
import uuid

import requests
from rich.panel import Panel

from shared.display import (
    console,
    _print_banner,
    _render_vm_list,
    _render_status,
    _render_monitor,
    _render_profiles,
    _render_snapshots,
    _render_system,
    _render_vnc_connect,
)

# ── Connection config ─────────────────────────────────────────────────────────
_CFG_PATH  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "connection_config.json")
_CFG       = json.load(open(_CFG_PATH))
SERVER_URL = os.environ.get("SERVER_URL",   _CFG.get("server_url", "http://localhost:8080"))
_TOKEN     = os.environ.get("API_TOKEN",    _CFG.get("token",      ""))
_TIMEOUT   = int(os.environ.get("API_TIMEOUT", _CFG.get("timeout", 120)))
_CA_CERT   = os.environ.get("API_CA_CERT", _CFG.get("ca_cert") or None)
_VERIFY    = (
    False if os.environ.get("API_VERIFY_SSL", "1") == "0"
    else (_CA_CERT or _CFG.get("verify_ssl", True))
)
_HEADERS   = {"Authorization": f"Bearer {_TOKEN}"} if _TOKEN else {}

# ── Session persistence ───────────────────────────────────────────────────────
_SESSION_FILE = os.path.expanduser("~/.qemu_vms/.chat_session_id")

_EXIT_CMDS = {"exit", "quit", "q", "bye"}


def _load_session_id() -> str:
    try:
        return open(_SESSION_FILE).read().strip()
    except FileNotFoundError:
        return ""


def _save_session_id(sid: str):
    os.makedirs(os.path.dirname(_SESSION_FILE), exist_ok=True)
    with open(_SESSION_FILE, "w") as f:
        f.write(sid)


# ── Tool-result renderer ──────────────────────────────────────────────────────

def _try_open_vnc(port: int):
    """Spawn a VNC viewer in the background, trying common clients."""
    import subprocess as _sp
    for viewer in ("vncviewer", "tigervncviewer", "xtigervncviewer", "gvncviewer", "vinagre"):
        try:
            _sp.Popen([viewer, f"localhost:{port}"],
                      stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            return viewer
        except FileNotFoundError:
            continue
    return None


def _render_tool_results(tool_results: list, verbose: bool = False):
    for tr in tool_results:
        tool   = tr.get("tool", "")
        result = tr.get("result", {})
        try:
            if tool == "launch_vm" and result.get("success") and result.get("display") == "vnc":
                port   = result.get("vnc_port", 5900)
                viewer = _try_open_vnc(port)
                if viewer:
                    console.print(Panel(
                        f"[bold green]✓ VNC viewer launched automatically[/bold green]\n\n"
                        f"If the window didn't appear, connect manually:\n"
                        f"  [bold yellow]vncviewer localhost:{port}[/bold yellow]",
                        title=f"[bold]VM Display — localhost:{port}[/bold]",
                        border_style="green",
                    ))
                else:
                    console.print(Panel(
                        f"Connect to the VM display:\n\n"
                        f"  [bold yellow]vncviewer localhost:{port}[/bold yellow]\n\n"
                        f"[dim]Or install a VNC viewer: sudo apt install tigervnc-viewer[/dim]",
                        title=f"[bold]VM Display — localhost:{port}[/bold]",
                        border_style="cyan",
                    ))
            elif tool == "list_vms":
                vms = result if isinstance(result, list) else result.get("vms", [])
                _render_vm_list(vms)
            elif tool == "check_system":
                _render_system(result)
            elif tool == "list_profiles":
                _render_profiles(result if isinstance(result, list) else result.get("profiles", []))
            elif tool in ("vm_status", "monitor_vm"):
                _render_monitor(result)
            elif tool == "list_snapshots":
                _render_snapshots(result)
            elif result.get("vnc_connect_cmd"):
                _render_vnc_connect(console, result)
            elif result.get("setup_cmd"):
                # Stealth guest setup required
                setup_cmd  = result["setup_cmd"]
                vm_name    = result.get("name", "")
                is_windows = setup_cmd.startswith("irm ")
                how_line   = (
                    "Open [bold]PowerShell[/bold] inside the VM and run:"
                    if is_windows else
                    "Open a terminal inside the VM and run (then reboot):"
                )
                console.print(Panel(
                    f"[bold]Stealth guest setup required.[/bold] {how_line}\n\n"
                    f"[cyan]{setup_cmd}[/cyan]\n\n"
                    f"[dim]When done, run:[/dim] [bold]setup-done {vm_name}[/bold]",
                    title="Stealth Setup", border_style="yellow",
                ))
            elif not result.get("success") and result.get("error"):
                console.print(f"[bold red]✖[/bold red] {result['error']}")
            elif verbose:
                console.print(Panel(
                    json.dumps(result, indent=2, default=str),
                    title=f"[dim]{tool}[/dim]",
                    border_style="dim",
                ))
        except Exception:
            pass  # never crash the UI over a renderer error


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _post_chat(message: str, session_id: str, auto_confirm: bool = False, verbose: bool = False) -> dict:
    payload = {
        "message":      message,
        "session_id":   session_id,
        "auto_confirm": auto_confirm,
        "verbose":      verbose,
    }
    try:
        resp = requests.post(
            f"{SERVER_URL}/chat",
            json=payload,
            headers=_HEADERS,
            timeout=_TIMEOUT,
            verify=_VERIFY,
        )
    except requests.ConnectionError:
        console.print(f"\n[bold red]Cannot connect to server at {SERVER_URL}[/bold red]")
        console.print("  → Make sure the server is running: [bold]qemu-api-serve[/bold]")
        sys.exit(1)

    if resp.status_code == 401:
        console.print("[bold red]Server rejected the token (401)[/bold red]")
        console.print("  → Check that API_TOKEN matches on both machines")
        sys.exit(1)

    if not resp.ok:
        return {
            "text":         f"Server error {resp.status_code}: {resp.text}",
            "session_id":   session_id,
            "tool_results": [],
            "needs_input":  None,
        }

    return resp.json()


def _handle_response(result: dict, session_id: str, verbose: bool) -> str:
    """Render a /chat response.  Returns the (possibly updated) session_id."""
    sid = result.get("session_id", session_id)
    _save_session_id(sid)

    _render_tool_results(result.get("tool_results", []), verbose)

    text = result.get("text", "").strip()
    if text:
        console.print(f"\n[bold green]Assistant:[/bold green] {text}\n")

    return sid


# ── Shortcut sets (mirrors server/ai/config.json shortcut_commands) ───────────

_SC_LIST     = {"list", "vms", "show", "show vms", "list vms", "ls", "list all",
                "show all", "list all vms", "show all vms"}
_SC_SYSTEM   = {"system", "system info", "check system", "show system"}
_SC_PROFILES = {"profiles", "list profiles", "show profiles"}
_SC_DRIFT    = {"drift", "check drift", "drift check", "drift status", "drift report"}
_SC_CLEAR    = {"clear session", "session clear", "clear_session", "forget", "/clear"}
_SC_HELP     = {"help", "?", "/help", "commands", "show commands"}


def _execute(tool_name: str, args: dict = {}, verbose: bool = False) -> dict:
    """Call /execute directly — bypasses AI for known shortcut commands."""
    try:
        resp = requests.post(
            f"{SERVER_URL}/execute",
            json={"tool_name": tool_name, "args": args, "verbose": verbose},
            headers=_HEADERS,
            timeout=_TIMEOUT,
            verify=_VERIFY,
        )
        if not resp.ok:
            return {"success": False, "error": f"Server error {resp.status_code}"}
        return resp.json().get("result", {})
    except requests.ConnectionError:
        console.print(f"\n[bold red]Cannot connect to server at {SERVER_URL}[/bold red]")
        return {"success": False, "error": "Connection error"}


def _render_shortcut_result(tool_name: str, result: dict, verbose: bool):
    """Render the result of a shortcut /execute call with a brief ack line."""
    try:
        if tool_name == "list_vms":
            vms = result if isinstance(result, list) else result.get("vms", [])
            console.print(f"\n[dim]VMs ({len(vms)} found):[/dim]")
            _render_vm_list(vms)
        elif tool_name == "check_system":
            console.print("\n[dim]System info:[/dim]")
            _render_system(result)
        elif tool_name == "list_profiles":
            profiles = result if isinstance(result, list) else result.get("profiles", [])
            console.print(f"\n[dim]Profiles ({len(profiles)} found):[/dim]")
            _render_profiles(profiles)
        elif tool_name == "check_drift":
            drift = result if isinstance(result, dict) else {}
            if not drift.get("drifted"):
                console.print("\n[dim green]✓ No drift detected.[/dim green]")
            else:
                console.print(Panel(
                    json.dumps(drift, indent=2, default=str),
                    title="[yellow]Drift detected[/yellow]", border_style="yellow",
                ))
        elif verbose:
            import json as _json
            console.print(Panel(
                _json.dumps(result, indent=2, default=str),
                title=f"[dim]{tool_name}[/dim]", border_style="dim",
            ))
    except Exception:
        pass


# ── Help panel ───────────────────────────────────────────────────────────────

def _render_help():
    console.print(Panel(
        "[bold]Shortcut commands (instant, no AI):[/bold]\n"
        "  [cyan]list[/cyan]  /  [cyan]vms[/cyan]             List all VMs\n"
        "  [cyan]system[/cyan]                  System capabilities\n"
        "  [cyan]profiles[/cyan]                Hardware profiles\n"
        "  [cyan]drift[/cyan]                   Configuration drift check\n"
        "  [cyan]kill <name>[/cyan]             Force-kill a VM (SIGKILL, asks confirm)\n"
        "  [cyan]force stop <name>[/cyan]       Force-kill a VM (SIGKILL, asks confirm)\n"
        "  [cyan]clear session[/cyan]           Clear conversation history\n"
        "  [cyan]help[/cyan]  /  [cyan]?[/cyan]               Show this message\n"
        "  [cyan]exit[/cyan]  /  [cyan]quit[/cyan]  /  [cyan]q[/cyan]     Exit\n\n"
        "[bold]Common AI requests (natural language):[/bold]\n"
        "  \"create a Ubuntu VM called myvm\"\n"
        "  \"create myvm with 8GB RAM and 100GB disk\"\n"
        "  \"launch myvm\"  /  \"start myvm\"\n"
        "  \"stop myvm\"  /  \"shutdown myvm\"\n"
        "  \"delete myvm\"\n"
        "  \"status myvm\"  /  \"monitor myvm\"\n"
        "  \"clone myvm as myvm-copy\"\n"
        "  \"create snapshot of myvm called before-update\"\n"
        "  \"resize myvm to 200GB\"\n"
        "  \"why did myvm fail\"\n\n"
        "[bold]VNC (all server-launched VMs run headless in VNC mode):[/bold]\n"
        "  Connect:  [bold yellow]vncviewer localhost:5900[/bold yellow]   (first VM)\n"
        "            [bold yellow]vncviewer localhost:5901[/bold yellow]   (second VM, etc.)\n"
        "  The exact port is shown after launch, or ask: [cyan]\"status myvm\"[/cyan]\n"
        "  For local-display mode run VMs directly: [dim]qemu-api launch <vm> sdl[/dim]\n\n"
        "[dim]For direct CLI commands (no AI), run: qemu-api help[/dim]",
        title="[bold]qemu-api — help[/bold]",
        border_style="cyan",
    ))


# ── Needs-input renderer ─────────────────────────────────────────────────────

def _render_needs_input(ni_type: str, question: str, opts: list, proposed: str = ""):
    """Render a needs_input prompt with appropriate styling per type."""
    if ni_type == "confirm_critical":
        body = f"[bold red]{question}[/bold red]"
        if proposed:
            body += f"\n[dim]You must type [bold red]{proposed}[/bold red] exactly to confirm.[/dim]"
        console.print(Panel(body, title="[bold red]⚠  Destructive Action[/bold red]",
                            border_style="red"))
    elif ni_type in ("confirm_yn", "confirm_name"):
        body = question
        if opts:
            body += "\n" + "  ".join(f"[bold cyan][{o}][/bold cyan]" for o in opts)
        console.print(Panel(body, title="[yellow]Confirm[/yellow]", border_style="yellow"))
    elif ni_type == "preflight":
        body = question
        if opts:
            body += "\n" + "  ".join(f"[bold cyan][{o}][/bold cyan]" for o in opts)
        console.print(Panel(body, title="[cyan]Pre-flight Check[/cyan]", border_style="cyan"))
    else:
        # clarify
        body = question
        if opts:
            body += "\n" + "  ".join(f"[dim][{o}][/dim]" for o in opts)
        console.print(Panel(body, title="[blue]More info needed[/blue]", border_style="blue"))


# ── Chat loop ─────────────────────────────────────────────────────────────────

def chat_loop(verbose: bool = False):
    # Health check
    try:
        r = requests.get(f"{SERVER_URL}/health", timeout=5, verify=_VERIFY)
        if not r.ok:
            console.print(f"[bold yellow]⚠ Server health check failed ({r.status_code})[/bold yellow]")
    except Exception:
        console.print(f"[bold red]Cannot reach server at {SERVER_URL}[/bold red]")
        console.print("  → Start the server with: [bold]qemu-api-serve[/bold]")
        sys.exit(1)

    # Fetch server info for the banner
    try:
        info_r = requests.get(f"{SERVER_URL}/info", headers=_HEADERS,
                               timeout=5, verify=_VERIFY)
        srv = info_r.json() if info_r.ok else {}
    except Exception:
        srv = {}

    _print_banner(
        verbose        = verbose,
        ollama_model   = srv.get("ollama_model", "unknown"),
        ollama_url     = srv.get("ollama_url",   "unknown"),
        ovmf_available = srv.get("ovmf_available", False),
        ovmf_code      = srv.get("ovmf_code", ""),
        api_url        = SERVER_URL,
    )

    session_id = _load_session_id() or str(uuid.uuid4())
    _save_session_id(session_id)

    while True:
        try:
            user_input = console.input("\n[bold cyan]You:[/bold cyan] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if not user_input:
            continue

        _ui = user_input.lower().strip()

        # ── Exit ──────────────────────────────────────────────────────────
        if _ui in _EXIT_CMDS:
            console.print("[dim]Goodbye.[/dim]")
            break

        # ── Clear session ─────────────────────────────────────────────────
        if _ui in _SC_CLEAR:
            try:
                requests.delete(f"{SERVER_URL}/sessions/{session_id}",
                                headers=_HEADERS, timeout=10, verify=_VERIFY)
            except Exception:
                pass
            session_id = str(uuid.uuid4())
            _save_session_id(session_id)
            console.print("[dim]Session cleared.[/dim]")
            continue

        # ── Shortcuts — bypass AI, call /execute directly ─────────────────
        if _ui in _SC_LIST:
            _render_shortcut_result("list_vms",    _execute("list_vms",    verbose=verbose), verbose)
            continue
        if _ui in _SC_SYSTEM:
            _render_shortcut_result("check_system", _execute("check_system", verbose=verbose), verbose)
            continue
        if _ui in _SC_PROFILES:
            _render_shortcut_result("list_profiles", _execute("list_profiles", verbose=verbose), verbose)
            continue
        if _ui in _SC_DRIFT:
            _render_shortcut_result("check_drift", _execute("check_drift", verbose=verbose), verbose)
            continue
        if _ui in _SC_HELP:
            _render_help()
            continue

        # ── kill <name> shortcut → stop_vm(force=True) ───────────────────
        _kill_match = None
        for _kpfx in ("kill ", "force stop ", "force kill ", "hard stop "):
            if _ui.startswith(_kpfx):
                _kill_match = user_input[len(_kpfx):].strip()
                break
        if _kill_match:
            vm_name = _kill_match
            _render_needs_input("confirm_yn", f"Force-kill (SIGKILL) VM: {vm_name}?", ["Yes", "Cancel"])
            try:
                _kill_ans = console.input("[bold cyan]You:[/bold cyan] ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Cancelled.[/dim]")
                continue
            if _kill_ans in ("y", "yes"):
                _res = _execute("stop_vm", {"name": vm_name, "force": True}, verbose)
                if _res.get("success"):
                    console.print(f"[dim green]✓ {_res.get('message', f'{vm_name} stopped.')}[/dim green]")
                else:
                    console.print(f"[bold red]✖[/bold red] {_res.get('error', 'Failed.')}")
            else:
                console.print("[dim]Cancelled.[/dim]")
            continue

        # ── Send to /chat (AI turn) ───────────────────────────────────────
        result = _post_chat(user_input, session_id, verbose=verbose)
        session_id = _handle_response(result, session_id, verbose)

        # ── Handle needs_input chain (loops for double-confirmation) ─────────
        _ALL_SHORTCUTS = _SC_LIST | _SC_SYSTEM | _SC_PROFILES | _SC_DRIFT | _SC_CLEAR | _SC_HELP
        while result.get("needs_input"):
            ni      = result["needs_input"]
            ni_type = ni.get("type", "clarify")
            question = ni.get("question", "Please confirm:")
            opts     = ni.get("options", [])
            proposed = ni.get("proposed", "")

            _render_needs_input(ni_type, question, opts, proposed)

            try:
                answer = console.input("[bold cyan]You:[/bold cyan] ").strip()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Cancelled.[/dim]")
                break

            _ans_lower = answer.lower().strip()

            # Exit commands cancel the pending action and quit
            if _ans_lower in _EXIT_CMDS:
                console.print("[dim]Goodbye.[/dim]")
                return

            # Shortcut commands run inline then re-ask the question
            if _ans_lower in _ALL_SHORTCUTS:
                if _ans_lower in _SC_LIST:
                    _render_shortcut_result("list_vms", _execute("list_vms", verbose=verbose), verbose)
                elif _ans_lower in _SC_SYSTEM:
                    _render_shortcut_result("check_system", _execute("check_system", verbose=verbose), verbose)
                elif _ans_lower in _SC_PROFILES:
                    _render_shortcut_result("list_profiles", _execute("list_profiles", verbose=verbose), verbose)
                elif _ans_lower in _SC_DRIFT:
                    _render_shortcut_result("check_drift", _execute("check_drift", verbose=verbose), verbose)
                elif _ans_lower in _SC_CLEAR:
                    console.print("[dim]Can't clear session mid-confirmation.[/dim]")
                elif _ans_lower in _SC_HELP:
                    _render_help()
                continue  # re-show the question

            if not answer or _ans_lower in ("cancel", "no", "n"):
                console.print("[dim]Cancelled.[/dim]")
                break

            is_confirm = ni_type in ("confirm_yn", "confirm_name", "confirm_critical", "preflight")
            result = _post_chat(answer, session_id, auto_confirm=is_confirm, verbose=verbose)
            session_id = _handle_response(result, session_id, verbose)

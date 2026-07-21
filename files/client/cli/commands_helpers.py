"""
commands_helpers.py — helpers for the direct client CLI.

The manager guard, remote /execute caller, the -cu/-tf/-cs flag handlers, and
the stealth-setup popup server. Split out of commands.py so run() stays the
file's focus. commands.py re-exports the flag handlers (client_wrapper imports
them) and imports back the two that run() uses.
"""
import json
import os
import socket
import threading

import requests
from rich.panel import Panel

try:
    from shared.display import console
except ImportError:
    from rich.console import Console
    console = Console()
try:
    from shared.executioner.tool_executor import manager
except ImportError:
    manager = None                                                            # type: ignore[assignment]

_CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "connection_config.json")
try:
    _CONN    = json.load(open(_CFG_PATH))
    _SERVER  = os.environ.get("SERVER_URL", _CONN.get("server_url", "http://localhost:8080"))
    _TOKEN   = os.environ.get("API_TOKEN",  _CONN.get("token", ""))
    _TIMEOUT = int(os.environ.get("API_TIMEOUT", _CONN.get("timeout", 120)))
    _CA_CERT = os.environ.get("API_CA_CERT", _CONN.get("ca_cert") or None)
    _VERIFY  = False if os.environ.get("API_VERIFY_SSL", "1") == "0" else (_CA_CERT or _CONN.get("verify_ssl", True))
    _HEADERS = {"Authorization": f"Bearer {_TOKEN}"} if _TOKEN else {}
except Exception:
    _SERVER, _TOKEN, _TIMEOUT, _VERIFY, _HEADERS = "http://localhost:8080", "", 120, True, {}


def _require_manager() -> None:
    """Abort with an install hint when no local QEMU manager is available."""
    if manager is None:
        console.print(
            "[bold yellow]Direct commands require QEMU to be installed on this machine.[/bold yellow]\n"
            "  → Run [bold]setup_client.sh[/bold] to install QEMU, or use the AI chat to manage remote VMs."
        )
        raise SystemExit(1)


def _remote_execute(tool_name: str, args: dict) -> dict:
    """POST a tool call to the configured server's /execute endpoint (no local QEMU needed)."""
    try:
        r = requests.post(
            f"{_SERVER}/execute", headers=_HEADERS,
            json={"tool_name": tool_name, "args": args},
            timeout=_TIMEOUT, verify=_VERIFY,
        )
        if not r.ok:
            try:
                body = r.json()
                msg = body.get("result", {}).get("error") or body.get("detail") or r.text
            except Exception:
                msg = r.text
            return {"success": False, "error": f"Server error {r.status_code}: {msg}"}
        result = r.json().get("result", {})
        return result if isinstance(result, dict) else {"success": True, "value": result}
    except Exception as e:
        return {"success": False, "error": f"Could not reach server: {e}"}


def set_custom_mode_flag(enabled: bool) -> None:
    """-cu — disable product-name verification. Local orchestrator install if
    present, otherwise toggled server-side via POST /custom-mode."""
    try:
        from orchestrator.preflight.validator import set_custom_mode
        set_custom_mode(enabled)
        console.print(f"[dim]Custom mode {'enabled' if enabled else 'disabled'} (local)[/dim]")
        return
    except ImportError:
        pass  # orchestrator not importable here — fall through to the HTTP path below
    if _SERVER and _TOKEN:
        try:
            r = requests.post(f"{_SERVER}/custom-mode", headers=_HEADERS,
                               json={"enabled": enabled}, timeout=_TIMEOUT, verify=_VERIFY)
            if r.ok:
                console.print(f"[dim]Custom mode {'enabled' if enabled else 'disabled'} on server[/dim]")
            else:
                console.print(f"[bold red]Failed to set custom mode on server: {r.status_code}[/bold red]")
        except Exception as e:
            console.print(f"[bold red]Could not reach server to set custom mode: {e}[/bold red]")
    else:
        console.print("[bold yellow]Custom mode requires either a local orchestrator install "
                       "or a configured server connection (SERVER_URL/API_TOKEN).[/bold yellow]")


def fingerprint_report(vm_name: str) -> None:
    """-tf <name> — inxi-style fingerprint report. Local QEMU if present,
    otherwise routed through the configured server's /execute endpoint."""
    if manager is not None:
        from executor.fingerprint import tf_report
        result = tf_report(vm_name)
        console.print(result.get("report") or result.get("error") or result)
        return
    if _SERVER and _TOKEN:
        result = _remote_execute("fingerprint_vm", {"name": vm_name})
        console.print(result.get("report") or result.get("error") or result)
    else:
        console.print("[bold yellow]Fingerprint report requires either local QEMU "
                       "or a configured server connection (SERVER_URL/API_TOKEN).[/bold yellow]")


def clear_session_flag() -> None:
    """-cs — clear the saved chat session before starting (local file; server
    session is naturally abandoned since a fresh session_id will be generated)."""
    session_file = os.path.expanduser("~/.qemu_vms/.chat_session_id")
    if os.path.exists(session_file):
        os.remove(session_file)
    console.print("[dim]Session cleared.[/dim]")


def _show_stealth_popup(vm_name: str, setup_cmd: str) -> None:
    """Serve the stealth setup script via a one-shot HTTP server so the VM can pull it."""
    script_path = None
    if manager is not None:   # None in remote mode — no local manager to generate against
        try:
            r = manager.generate_guest_setup(vm_name)
            if r.get("success"):
                script_path = r["path"]
        except Exception as e:
            # a genuine generation failure — surface it instead of swallowing
            console.print(f"[dim]guest setup generation failed: {e}[/dim]")
    if not script_path:
        return

    script_dir  = os.path.dirname(script_path)
    script_file = os.path.basename(script_path)

    with socket.socket() as s:
        s.bind(("", 0))
        port = s.getsockname()[1]

    import http.server
    class _Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_) -> None:
            """Silence the default stderr request logging."""

    def _serve() -> None:
        """Serve exactly one request, then shut the one-shot script server down."""
        srv = http.server.HTTPServer(("", port), _Handler)
        srv.handle_request()
        srv.server_close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()

    try:
        host_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        host_ip = "127.0.0.1"

    fetch_url = f"http://{host_ip}:{port}/{script_file}"
    console.print(Panel(
        f"[bold]Or fetch the script directly from the VM:[/bold]\n\n"
        f"[cyan]{fetch_url}[/cyan]\n\n"
        f"[dim]This URL is valid for one download.[/dim]",
        title="Script Server", border_style="dim",
    ))

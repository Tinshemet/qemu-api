"""
commands.py — Direct QEMU CLI (client-local)

Dispatches sub-commands directly to the local QEMU engine via
shared.executioner.tool_executor.  No AI or network call involved.

Used when the client machine has QEMU installed and the user wants to
manage VMs directly rather than through the AI chat interface.

Usage (via client_wrapper.py):
    gorgon list
    gorgon launch <vm> [sdl|vnc]
    gorgon stop <vm>
    gorgon status <vm>
    gorgon snapshot list|create|restore|delete <vm> [tag]
    gorgon clone <src> <dst>
    gorgon delete <vm>
    gorgon resize <vm> <gb>
    gorgon config <vm>
    gorgon profiles
    gorgon system
    gorgon isos
    gorgon show-cmd <vm>
    gorgon setup-done <vm>
"""

import getpass
import json
import os
import socket
import threading
from typing import List, Optional

import requests

try:
    # Same defensive-import reasoning as the `manager` import below — this
    # module runs on client-only checkouts too, where orchestrator/ may be
    # absent. See _operator_gate_ok(): unavailable means "degrade open",
    # matching manager's own None-and-skip fallback below.
    from orchestrator.auth import store as _auth_store, sessions as _auth_sessions
except ImportError:
    _auth_store    = None                                          # type: ignore[assignment]
    _auth_sessions = None                                          # type: ignore[assignment]

from rich import box
from rich.panel import Panel
from rich.table import Table

try:
    # shared/ isn't part of a true client-only checkout (see README's client
    # sparse checkout) — fall back to plain rich output instead of crashing
    # the whole direct-CLI module (even "gorgon help" needs this import).
    from shared.display import (
        console,
        render_vm_list,
        render_status,
        render_monitor,
        render_profiles,
        render_templates,
        render_compat,
        render_snapshots,
        render_system,
        render_fleet,
        render_fleets,
    )
except ImportError:
    from rich.console import Console
    console = Console()

    def _render_json(data: object, *_a, **_kw) -> None:
        """Fallback renderer — dump JSON when shared.display isn't importable."""
        console.print_json(data=data, default=str)

    render_vm_list   = _render_json
    render_status    = _render_json
    render_monitor    = _render_json
    render_profiles  = _render_json
    render_templates = _render_json
    render_compat    = _render_json
    render_snapshots = _render_json
    render_system    = _render_json
    render_fleet     = _render_json
    render_fleets    = _render_json

_CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "connection_config.json")
try:
    _CONN      = json.load(open(_CFG_PATH))
    _SERVER    = os.environ.get("SERVER_URL", _CONN.get("server_url", "http://localhost:8080"))
    _TOKEN     = os.environ.get("API_TOKEN",  _CONN.get("token", ""))
    _TIMEOUT   = int(os.environ.get("API_TIMEOUT", _CONN.get("timeout", 120)))
    _CA_CERT   = os.environ.get("API_CA_CERT", _CONN.get("ca_cert") or None)
    _VERIFY    = False if os.environ.get("API_VERIFY_SSL", "1") == "0" else (_CA_CERT or _CONN.get("verify_ssl", True))
    _HEADERS   = {"Authorization": f"Bearer {_TOKEN}"} if _TOKEN else {}
    _VNC_VIEWERS = _CONN.get("vnc_viewer_candidates", ["vncviewer", "tigervnc", "xtigervncviewer"])
    _IO_CHUNK    = _CONN.get("io_chunk_bytes", 4 * 1024 * 1024)
except Exception:
    _SERVER, _TOKEN, _TIMEOUT, _VERIFY, _HEADERS = "http://localhost:8080", "", 120, True, {}
    _VNC_VIEWERS, _IO_CHUNK = [], 4 * 1024 * 1024

try:
    from executor.api.qemu_config import (
        OVMF,
        check_profile_compatibility,
        check_system_capabilities,
        list_profiles,
    )
except ImportError:
    OVMF = {"available": False}
    # Fallbacks when the executor package isn't in a client-only checkout —
    # return empty data so the direct CLI still imports and runs (server path).
    def list_profiles() -> list:                          # type: ignore[misc]
        """No local profiles when the executor package is absent."""
        return []
    def check_profile_compatibility(*a, **kw) -> dict:    # type: ignore[misc]
        """No local compat data when the executor package is absent."""
        return {}
    def check_system_capabilities() -> dict:              # type: ignore[misc]
        """No local capability data when the executor package is absent."""
        return {}

try:
    from shared.executioner.tool_executor import manager
except ImportError:
    manager = None                                               # type: ignore[assignment]

from .commands_helpers import (  # helpers (extracted from this file)
    _require_manager, _show_stealth_popup,     # used by run()
    set_custom_mode_flag, fingerprint_report, clear_session_flag,  # re-exported for client_wrapper
)


def _allowed_tools() -> Optional[set]:
    """Return the executor's allowed-tools set for help filtering, or None if unrestricted."""
    try:
        from orchestrator.executor_client import _ALLOWED_TOOLS
        return set(_ALLOWED_TOOLS) or None
    except Exception:
        return None


# login/logout bypass the gate itself; everything else — including
# "operator" management — is held to it. Mirrors orchestrator/ai/direct_cli.py's
# _operator_gate_ok exactly: this file is the OTHER in-process, unauthenticated-
# by-default path to `manager` (client_wrapper.py's `gorgon <cmd>` uses THIS
# module, not orchestrator/ai/direct_cli.py — both needed the same gate).
_AUTH_EXEMPT_COMMANDS = {"login", "logout"}


def _operator_gate_ok(cmd: str) -> bool:
    """True if cmd may dispatch: the auth package isn't available (pure
    client-only checkout — degrade open, same philosophy as `manager`'s own
    None-and-skip fallback above), no operator accounts exist yet
    (pre-bootstrap, identical to legacy behavior), or this box holds a
    valid, unexpired login."""
    if _auth_store is None:
        return True
    if cmd in _AUTH_EXEMPT_COMMANDS:
        return True
    if not _auth_store.operators_exist():
        return True
    return _auth_sessions.current_username() is not None


def _require_operator_password(action: str) -> bool:
    """Re-authenticate the operator for a HIGH-IMPACT change (forging/signing a
    contract, switching the active agent). Stronger than _operator_gate_ok: an
    active session isn't enough — the operator must re-enter their password, so a
    walk-up to an unlocked terminal can't reassign contracts or agents.

    Degrades open only where auth genuinely can't apply: no auth package (client-
    only checkout) or pre-bootstrap (no operators yet). Otherwise it needs a
    logged-in operator AND a correct password. Returns True to proceed.
    """
    if _auth_store is None or not _auth_store.operators_exist():
        return True
    user = _auth_sessions.current_username()
    if not user:
        console.print("[bold red]Login required.[/bold red] Run [cyan]gorgon login[/cyan] first.")
        return False
    pw = getpass.getpass(f"Operator password to {action}: ")
    if _auth_store.verify_password(user, pw):
        return True
    console.print("[bold red]Password incorrect — aborted.[/bold red]")
    return False


def run(args: List[str], verbose: bool = False) -> None:
    """Dispatch a direct ``gorgon <cmd>`` sub-command.

    Routes the first arg (list / launch / stop / status / snapshot / clone /
    delete / resize / config / profiles / templates / system / isos / show-cmd / setup-done)
    to the local QEMU manager, or to the configured server when QEMU isn't
    installed locally. ``verbose`` echoes the raw JSON result for each call.

    Example::

        run(["list"])                 # renders the VM table
        run(["launch", "myvm", "vnc"])
    """
    if not args:
        console.print("[dim]No command given. Try: list, launch, stop, status, profiles, system[/dim]")
        return

    cmd  = args[0]
    rest = args[1:]

    if not _operator_gate_ok(cmd):
        console.print("[bold red]Login required.[/bold red] Run [cyan]gorgon login[/cyan] first.")
        return

    def pp(data: object) -> None:
        """Echo the raw JSON result when running in verbose mode."""
        if verbose:
            console.print_json(json.dumps(data, default=str))

    if cmd == "list":
        _require_manager()
        vms = manager.list_vms()
        render_vm_list(vms)
        pp(vms)

    elif cmd == "status" and rest:
        _require_manager()
        r = manager.vm_status(rest[0])
        render_status(r)
        pp(r)

    elif cmd == "monitor":
        _require_manager()
        name = rest[0] if rest else "all"
        r    = manager.monitor_all() if name == "all" else manager.monitor_vm(name)
        if isinstance(r, dict) and "state" in r:
            render_monitor(r)
        else:
            for v in (r.values() if isinstance(r, dict) else [r]):
                render_monitor(v)
        pp(r)

    elif cmd == "launch" and rest:
        _require_manager()
        vm_name = rest[0]
        display = rest[1] if len(rest) > 1 else None

        # If the VM exists locally, check whether it also exists on the server
        local_exists = any(v.get("name") == vm_name for v in manager.list_vms())
        remote_exists = False
        if local_exists and _SERVER and _TOKEN:
            try:
                resp = requests.post(f"{_SERVER}/execute", headers=_HEADERS,
                                     json={"tool_name": "list_vms", "args": {}},
                                     timeout=10, verify=_VERIFY)
                if resp.ok:
                    remote_result = resp.json().get("result", [])
                    remote_vms = remote_result if isinstance(remote_result, list) else remote_result.get("vms", [])
                    remote_exists = any(v.get("name") == vm_name for v in remote_vms)
            except Exception:
                pass  # remote existence check is best-effort — a network error just skips remote dedupe

        use_remote = False
        if local_exists and remote_exists:
            console.print(
                f"\n  [bold yellow]'{vm_name}' exists both locally and on the remote server.[/bold yellow]\n"
                f"  [1] Local  (this machine)\n"
                f"  [2] Remote ({_SERVER})\n"
            )
            choice = input("  Launch which? [1/2]: ").strip()
            use_remote = choice == "2"

        if use_remote:
            try:
                resp = requests.post(f"{_SERVER}/execute", headers=_HEADERS,
                                     json={"tool_name": "launch_vm", "args": {"name": vm_name, "display": display or "vnc"}},
                                     timeout=_TIMEOUT, verify=_VERIFY)
                r = resp.json().get("result", {}) if resp.ok else {"error": resp.text}
            except Exception as e:
                r = {"error": str(e)}
        else:
            r = manager.launch_vm(vm_name, display=display)
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")
        if r.get("display") == "vnc" and (r.get("success") or r.get("already_running")):
            port = r.get("vnc_port", 5900)
            opened = None
            _fallback = ("vncviewer", "tigervncviewer", "xtigervncviewer", "gvncviewer", "vinagre")
            for _viewer in (_VNC_VIEWERS or _fallback):
                try:
                    import subprocess as _sp
                    _sp.Popen([_viewer, f"localhost:{port}"], stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                    opened = _viewer
                    break
                except FileNotFoundError:
                    continue
            if opened:
                console.print(Panel(
                    f"[bold green]✓ VNC viewer launched automatically[/bold green]\n\n"
                    f"If the window didn't appear:\n"
                    f"  [bold yellow]vncviewer localhost:{port}[/bold yellow]",
                    title=f"[bold]VM Display — localhost:{port}[/bold]", border_style="green",
                ))
            else:
                console.print(Panel(
                    f"Connect to the VM display:\n\n"
                    f"  [bold yellow]vncviewer localhost:{port}[/bold yellow]\n\n"
                    f"[dim]Install a viewer: sudo apt install tigervnc-viewer[/dim]",
                    title=f"[bold]VM Display — localhost:{port}[/bold]", border_style="cyan",
                ))
        if r.get("setup_cmd"):
            setup_cmd  = r["setup_cmd"]
            is_windows = setup_cmd.startswith("irm ")
            how_line   = (
                "Open [bold]PowerShell[/bold] inside the VM and run:"
                if is_windows else
                "Open a terminal inside the VM and run (then reboot):"
            )
            console.print(Panel(
                f"[bold]Stealth guest setup required.[/bold] {how_line}\n\n"
                f"[cyan]{setup_cmd}[/cyan]\n\n"
                f"[dim]When done, run:[/dim] [bold]gorgon setup-done {rest[0]}[/bold]",
                title="Stealth Setup", border_style="yellow",
            ))
            _show_stealth_popup(rest[0], setup_cmd)
        pp(r)

    elif cmd == "stop" and rest:
        _require_manager()
        r     = manager.stop_vm(rest[0])
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")
        pp(r)

    elif cmd == "config" and rest:
        _require_manager()
        r = manager.show_config(rest[0])
        if r.get("success"):
            console.print_json(json.dumps(r["config"], default=str))
        else:
            console.print(f"[error]{r['error']}[/error]")

    elif cmd == "resize" and len(rest) >= 2:
        _require_manager()
        r     = manager.resize_disk(rest[0], 0, int(rest[1]))
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

    elif cmd == "clone" and len(rest) >= 2:
        _require_manager()
        r     = manager.clone_vm(rest[0], rest[1])
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

    elif cmd == "snapshot" and len(rest) >= 2:
        _require_manager()
        sub = rest[0]
        if sub == "list" and len(rest) >= 2:
            r = manager.snapshot_list(rest[1])
            render_snapshots(r)
        elif sub == "create" and len(rest) >= 3:
            r = manager.snapshot_create(rest[1], rest[2])
            style = "success" if r.get("success") else "error"
            console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")
        elif sub == "restore" and len(rest) >= 3:
            r = manager.snapshot_restore(rest[1], rest[2])
            style = "success" if r.get("success") else "error"
            console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")
        elif sub == "delete" and len(rest) >= 3:
            r = manager.snapshot_delete(rest[1], rest[2])
            style = "success" if r.get("success") else "error"
            console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")
        else:
            console.print("[dim]Usage: snapshot list|create|restore|delete <vm> [tag][/dim]")

    elif cmd == "delete" and rest:
        _require_manager()
        confirm = console.input(
            f"[bold yellow]⚠ Delete '{rest[0]}'? This cannot be undone.[/bold yellow] [y/N]: "
        ).strip().lower()
        if confirm in ("y", "yes"):
            r     = manager.delete_vm(rest[0])
            style = "success" if r.get("success") else "error"
            console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")
        else:
            console.print("[dim]Cancelled.[/dim]")

    elif cmd == "network":
        _require_manager()
        sub = rest[0] if rest else "list"
        if sub == "list":
            console.print_json(json.dumps(manager.list_networks(), default=str))
        elif sub == "create" and len(rest) >= 2:
            console.print_json(json.dumps(manager.create_network(rest[1]), default=str))
        elif sub == "delete" and len(rest) >= 2:
            console.print_json(json.dumps(manager.delete_network(rest[1]), default=str))
        elif sub == "add" and len(rest) >= 3:
            console.print_json(json.dumps(manager.add_vm_to_network(rest[1], rest[2]), default=str))
        else:
            console.print("[dim]Usage: network list|create|delete|add [name] [vm][/dim]")

    elif cmd == "profiles":
        render_profiles(list_profiles())

    elif cmd == "templates":
        _require_manager()
        render_templates(manager.list_templates())

    elif cmd == "check-profile" and rest:
        render_compat(check_profile_compatibility(rest[0]))

    elif cmd == "system":
        caps = check_system_capabilities()
        caps["ovmf_paths"] = OVMF
        render_system(caps)

    elif cmd == "isos":
        _require_manager()
        isos = manager.scan_isos()
        if isos:
            t = Table(box=box.ROUNDED, border_style="cyan")
            t.add_column("File")
            t.add_column("Size")
            t.add_column("Path", style="dim")
            for iso in isos:
                t.add_row(iso["name"], f"{iso['size_gb']}GB", iso["path"])
            console.print(t)
        else:
            console.print("[bold yellow]No ISOs found in common locations.[/bold yellow]")

    elif cmd == "show-cmd" and rest:
        _require_manager()
        r = manager.print_command(rest[0])
        if r.get("success"):
            console.print(Panel(r["command"], title="QEMU Command", border_style="cyan"))
        else:
            console.print(f"[error]{r.get('error', 'Unknown error')}[/error]")

    elif cmd == "setup-done" and rest:
        _require_manager()
        r     = manager.mark_stealth_done(rest[0])
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

    elif cmd == "guest-setup" and rest:
        _require_manager()
        r = manager.generate_guest_setup(rest[0])
        if not r.get("success"):
            console.print(f"[error]{r['error']}[/error]")
            return
        console.print(Panel(
            f"[bold]Script generated:[/bold] {r['path']}\n\n"
            f"[dim]Copy this to the VM and run it, or use:[/dim]\n"
            f"[bold]gorgon launch {rest[0]}[/bold]  (auto-serves on first boot)",
            title="Guest Setup", border_style="cyan",
        ))
        _show_stealth_popup(rest[0], r.get("setup_cmd", ""))

    elif cmd == "fetch" and rest:
        vm_name = rest[0]
        dest    = rest[1] if len(rest) > 1 else os.path.join(os.getcwd(), f"{vm_name}.qcow2")

        # Get size + checksum first
        try:
            meta = requests.get(
                f"{_SERVER}/images/{vm_name}/sha256",
                headers=_HEADERS, timeout=60, verify=_VERIFY,
            )
            if not meta.ok:
                console.print(f"[bold red]Server error {meta.status_code}:[/bold red] {meta.text}")
                return
            m = meta.json()
            expected_sha256 = m["sha256"]
            size_bytes = m["size_bytes"]
        except requests.ConnectionError:
            console.print(f"[bold red]Cannot reach server at {_SERVER}[/bold red]")
            return

        size_mb = size_bytes / (1024 * 1024)
        console.print(
            f"  Fetching [bold]{vm_name}[/bold] → [dim]{dest}[/dim]\n"
            f"  Size: {size_mb:.1f} MB  |  SHA256: [dim]{expected_sha256[:16]}…[/dim]"
        )

        # Stream download with progress
        import hashlib
        h = hashlib.sha256()
        downloaded = 0
        try:
            with requests.get(
                f"{_SERVER}/images/{vm_name}",
                headers=_HEADERS, stream=True,
                timeout=_TIMEOUT, verify=_VERIFY,
            ) as resp:
                if not resp.ok:
                    console.print(f"[bold red]Download failed {resp.status_code}[/bold red]")
                    return
                os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
                with open(dest, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=_IO_CHUNK):
                        if chunk:
                            f.write(chunk)
                            h.update(chunk)
                            downloaded += len(chunk)
                            pct = downloaded / size_bytes * 100 if size_bytes else 0
                            console.print(
                                f"  [dim]{pct:5.1f}%  {downloaded // (1024*1024)} / {int(size_mb)} MB[/dim]",
                                end="\r",
                            )
        except requests.ConnectionError:
            console.print(f"\n[bold red]Connection lost during download.[/bold red]")
            return

        actual = h.hexdigest()
        if actual != expected_sha256:
            console.print(
                f"\n[bold red]✖ Checksum mismatch![/bold red]\n"
                f"  Expected: {expected_sha256}\n"
                f"  Got:      {actual}\n"
                f"  File may be corrupt — delete it and retry."
            )
        else:
            console.print(
                f"\n[bold green]✓ {vm_name}.qcow2 downloaded[/bold green]  "
                f"({size_mb:.1f} MB, checksum verified)\n"
                f"  Saved to: {dest}"
            )

    elif cmd == "bundle" and rest:
        vm_name = rest[0]
        dest_dir = os.path.expanduser(rest[1]) if len(rest) > 1 else os.path.expanduser("~/.qemu_vms")
        dest_file = os.path.join(dest_dir, f"{vm_name}.tar.gz")

        console.print(f"  Fetching VM bundle [bold]{vm_name}[/bold] → [dim]{dest_file}[/dim]")
        os.makedirs(dest_dir, exist_ok=True)
        try:
            with requests.get(
                f"{_SERVER}/vms/{vm_name}/bundle",
                headers=_HEADERS, stream=True,
                timeout=_TIMEOUT, verify=_VERIFY,
            ) as resp:
                if not resp.ok:
                    console.print(f"[bold red]Server error {resp.status_code}:[/bold red] {resp.text}")
                    return
                downloaded = 0
                with open(dest_file, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=_IO_CHUNK):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            console.print(f"  [dim]{downloaded // (1024*1024)} MB downloaded...[/dim]", end="\r")
        except requests.ConnectionError:
            console.print(f"\n[bold red]Connection lost during download.[/bold red]")
            return

        # Extract into dest_dir
        import tarfile as _tar
        console.print(f"\n  Extracting to {dest_dir}...")
        with _tar.open(dest_file, "r:gz") as t:
            t.extractall(dest_dir, filter="data")
        os.remove(dest_file)

        # Fix absolute paths in config.json to match new location
        cfg_path = os.path.join(dest_dir, vm_name, "config.json")
        if os.path.exists(cfg_path):
            import json as _json
            with open(cfg_path) as f:
                cfg = _json.load(f)
            cfg_str = _json.dumps(cfg)
            # Replace old home path with current home
            import re as _re
            cfg_str = _re.sub(r"/home/[^/]+/\.qemu_vms", dest_dir.rstrip("/"), cfg_str)
            with open(cfg_path, "w") as f:
                f.write(cfg_str)

        console.print(f"[bold green]✓ {vm_name} bundle extracted to {dest_dir}/{vm_name}[/bold green]")

    elif cmd == "label":
        # label add <vm> <label> · label remove <vm> <label> · label list
        _require_manager()
        sub = rest[0] if rest else None
        if sub in ("add", "remove") and len(rest) >= 3:
            r = (manager.add_label if sub == "add" else manager.remove_label)(rest[1], rest[2])
            style = "green" if r.get("success") else "red"
            console.print(f"[bold {style}]{r.get('message', r.get('error', 'unknown error'))}[/bold {style}]")
            pp(r)
        elif sub == "list":
            render_fleets(manager.list_labels().get("usage", {}))
        else:
            console.print("[bold red]Usage: gorgon label add|remove <vm> <label>  |  "
                          "gorgon label list[/bold red]")

    elif cmd == "fleet":
        # fleet                       → list current fleets (labels → member VMs)
        # fleet <label>               → preview members of one fleet
        # fleet <label> exec <cmd...> → run a command on every member
        # fleet <label> stop|launch|ping|status → broadcast that action
        _require_manager()
        if not rest:
            render_fleets(manager.list_labels().get("usage", {}))
            return
        label  = rest[0]
        action = rest[1] if len(rest) > 1 else None
        if action is None:
            r = manager.fleet(label, "status")
            if r.get("results"):
                render_fleet(r)
            else:
                console.print(f"[yellow]{r.get('error', 'No members.')}[/yellow]")
            return
        if action == "exec":
            if len(rest) < 3:
                console.print("[bold red]Usage: gorgon fleet <label> exec <command>[/bold red]")
                return
            r = manager.fleet(label, "exec", command=" ".join(rest[2:]))
        elif action in ("ping", "status", "stop", "launch"):
            r = manager.fleet(label, action)
        else:
            console.print(f"[bold red]Unknown fleet action '{action}'. "
                          f"Use: exec, ping, status, stop, launch.[/bold red]")
            return
        render_fleet(r)
        pp(r)

    elif cmd == "login":
        if _auth_store is None:
            console.print("[bold red]Auth package unavailable on this checkout.[/bold red]")
            return
        username = rest[0] if rest else None
        if not _auth_store.operators_exist():
            console.print("[bold cyan]No operator account exists yet — creating the first one.[/bold cyan]")
            username = username or console.input("Username: ").strip()
            while True:
                pw1 = getpass.getpass("Password: ")
                pw2 = getpass.getpass("Confirm password: ")
                if pw1 != pw2:
                    console.print("[red]Passwords didn't match — try again.[/red]")
                    continue
                if len(pw1) < 8:
                    console.print("[red]Password must be at least 8 characters.[/red]")
                    continue
                break
            r = _auth_store.create_operator(username, pw1)
            if not r.get("success"):
                console.print(f"[bold red]{r.get('error')}[/bold red]")
                return
            password = pw1
        else:
            username = username or console.input("Username: ").strip()
            password = getpass.getpass("Password: ")
        if not _auth_store.verify_password(username, password):
            console.print("[bold red]Invalid username or password.[/bold red]")
            return
        token = _auth_sessions.create_session(username)
        _auth_sessions.write_current_session(token)
        console.print(f"[bold green]Logged in as '{username}'.[/bold green]")

    elif cmd == "logout":
        if _auth_sessions is not None:
            _auth_sessions.invalidate_session(_auth_sessions.read_current_session())
            _auth_sessions.clear_current_session()
        console.print("[dim]Logged out.[/dim]")

    elif cmd == "operator" and rest:
        if _auth_store is None:
            console.print("[bold red]Auth package unavailable on this checkout.[/bold red]")
            return
        sub = rest[0]
        if sub == "add" and len(rest) >= 2:
            pw = getpass.getpass("Password: ")
            r  = _auth_store.create_operator(rest[1], pw)
            console.print(f"[green]Operator '{rest[1]}' created.[/green]" if r.get("success")
                          else f"[bold red]{r.get('error')}[/bold red]")
        elif sub == "list":
            for u in _auth_store.list_operators():
                console.print(f"  {u}")
        elif sub == "remove" and len(rest) >= 2:
            r = _auth_store.delete_operator(rest[1])
            console.print(f"[green]Operator '{rest[1]}' removed.[/green]" if r.get("success")
                          else f"[bold red]{r.get('error')}[/bold red]")
        else:
            console.print("[yellow]Usage: gorgon operator add|list|remove <username>[/yellow]")

    elif cmd == "contract":
        # gorgon contract forge [--full] | show <file> | sign <file> <safeword>
        # Forging is a deliberate, coherence-gated CLI act. The plain `forge`
        # asks only the essential fields (name / goal / toolkit / done-when) and
        # defaults the rest; `--full` walks every field in forge_fields.json.
        try:
            from orchestrator.ai import forge as _forge
        except ImportError:
            console.print("[bold red]Contract forging unavailable on this checkout "
                          "(orchestrator package not present).[/bold red]")
            return
        _agent_dir = os.path.dirname(os.path.abspath(_forge.__file__))
        sub = rest[0] if rest else ""
        if sub == "forge":
            if not _require_operator_password("forge a contract"):
                return
            _full = "--full" in rest
            _forge.forge_interactive(
                ask=lambda p: console.input(f"[bold cyan]{p}:[/bold cyan] ").strip(),
                out=console.print, write_dir=_agent_dir, essential_only=not _full)
        elif sub == "show" and len(rest) >= 2:
            path = rest[1] if os.path.isabs(rest[1]) else os.path.join(_agent_dir, rest[1])
            console.print(_forge.render(json.load(open(path))))
        elif sub == "sign" and len(rest) >= 3:
            if not _require_operator_password("sign a contract"):
                return
            path = rest[1] if os.path.isabs(rest[1]) else os.path.join(_agent_dir, rest[1])
            g = json.load(open(path))
            try:
                _forge.sign(g, rest[2]); _forge.write_grgn(g, path)
                console.print(f"[green]Signed → {path}[/green]")
            except ValueError as e:
                console.print(f"[bold red]{e}[/bold red]")
        else:
            console.print("[yellow]Usage: gorgon contract forge [--full] | "
                          "show <file> | sign <file> <safeword>[/yellow]")

    elif cmd == "agent":
        # gorgon agent | agent <file> | agent load <file> | agent reset
        # Switching the active agent is HIGH-IMPACT: it swaps the whole contract
        # (persona, toolkit, red lines, kill-switch). Guarded by (1) the active
        # contract's blacklist — an agent under a locked contract can't switch
        # itself out — and (2) operator re-authentication. The client never
        # restarts the server; a change takes effect when the operator reboots it.
        import glob as _glob
        from shared import agent_select as _sel
        from orchestrator.ai import forge as _forge
        _agent_dir = os.path.dirname(os.path.abspath(_forge.__file__))
        _resolve  = lambda f: f if os.path.isabs(f) else os.path.join(_agent_dir, f)

        def _validate(f: str):
            p = _resolve(f)
            if not os.path.isfile(p):
                return f"no such agent file: {f}"
            try:
                g = json.load(open(p))
            except Exception as e:
                return f"{f} is not valid JSON: {e}"
            if not (isinstance(g, dict) and "contract" in g and "persona" in g):
                return f"{f} is not a .grgn agent (missing contract/persona)"
            return None

        def _change_allowed() -> bool:
            # (1) the active contract may forbid agent-switching entirely
            try:
                from orchestrator.ai.contract import is_forbidden
                if is_forbidden("switch_agent"):
                    console.print("[bold red]The active contract forbids switching agents "
                                  "(switch_agent is blacklisted).[/bold red]")
                    return False
            except Exception:
                pass  # contract layer unavailable — fall through to the auth gate
            # (2) operator re-authentication
            return _require_operator_password("switch the active agent")

        def _persist(f: str) -> None:
            _sel.set_selection(f if os.path.isabs(f) else os.path.basename(_resolve(f)))

        sub = rest[0] if rest else ""
        if not sub:
            cur = os.environ.get("GORGON_AGENT") or _sel.get_selection() or "doorman.grgn (default)"
            files = sorted(os.path.basename(p) for p in _glob.glob(os.path.join(_agent_dir, "*.grgn")))
            console.print(f"[bold]Active agent:[/bold] {cur}")
            console.print("[dim]Available:[/dim] " + (", ".join(files) or "(none)"))
            if os.environ.get("GORGON_AGENT"):
                console.print("[dim](GORGON_AGENT env var is set — it overrides the saved selection.)[/dim]")
        elif sub == "reset":
            if not _change_allowed():
                return
            _sel.clear_selection()
            console.print("[green]Agent reset — doorman.grgn on next server boot.[/green]")
            console.print("[yellow]Restart the orchestrator server to apply.[/yellow]")
        elif sub == "load":
            if len(rest) < 2:
                console.print("[yellow]Usage: gorgon agent load <file>[/yellow]")
                return
            f = rest[1]
            err = _validate(f)
            if err:
                console.print(f"[bold red]{err}[/bold red]")
                return
            if not _change_allowed():
                return
            _persist(f)
            # Operator access is required to reach here, so the client is allowed
            # to bounce the server — the respawn re-imports the contract and picks
            # up the new selection.
            try:
                _persona = (json.load(open(_resolve(f))).get("persona") or {}).get("name")
            except Exception:
                _persona = None
            _label = _persona or os.path.basename(_resolve(f))
            console.print(f"\n[bold cyan]Loading agent “{_label}”[/bold cyan] … "
                          "restarting the orchestrator server.")
            from shared import server_control as _srv
            pid = _srv.restart_server()
            if pid:
                console.print(f"[green]✔ Server back up (pid {pid}) — “{_label}” is now the active agent.[/green]")
                # Surface any load-time drift for the freshly-loaded agent.
                try:
                    _info = requests.get(f"{_SERVER}/info", headers=_HEADERS,
                                         timeout=10, verify=_VERIFY).json()
                    for _w in _info.get("agent_warnings", []):
                        console.print(f"[yellow]  ⚠ {_w}[/yellow]")
                except Exception:
                    pass  # server still settling — warnings are also in the server log
                console.print("[dim]Reopen the CLI in a few seconds to reconnect.[/dim]")
            else:
                console.print("[bold red]✖ Server did not come back up — check "
                              f"{os.environ.get('GORGON_SERVER_LOG', '/tmp/gorgon-orchestrator.log')}.[/bold red]")
                console.print("[dim]The selection is saved; start the server manually to apply it.[/dim]")
        elif sub not in ("load", "reset"):
            f = sub
            err = _validate(f)
            if err:
                console.print(f"[bold red]{err}[/bold red]")
                return
            if not _change_allowed():
                return
            _persist(f)
            console.print(f"[green]Agent set to {f} — active on next server boot.[/green] "
                          f"Run [cyan]gorgon agent load {f}[/cyan] for the apply-now steps.")
        else:
            console.print("[yellow]Usage: gorgon agent | agent <file> | agent load <file> | agent reset[/yellow]")

    elif cmd in ("help", "--help", "-h"):
        from shared.command_help import load_local_catalog, render_terminal_panel
        catalog, order = load_local_catalog()
        if catalog is None:
            console.print("[dim]Command list unavailable — the executor package "
                          "could not be loaded.[/dim]")
        else:
            body = render_terminal_panel(catalog, _allowed_tools(), order)
            body += (
                "\n\n[bold cyan]Flags[/bold cyan]\n"
                "  -v                             Verbose / raw JSON output\n"
                "  -cu                            Custom mode: skip product verification\n"
                "  -cs                            Clear the saved session first"
            )
            console.print(Panel(body, title="gorgon help", border_style="cyan"))

    else:
        console.print(
            f"[bold yellow]Unknown command: {cmd}[/bold yellow]  "
            f"Run [bold]gorgon help[/bold] for usage."
        )

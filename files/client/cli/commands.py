"""
commands.py — Direct QEMU CLI (client-local)

Dispatches sub-commands directly to the local QEMU engine via
shared.executioner.tool_executor.  No AI or network call involved.

Used when the client machine has QEMU installed and the user wants to
manage VMs directly rather than through the AI chat interface.

Usage (via client_wrapper.py):
    qemu-api list
    qemu-api launch <vm> [sdl|vnc]
    qemu-api stop <vm>
    qemu-api status <vm>
    qemu-api snapshot list|create|restore|delete <vm> [tag]
    qemu-api clone <src> <dst>
    qemu-api delete <vm>
    qemu-api resize <vm> <gb>
    qemu-api config <vm>
    qemu-api profiles
    qemu-api system
    qemu-api isos
    qemu-api show-cmd <vm>
    qemu-api setup-done <vm>
"""

import json
import os
import socket
import threading
from typing import List

import requests

from rich import box
from rich.panel import Panel
from rich.table import Table

from shared.display import (
    console,
    _render_vm_list,
    _render_status,
    _render_monitor,
    _render_profiles,
    _render_compat,
    _render_snapshots,
    _render_system,
)

_CFG_PATH  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "connection_config.json")
try:
    _CONN      = json.load(open(_CFG_PATH))
    _SERVER    = os.environ.get("SERVER_URL", _CONN.get("server_url", "http://localhost:8080"))
    _TOKEN     = os.environ.get("API_TOKEN",  _CONN.get("token", ""))
    _TIMEOUT   = int(os.environ.get("API_TIMEOUT", _CONN.get("timeout", 120)))
    _CA_CERT   = os.environ.get("API_CA_CERT", _CONN.get("ca_cert") or None)
    _VERIFY    = False if os.environ.get("API_VERIFY_SSL", "1") == "0" else (_CA_CERT or _CONN.get("verify_ssl", True))
    _HEADERS   = {"Authorization": f"Bearer {_TOKEN}"} if _TOKEN else {}
except Exception:
    _SERVER, _TOKEN, _TIMEOUT, _VERIFY, _HEADERS = "http://localhost:8080", "", 120, True, {}

try:
    from shared.api.qemu_config import (
        OVMF,
        check_profile_compatibility,
        check_system_capabilities,
        list_profiles,
    )
except ImportError:
    OVMF = {"available": False}
    def list_profiles(): return []                                # type: ignore[misc]
    def check_profile_compatibility(*a, **kw): return {}         # type: ignore[misc]
    def check_system_capabilities(): return {}                   # type: ignore[misc]

try:
    from shared.executioner.tool_executor import manager
except ImportError:
    manager = None                                               # type: ignore[assignment]


def _require_manager():
    if manager is None:
        console.print(
            "[bold yellow]Direct commands require QEMU to be installed on this machine.[/bold yellow]\n"
            "  → Run [bold]setup_client.sh[/bold] to install QEMU, or use the AI chat to manage remote VMs."
        )
        raise SystemExit(1)


def _show_stealth_popup(vm_name: str, setup_cmd: str):
    """Serve the stealth setup script via a one-shot HTTP server so the VM can pull it."""
    script_path = None
    try:
        r = manager.generate_guest_setup(vm_name)
        if r.get("success"):
            script_path = r["path"]
    except Exception:
        pass
    if not script_path:
        return

    script_dir  = os.path.dirname(script_path)
    script_file = os.path.basename(script_path)

    with socket.socket() as s:
        s.bind(("", 0))
        port = s.getsockname()[1]

    import http.server
    class _Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_): pass

    def _serve():
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


def run(args: List[str], verbose: bool = False):
    if not args:
        console.print("[dim]No command given. Try: list, launch, stop, status, profiles, system[/dim]")
        return

    cmd  = args[0]
    rest = args[1:]

    def pp(data):
        if verbose:
            console.print_json(json.dumps(data, default=str))

    if cmd == "list":
        _require_manager()
        vms = manager.list_vms()
        _render_vm_list(vms)
        pp(vms)

    elif cmd == "status" and rest:
        _require_manager()
        r = manager.vm_status(rest[0])
        _render_status(r)
        pp(r)

    elif cmd == "monitor":
        _require_manager()
        name = rest[0] if rest else "all"
        r    = manager.monitor_all() if name == "all" else manager.monitor_vm(name)
        if isinstance(r, dict) and "state" in r:
            _render_monitor(r)
        else:
            for v in (r.values() if isinstance(r, dict) else [r]):
                _render_monitor(v)
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
                resp = requests.get(f"{_SERVER}/execute", headers=_HEADERS,
                                    json={"tool": "list_vms", "args": {}},
                                    timeout=10, verify=_VERIFY)
                if resp.ok:
                    remote_vms = resp.json().get("result", {}).get("vms", [])
                    remote_exists = any(v.get("name") == vm_name for v in remote_vms)
            except Exception:
                pass

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
                                     json={"tool": "launch_vm", "args": {"name": vm_name, "display": display or "vnc"}},
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
            for _viewer in ("vncviewer", "tigervncviewer", "xtigervncviewer", "gvncviewer", "vinagre"):
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
                f"[dim]When done, run:[/dim] [bold]qemu-api setup-done {rest[0]}[/bold]",
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
            _render_snapshots(r)
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
        _render_profiles(list_profiles())

    elif cmd == "check-profile" and rest:
        _render_compat(check_profile_compatibility(rest[0]))

    elif cmd == "system":
        caps = check_system_capabilities()
        caps["ovmf_paths"] = OVMF
        _render_system(caps)

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
            f"[bold]qemu-api launch {rest[0]}[/bold]  (auto-serves on first boot)",
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
                    for chunk in resp.iter_content(chunk_size=4 * 1024 * 1024):
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
                    for chunk in resp.iter_content(chunk_size=4 * 1024 * 1024):
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
            t.extractall(dest_dir)
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

    elif cmd in ("help", "--help", "-h"):
        console.print(Panel(
            "[bold]Direct QEMU commands (no AI):[/bold]\n\n"
            "  list                         List all VMs\n"
            "  status <vm>                  Show VM status\n"
            "  launch <vm> \\[sdl|vnc]       Start a VM\n"
            "  stop <vm>                    Stop a VM\n"
            "  delete <vm>                  Delete a VM and its disk\n"
            "  clone <src> <dst>            Clone a VM\n"
            "  resize <vm> <gb>             Grow disk to <gb> GB\n"
            "  config <vm>                  Show VM config JSON\n"
            "  snapshot list <vm>           List snapshots\n"
            "  snapshot create <vm> <tag>   Create snapshot\n"
            "  snapshot restore <vm> <tag>  Restore snapshot\n"
            "  snapshot delete <vm> <tag>   Delete snapshot\n"
            "  network list|create|delete   Manage networks\n"
            "  profiles                     List hardware profiles\n"
            "  check-profile <name>         Check profile compatibility\n"
            "  system                       Show system capabilities\n"
            "  isos                         List available ISOs\n"
            "  show-cmd <vm>                Print full QEMU command\n"
            "  setup-done <vm>              Mark stealth setup complete\n"
            "  guest-setup <vm>             Generate/serve guest setup script\n"
            "  fetch <vm> [dest]            Download VM disk from server (SHA256 verified)\n"
            "  bundle <vm> [dest_dir]       Download entire VM folder (disk + config) as zip\n\n"
            "[dim]For AI-assisted management, run without arguments: qemu-api[/dim]",
            title="qemu-api help", border_style="cyan",
        ))

    else:
        console.print(
            f"[bold yellow]Unknown command: {cmd}[/bold yellow]  "
            f"Run [bold]qemu-api help[/bold] for usage."
        )

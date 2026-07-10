"""
direct_cli.py — Direct sub-command CLI.

Dispatches the non-interactive ``qemu-api <cmd>`` sub-commands (list, launch,
stop, snapshot, network, setup-done, …) to the manager / executor and renders
their output. cli.py's __main__ imports cli_direct() from here when the process
is invoked with arguments; the arg-less path stays in cli.py as the chat REPL.

This module imports every dependency from its own source module, so it never
imports from cli — the edge is one-directional (cli -> direct_cli), no cycle.
"""

import http.server
import json
import os
import socket
import sys
import threading
from typing import List

from rich import box
from rich.panel import Panel
from rich.table import Table

from orchestrator.executor_client import (
    execute_tool, API_URL, _VERIFY, _TOKEN, _TIMEOUT,
    get_ovmf as _get_ovmf, get_profiles as list_profiles,
    get_capabilities as check_system_capabilities, check_profile_compatibility,
)
from .session import clear_session
from shared.display import (
    console, render_compat, render_monitor, render_profiles,
    render_snapshots, render_status, render_system, render_vm_list,
)
try:
    from shared.executioner.tool_executor import manager
except ImportError:
    manager = None                                                            # type: ignore[assignment]


_SHARED_API_CFG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "executor", "api", "config.json",
)
try:
    # executor/api/config.json is absent on an orchestrator-only checkout
    # (files/executor/ isn't part of that sparse checkout) — fall back to defaults.
    _SHARED_API_CFG = json.load(open(_SHARED_API_CFG_PATH))
except (FileNotFoundError, json.JSONDecodeError):
    _SHARED_API_CFG = {}
_QEMU_HOST_IP    = _SHARED_API_CFG.get("qemu_user_net_gateway", "10.0.2.2")
_IO_CHUNK        = _SHARED_API_CFG.get("io_chunk_bytes", 4 * 1024 * 1024)


def tf_report(vm_name: str) -> None:
    result = execute_tool("fingerprint_vm", {"name": vm_name})
    console.print(result.get("report") or result.get("error") or result)


def _show_stealth_popup(vm_name: str, setup_cmd: str) -> None:
    import platform
    import subprocess
    is_win_guest = setup_cmd.startswith("irm ")
    if is_win_guest:
        how    = "Open PowerShell inside the VM and run:"
        reboot = "No reboot required."
    else:
        how    = "Open a terminal inside the VM and run:"
        reboot = "Then reboot the VM."
    text = (
        f"Stealth VM \"{vm_name}\" needs one-time guest setup.\n\n"
        f"{how}\n\n"
        f"  {setup_cmd}\n\n"
        f"{reboot}\n\n"
        f"When done, run on the host:\n"
        f"  qemu-api setup-done {vm_name}"
    )
    title = f"Stealth Setup: {vm_name}"

    # ── Windows host ──────────────────────────────────────────────────────────
    if platform.system() == "Windows":
        try:
            import ctypes
            # Run in a daemon thread so the CLI doesn't block on the dialog
            threading.Thread(
                target=lambda: ctypes.windll.user32.MessageBoxW(0, text, title, 0x40),
                daemon=True,
            ).start()
            return
        except Exception:
            pass  # ctypes/user32 unavailable — fall through to the next GUI method

    # ── Linux/macOS host: zenity first (GNOME/Cinnamon) ──────────────────────
    try:
        subprocess.Popen([
            "zenity", "--info",
            f"--title={title}",
            f"--text={text}",
            "--width=520",
            "--no-wrap",
        ])
        return
    except FileNotFoundError:
        pass  # zenity not installed — fall through to notify-send
    # notify-send (desktop notification, non-blocking)
    try:
        subprocess.Popen([
            "notify-send", title, setup_cmd,
            "--urgency=critical", "--expire-time=0",
        ])
        return
    except FileNotFoundError:
        pass  # notify-send not installed — fall through to tkinter
    # tkinter (universal fallback)
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(title, text)
        root.destroy()
    except Exception:
        pass  # no GUI toolkit available — the popup is optional, so give up silently


# Dispatches direct sub-commands (list, launch, stop, snapshot, network, etc.) to the manager and renders output.
# In: List[str] args, bool verbose → Out: nothing
def cli_direct(args: List[str], verbose: bool = False):
    if manager is None:
        console.print("[bold yellow]Direct CLI requires the client package. In server-only mode use the AI chat — commands execute remotely via API_URL.[/bold yellow]")
        return

    def pp(data):
        if verbose:
            console.print_json(json.dumps(data, default=str))

    cmd  = args[0]
    rest = args[1:]

    if cmd == "list":
        vms = manager.list_vms()
        render_vm_list(vms)
        if verbose:
            pp(vms)

    elif cmd == "status" and rest:
        r = manager.vm_status(rest[0])
        render_status(r)
        if verbose:
            pp(r)

    elif cmd == "monitor":
        name = rest[0] if rest else "all"
        r    = manager.monitor_all() if name == "all" else manager.monitor_vm(name)
        if isinstance(r, dict) and "state" in r:
            render_monitor(r)
        else:
            for v in r.values():
                render_monitor(v)
        if verbose:
            pp(r)

    elif cmd == "launch" and rest:
        r     = manager.launch_vm(rest[0], display=rest[1] if len(rest) > 1 else None)
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")
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

    elif cmd == "stop" and rest:
        r     = manager.stop_vm(rest[0])
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

    elif cmd == "config" and rest:
        r = manager.show_config(rest[0])
        if r.get("success"):
            console.print_json(json.dumps(r["config"], default=str))
        else:
            console.print(f"[error]{r['error']}[/error]")

    elif cmd == "resize" and len(rest) >= 2:
        r     = manager.resize_disk(rest[0], 0, int(rest[1]))
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

    elif cmd == "clone" and len(rest) >= 2:
        r     = manager.clone_vm(rest[0], rest[1])
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

    elif cmd == "snapshot" and len(rest) >= 2:
        sub = rest[0]
        if sub == "list" and len(rest) >= 2:
            r = manager.snapshot_list(rest[1])
            render_snapshots(r)
        elif sub == "create" and len(rest) >= 3:
            r = manager.snapshot_create(rest[1], rest[2])
            console.print(f"[success]{r.get('message', r.get('error'))}[/success]")
        elif sub == "restore" and len(rest) >= 3:
            r = manager.snapshot_restore(rest[1], rest[2])
            console.print(f"[success]{r.get('message', r.get('error'))}[/success]")
        elif sub == "delete" and len(rest) >= 3:
            r = manager.snapshot_delete(rest[1], rest[2])
            console.print(f"[success]{r.get('message', r.get('error'))}[/success]")

    elif cmd == "network" and rest:
        sub = rest[0]
        if sub == "list":
            console.print_json(json.dumps(manager.list_networks(), default=str))
        elif sub == "create" and len(rest) >= 2:
            console.print_json(json.dumps(manager.create_network(rest[1]), default=str))
        elif sub == "delete" and len(rest) >= 2:
            console.print_json(json.dumps(manager.delete_network(rest[1]), default=str))
        elif sub == "add" and len(rest) >= 3:
            console.print_json(json.dumps(manager.add_vm_to_network(rest[1], rest[2]), default=str))

    elif cmd == "limit" and len(rest) >= 2:
        cpu = int(rest[1]) if len(rest) > 1 else None
        mem = int(rest[2]) if len(rest) > 2 else None
        r   = manager.set_resource_limits(rest[0], cpu_percent=cpu, memory_mb=mem)
        console.print_json(json.dumps(r, default=str))

    elif cmd == "delete" and rest:
        if console.input(f"[warn]Delete '{rest[0]}'? [y/N]:[/warn] ").lower() == "y":
            r = manager.delete_vm(rest[0])
            console.print(f"[success]{r.get('message', r.get('error'))}[/success]")

    elif cmd == "cmd" and len(rest) >= 2:
        r = manager.send_monitor_cmd(rest[0], rest[1])
        if r.get("success"):
            console.print(r["output"])

    elif cmd == "profiles":
        render_profiles(list_profiles())

    elif cmd == "check-profile" and rest:
        render_compat(check_profile_compatibility(rest[0]))

    elif cmd == "system":
        caps = check_system_capabilities()
        caps["ovmf_paths"] = _get_ovmf()
        render_system(caps)

    elif cmd == "isos":
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
            console.print("[warn]No ISOs found in common locations.[/warn]")

    elif cmd == "show-cmd" and rest:
        r = manager.print_command(rest[0])
        if r.get("success"):
            console.print(Panel(r["command"], title="QEMU Command", border_style="cyan"))

    elif cmd == "setup-done" and rest:
        r = manager.mark_stealth_done(rest[0])
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

    elif cmd == "guest-setup" and rest:
        vm_name = rest[0]
        r = manager.generate_guest_setup(vm_name)
        if not r.get("success"):
            console.print(f"[error]{r['error']}[/error]")
            return

        script_path = r["path"]
        script_dir  = os.path.dirname(script_path)
        script_file = os.path.basename(script_path)

        # Find a free port and serve the script via HTTP so the VM can pull it
        with socket.socket() as s:
            s.bind(('', 0))
            port = s.getsockname()[1]

        class _Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=script_dir, **kw)
            def log_message(self, *_):
                pass  # silence access log

        srv = http.server.HTTPServer(('0.0.0.0', port), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        url     = f"http://{_QEMU_HOST_IP}:{port}/{script_file}"

        console.print(Panel(
            f"[bold]Script:[/bold] {script_path}\n\n"
            f"[bold]Inside the VM, run:[/bold]\n"
            f"[cyan]curl {url} | sudo bash[/cyan]\n\n"
            f"[dim]Server will exit when you press Ctrl+C.[/dim]",
            title=f"Guest Setup — {vm_name}",
            border_style="green",
        ))
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            srv.shutdown()
            console.print("[dim]Server stopped.[/dim]")

    elif cmd == "serve":
        import uvicorn
        from orchestrator.executor_client import _EX
        # Parse: serve [host] [port] [--cert cert.pem --key key.pem]
        positional = [a for a in rest if not a.startswith("--")]
        flags      = rest  # full list for --flag parsing
        host = positional[0] if positional else "0.0.0.0"
        port = int(positional[1]) if len(positional) > 1 else _EX.get("port", 8080)
        cert = flags[flags.index("--cert") + 1] if "--cert" in flags else None
        key  = flags[flags.index("--key")  + 1] if "--key"  in flags else None
        tls_line = (
            f"[green]TLS ON[/green] — cert: {cert}"
            if cert else
            "[yellow]TLS OFF[/yellow] — use --cert / --key for HTTPS (required over untrusted networks)"
        )
        console.print(Panel(
            f"[bold cyan]qemu-api executor service[/bold cyan]\n"
            f"Listening on [bold]{host}:{port}[/bold]\n"
            f"{tls_line}\n"
            f"[dim]Set API_TOKEN on this machine and on the AI provider before connecting.[/dim]",
            border_style="cyan", title="Client Machine",
        ))
        uvicorn_kwargs: dict = {"host": host, "port": port, "log_level": "warning"}
        if cert and key:
            uvicorn_kwargs["ssl_certfile"] = cert
            uvicorn_kwargs["ssl_keyfile"]  = key
        elif cert or key:
            console.print("[bold red]--cert and --key must both be provided for TLS.[/bold red]")
            sys.exit(1)
        uvicorn.run("client.server.api_server:app", **uvicorn_kwargs)

    elif cmd == "fetch":
        # fetch <vm_name> [--out /dest/dir] — download VM disk from client machine
        if not rest:
            console.print("[bold red]Usage: fetch <vm_name> [--out /dest/dir][/bold red]")
            sys.exit(1)
        if API_URL == "local":
            console.print("[bold red]fetch requires remote mode (API_URL must be set)[/bold red]")
            sys.exit(1)
        import requests as _req, hashlib as _hl, pathlib as _pl
        vm_name = rest[0]
        out_dir = rest[rest.index("--out") + 1] if "--out" in rest else os.getcwd()
        out_dir = _pl.Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        headers = {"Authorization": f"Bearer {_TOKEN}"} if _TOKEN else {}

        # Fetch checksum first so we can verify after download
        console.print(f"[dim]Fetching SHA256 for [bold]{vm_name}[/bold]...[/dim]")
        try:
            cs_resp = _req.get(f"{API_URL}/images/{vm_name}/sha256",
                               headers=headers, timeout=30, verify=_VERIFY)
        except Exception as e:
            console.print(f"[bold red]Cannot reach client machine: {e}[/bold red]")
            sys.exit(1)
        if not cs_resp.ok:
            console.print(f"[bold red]{cs_resp.status_code}: {cs_resp.text}[/bold red]")
            sys.exit(1)
        cs_data      = cs_resp.json()
        expected_sha = cs_data["sha256"]
        disk_name    = cs_data["disk"]
        total_bytes  = cs_data["size_bytes"]
        out_path     = out_dir / disk_name

        # Resume if partial file exists
        resume_from = out_path.stat().st_size if out_path.exists() else 0
        if resume_from >= total_bytes:
            console.print(f"[green]Already complete:[/green] {out_path}")
        else:
            dl_headers = dict(headers)
            if resume_from:
                dl_headers["Range"] = f"bytes={resume_from}-"
                console.print(f"[dim]Resuming from {resume_from // 1024 // 1024} MB...[/dim]")

            with _req.get(f"{API_URL}/images/{vm_name}", headers=dl_headers,
                          stream=True, timeout=_TIMEOUT, verify=_VERIFY) as r:
                if not r.ok:
                    console.print(f"[bold red]Download failed {r.status_code}: {r.text}[/bold red]")
                    sys.exit(1)
                mode = "ab" if resume_from else "wb"
                downloaded = resume_from
                with open(out_path, mode) as f:
                    for chunk in r.iter_content(chunk_size=_IO_CHUNK):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            pct = downloaded * 100 // total_bytes
                            console.print(
                                f"  [dim]{pct}%  {downloaded // 1024 // 1024} / "
                                f"{total_bytes // 1024 // 1024} MB[/dim]",
                                end="\r",
                            )
            console.print()

        # Verify checksum
        console.print("[dim]Verifying checksum...[/dim]")
        h = _hl.sha256()
        with open(out_path, "rb") as f:
            for chunk in iter(lambda: f.read(_IO_CHUNK), b""):
                h.update(chunk)
        actual_sha = h.hexdigest()
        if actual_sha != expected_sha:
            console.print(f"[bold red]Checksum MISMATCH — file may be corrupt![/bold red]\n"
                          f"  expected: {expected_sha}\n  actual:   {actual_sha}")
            sys.exit(1)
        console.print(Panel(
            f"[bold green]{vm_name}[/bold green] downloaded and verified.\n"
            f"Disk: [bold]{out_path}[/bold]\n"
            f"SHA256: [dim]{actual_sha}[/dim]",
            border_style="green", title="fetch_vm complete",
        ))

    elif cmd == "clear-session":
        clear_session()

    elif cmd == "-tf" and rest:
        tf_report(rest[0])

    else:
        console.print(Panel(
            "[bold]Direct CLI usage:[/bold]\n\n"
            "  qemu-api list\n"
            "  qemu-api status <name>\n"
            "  qemu-api monitor <name|all>\n"
            "  qemu-api launch <name> [display]\n"
            "  qemu-api stop <name>\n"
            "  qemu-api clone <source> <new>\n"
            "  qemu-api config <name>\n"
            "  qemu-api resize <name> <gb>\n"
            "  qemu-api snapshot list|create|restore|delete <vm> [snap]\n"
            "  qemu-api network list|create|delete|add [args]\n"
            "  qemu-api limit <name> <cpu%> [mem_mb]\n"
            "  qemu-api delete <name>\n"
            "  qemu-api cmd <name> \"<qemu cmd>\"\n"
            "  qemu-api profiles\n"
            "  qemu-api check-profile <name>\n"
            "  qemu-api system\n"
            "  qemu-api isos\n"
            "  qemu-api show-cmd <name>\n"
            "  qemu-api clear-session\n"
            "  qemu-api -tf <name>\n"
            "  qemu-api serve [host] [port]    ← run as API computer\n\n"
            "Add [bold]-v[/bold] anywhere for verbose/raw output.\n"
            "Add [bold]-cu[/bold] to AI chat to skip product verification for custom machines.\n"
            "Add [bold]-cs[/bold] to AI chat to clear the session before starting.",
            border_style="cyan", title="qemu-api help",
        ))


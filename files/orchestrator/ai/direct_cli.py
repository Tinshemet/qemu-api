"""
direct_cli.py — Direct sub-command CLI.

Dispatches the non-interactive ``gorgon <cmd>`` sub-commands (list, launch,
stop, snapshot, network, setup-done, …) to the manager / executor and renders
their output. cli.py's __main__ imports cli_direct() from here when the process
is invoked with arguments; the arg-less path stays in cli.py as the chat REPL.

This module imports every dependency from its own source module, so it never
imports from cli — the edge is one-directional (cli -> direct_cli), no cycle.
"""

import getpass
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
from orchestrator.auth import store as _auth_store, sessions as _auth_sessions
from .session import clear_session
from shared.display import (
    console, render_compat, render_fleet, render_fleets, render_monitor,
    render_profiles, render_templates, render_snapshots, render_status,
    render_system, render_vm_list,
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
    """Print an inxi-style fingerprint report for a VM."""
    result = execute_tool("fingerprint_vm", {"name": vm_name})
    console.print(result.get("report") or result.get("error") or result)


def _show_stealth_popup(vm_name: str, setup_cmd: str) -> None:
    """Show the one-time stealth guest-setup instructions via a GUI popup."""
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
        f"  gorgon setup-done {vm_name}"
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


# login/logout bypass the gate itself (nothing to check a session against
# yet); everything else — including "operator" management — is held to it.
# Mirror of client/cli/commands.py's _operator_gate_ok: that module is the
# OTHER in-process path to `manager` (client_wrapper.py routes `gorgon <cmd>`
# there, not here), so any change to this gate must be made in both files.
_AUTH_EXEMPT_COMMANDS = {"login", "logout"}


def _operator_gate_ok(cmd: str) -> bool:
    """True if cmd may dispatch: no operator accounts exist yet (pre-bootstrap,
    identical to legacy behavior — nothing breaks until someone opts in via
    `gorgon login`), or this box holds a valid, unexpired login."""
    if cmd in _AUTH_EXEMPT_COMMANDS:
        return True
    if not _auth_store.operators_exist():
        return True
    return _auth_sessions.current_username() is not None


# Dispatches direct sub-commands (list, launch, stop, snapshot, network, etc.) to the manager and renders output.
# In: List[str] args, bool verbose → Out: nothing
def cli_direct(args: List[str], verbose: bool = False) -> None:
    """Dispatch a direct ``gorgon <cmd>`` sub-command and render its output."""
    if manager is None:
        console.print("[bold yellow]Direct CLI requires the client package. In server-only mode use the AI chat — commands execute remotely via API_URL.[/bold yellow]")
        return

    def pp(data: object) -> None:
        """Pretty-print a JSON result when running in verbose mode."""
        if verbose:
            console.print_json(json.dumps(data, default=str))

    cmd  = args[0]
    rest = args[1:]

    if not _operator_gate_ok(cmd):
        console.print("[bold red]Login required.[/bold red] Run [cyan]gorgon login[/cyan] first.")
        return

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
                f"[dim]When done, run:[/dim] [bold]gorgon setup-done {rest[0]}[/bold]",
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

    elif cmd == "templates":
        render_templates(manager.list_templates())

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
            def log_message(self, *_) -> None:
                """Silence the default HTTP request logging."""
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

    elif cmd == "guest-agent-setup" and rest:
        vm_name = rest[0]
        r = manager.generate_guest_agent_setup(vm_name)
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
            def log_message(self, *_) -> None:
                """Silence the default HTTP request logging."""
                pass  # silence access log

        srv = http.server.HTTPServer(('0.0.0.0', port), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        url = r["cmd_template"].format(port=port)

        console.print(Panel(
            f"[bold]Script:[/bold] {script_path}\n\n"
            f"[bold]Inside the VM, run:[/bold]\n"
            f"[cyan]{url}[/cyan]\n\n"
            f"[dim]Server will exit when you press Ctrl+C.[/dim]",
            title=f"Guest Agent Setup — {vm_name}",
            border_style="green",
        ))
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            srv.shutdown()
            console.print("[dim]Server stopped.[/dim]")

    elif cmd == "guest-ping" and rest:
        r = manager.guest_ping(rest[0])
        if not r.get("success"):
            console.print(f"[error]{r.get('error', 'unknown error')}[/error]")
        else:
            style = "success" if r.get("alive") else "warn"
            state = "alive" if r.get("alive") else "not responding"
            console.print(f"[{style}]{rest[0]}: guest agent {state}[/{style}]")

    elif cmd == "guest-agent-offline" and rest:
        r = manager.provision_guest_agent_offline(rest[0])
        if not r.get("success"):
            console.print(f"[error]{r.get('error', 'unknown error')}[/error]")
        else:
            console.print(f"[success]Stealth serial-agent provisioned offline on '{rest[0]}'.[/success]")

    elif cmd == "execute" and len(rest) >= 2:
        r = manager.run_guest_command(rest[0], " ".join(rest[1:]))
        if not r.get("success"):
            console.print(f"[error]{r.get('error', 'unknown error')}[/error]")
            return
        if r.get("stdout"):
            console.print(r["stdout"], end="" if r["stdout"].endswith("\n") else "\n")
        if r.get("stderr"):
            console.print(f"[error]{r['stderr']}[/error]", end="")
        console.print(f"[dim]exit code: {r.get('exit_code')}[/dim]")

    elif cmd == "label":
        # label add <vm> <label> · label remove <vm> <label> · label list
        sub = rest[0] if rest else None
        if sub in ("add", "remove") and len(rest) >= 3:
            r = (manager.add_label if sub == "add" else manager.remove_label)(rest[1], rest[2])
            style = "success" if r.get("success") else "error"
            console.print(f"[{style}]{r.get('message', r.get('error', 'unknown error'))}[/{style}]")
        elif sub == "list":
            render_fleets(manager.list_labels().get("usage", {}))
        else:
            console.print("[error]Usage: gorgon label add|remove <vm> <label>  |  "
                          "gorgon label list[/error]")

    elif cmd == "fleet":
        # fleet                       → list current fleets (labels → member VMs)
        # fleet <label>               → preview members of one fleet
        # fleet <label> exec <cmd...> → run a command on every member
        # fleet <label> stop|launch|ping|status → broadcast that action
        if not rest:
            render_fleets(manager.list_labels().get("usage", {}))
            return
        label  = rest[0]
        action = rest[1] if len(rest) > 1 else None
        if action is None:
            # Preview: show the members of this fleet (status action, read-only)
            r = manager.fleet(label, "status")
            render_fleet(r) if r.get("results") else console.print(
                f"[warn]{r.get('error', 'No members.')}[/warn]")
            return
        if action == "exec":
            if len(rest) < 3:
                console.print("[error]Usage: gorgon fleet <label> exec <command>[/error]")
                return
            r = manager.fleet(label, "exec", command=" ".join(rest[2:]))
        elif action in ("ping", "status", "stop", "launch"):
            r = manager.fleet(label, action)
        else:
            console.print(f"[error]Unknown fleet action '{action}'. "
                          f"Use: exec, ping, status, stop, launch.[/error]")
            return
        render_fleet(r)

    elif cmd == "guest-agent-enable" and rest:
        r = manager.update_config(rest[0], {"guest_agent": True})
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

    elif cmd == "guest-agent-disable" and rest:
        r = manager.update_config(rest[0], {"guest_agent": False})
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

    elif cmd == "serve":
        import uvicorn
        # Parse: serve [host] [port] [--cert cert.pem --key key.pem]
        positional = [a for a in rest if not a.startswith("--")]
        flags      = rest  # full list for --flag parsing
        host = positional[0] if positional else "0.0.0.0"
        port = int(positional[1]) if len(positional) > 1 else 8080
        cert = flags[flags.index("--cert") + 1] if "--cert" in flags else None
        key  = flags[flags.index("--key")  + 1] if "--key"  in flags else None
        tls_line = (
            f"[green]TLS ON[/green] — cert: {cert}"
            if cert else
            "[yellow]TLS OFF[/yellow] — use --cert / --key for HTTPS (required over untrusted networks)"
        )
        console.print(Panel(
            f"[bold cyan]gorgon orchestrator service[/bold cyan]\n"
            f"Listening on [bold]{host}:{port}[/bold]\n"
            f"{tls_line}\n"
            f"[dim]Set API_TOKEN on this machine and on every client before connecting.[/dim]",
            border_style="cyan", title="Orchestrator Machine",
        ))
        uvicorn_kwargs: dict = {"host": host, "port": port, "log_level": "warning"}
        if cert and key:
            uvicorn_kwargs["ssl_certfile"] = cert
            uvicorn_kwargs["ssl_keyfile"]  = key
        elif cert or key:
            console.print("[bold red]--cert and --key must both be provided for TLS.[/bold red]")
            sys.exit(1)
        uvicorn.run("orchestrator.http.api_server:app", **uvicorn_kwargs)

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

    elif cmd == "login":
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
        _auth_sessions.invalidate_session(_auth_sessions.read_current_session())
        _auth_sessions.clear_current_session()
        console.print("[dim]Logged out.[/dim]")

    elif cmd == "operator" and rest:
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
        # gorgon contract forge | show <file> | sign <file> <safeword>
        import os, json as _json
        from orchestrator.ai import forge as _forge
        _agent_dir = os.path.dirname(os.path.abspath(_forge.__file__))
        sub = rest[0] if rest else ""
        if sub == "forge":
            _forge.forge_interactive(
                ask=lambda p: console.input(f"[bold cyan]{p}:[/bold cyan] ").strip(),
                out=console.print, write_dir=_agent_dir)
        elif sub == "show" and len(rest) >= 2:
            from shared.grgn_sign import read as _read_grgn
            path = rest[1] if os.path.isabs(rest[1]) else os.path.join(_agent_dir, rest[1])
            g, st = _read_grgn(path)
            if g is None:
                console.print(f"[error]Cannot read {rest[1]} ({st}).[/error]")
            else:
                console.print(_forge.render(g))
                console.print(f"[dim]integrity: {st}[/dim]")
        elif sub == "sign" and len(rest) >= 3:
            from shared.grgn_sign import read as _read_grgn
            path = rest[1] if os.path.isabs(rest[1]) else os.path.join(_agent_dir, rest[1])
            g, st = _read_grgn(path)
            if g is None:
                console.print(f"[error]Cannot read {rest[1]} ({st}).[/error]")
            else:
                try:
                    _forge.sign(g, rest[2]); _forge.write_grgn(g, path)
                    console.print(f"[success]Signed → {path}[/success]")
                except ValueError as e:
                    console.print(f"[error]{e}[/error]")
        else:
            console.print("[yellow]Usage: gorgon contract forge | show <file> | sign <file> <safeword>[/yellow]")

    else:
        from shared.command_help import load_local_catalog, render_terminal_panel
        try:
            from orchestrator.executor_client import _ALLOWED_TOOLS
            allowed = set(_ALLOWED_TOOLS) or None
        except Exception:
            allowed = None
        catalog, order = load_local_catalog()
        body = (render_terminal_panel(catalog, allowed, order) if catalog
                else "[dim]Command list unavailable.[/dim]")
        body += (
            "\n\n[bold cyan]Direct-CLI extras[/bold cyan]\n"
            "  limit <vm> <cpu%> \\[mem_mb]     Set CPU/memory resource limits\n"
            "  cmd <vm> \"<monitor cmd>\"        Send a raw QEMU monitor command\n"
            "  serve \\[host] \\[port]           Run this node as the executor API\n"
            "  clear-session                  Wipe the saved AI session\n"
            "  -tf <vm>                       Show a fingerprint report for a VM\n"
            "  login \\[username]               Log in (creates the first operator if none exist)\n"
            "  logout                         End this box's login session\n"
            "  operator add|list|remove       Manage operator accounts (requires login)\n\n"
            "[bold cyan]Flags[/bold cyan]\n"
            "  -v   verbose / raw output      -cu  custom mode      -cs  clear session first"
        )
        console.print(Panel(body, border_style="cyan", title="gorgon help"))


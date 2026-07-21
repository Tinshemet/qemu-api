"""commands/launch.py — gorgon launch <vm> [sdl|vnc]."""

import requests

from client.cli.commands.base import Command
from client.cli.commands.context import (
    _HEADERS, _require_manager, _SERVER, _show_stealth_popup, _TIMEOUT, _TOKEN,
    _VERIFY, _VNC_VIEWERS, console, manager, Panel, pp,
)


class LaunchCommand(Command):
    names = ("launch",)
    min_args = 1

    def run(self, cmd, rest, verbose):
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
        pp(r, verbose)

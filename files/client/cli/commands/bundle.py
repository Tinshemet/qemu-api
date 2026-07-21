"""commands/bundle.py — gorgon bundle <vm> [dest_dir] (fetch + extract a full VM bundle)."""

import os

import requests

from client.cli.commands.base import Command
from client.cli.commands.context import _HEADERS, _IO_CHUNK, _SERVER, _TIMEOUT, _VERIFY, console


class BundleCommand(Command):
    names = ("bundle",)
    min_args = 1

    def run(self, cmd, rest, verbose):
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

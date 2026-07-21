"""commands/fetch.py — gorgon fetch <vm> [dest] (stream + checksum a disk image)."""

import os

import requests

from client.cli.commands.base import Command
from client.cli.commands.context import _HEADERS, _IO_CHUNK, _SERVER, _TIMEOUT, _VERIFY, console


class FetchCommand(Command):
    names = ("fetch",)
    min_args = 1

    def run(self, cmd, rest, verbose):
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

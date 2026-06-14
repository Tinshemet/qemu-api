"""
display.py — Rich Terminal Rendering Layer

All Rich console output: theme, console singleton, and every _render_*
helper that turns a manager result dict into a formatted terminal panel
or table.
"""

from typing import Any, Dict, List

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.live    import Live
from rich.panel   import Panel
from rich.spinner import Spinner
from rich.table   import Table
from rich.text    import Text
from rich.theme   import Theme

THEME = Theme({
    "tool":    "bold cyan",
    "success": "bold green",
    "error":   "bold red",
    "warn":    "bold yellow",
    "info":    "dim cyan",
    "ai":      "bold white",
    "user":    "bold blue",
    "dim":     "dim white",
    "header":  "bold magenta",
})
console = Console(theme=THEME)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt_uptime(seconds: int) -> str:
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ── Render functions ───────────────────────────────────────────────────────────

def _render_vm_list(vms: List[Dict]):
    if not vms:
        console.print("[warn]No VMs found.[/warn]")
        return
    t = Table(box=box.ROUNDED, border_style="cyan", header_style="bold cyan")
    t.add_column("#",      style="dim",        width=3)
    t.add_column("Name",   style="bold white")
    t.add_column("OS",     style="cyan")
    t.add_column("CPU",    justify="right")
    t.add_column("RAM",    justify="right")
    t.add_column("Disks",  justify="right")
    t.add_column("Status", justify="center")
    for i, vm in enumerate(vms, 1):
        status_str = (
            "[success]● running[/success]" if vm.get("status") == "running"
            else "[dim]○ stopped[/dim]"
        )
        t.add_row(
            str(i),
            vm.get("name", "?"),
            vm.get("os", "?"),
            str(vm.get("cpu_cores", "?")),
            f"{vm.get('memory_mb', 0) // 1024}GB",
            str(vm.get("disks", "?")),
            status_str,
        )
    console.print(t)


def _render_status(status: Dict):
    state  = status.get("state", "unknown")
    colour = "green" if state == "running" else "red"
    name   = status.get("name", "?")
    lines  = [f"[bold {colour}]{state.upper()}[/bold {colour}]"]
    if status.get("pid"):
        lines.append(f"PID: {status['pid']}")
    if status.get("cpu_percent") is not None:
        lines.append(f"CPU: {status['cpu_percent']:.1f}%")
    if status.get("rss_mb"):
        lines.append(f"RAM: {status['rss_mb']}MB")
    if status.get("uptime_s"):
        lines.append(f"Uptime: {_fmt_uptime(status['uptime_s'])}")
    if status.get("qemu_status"):
        lines.append(f"QEMU: {status['qemu_status']}")
    console.print(Panel("\n".join(lines), title=f"[bold]{name}[/bold]", border_style=colour))


def _render_monitor(report: Dict):
    name  = report.get("name", "?")
    state = report.get("state", "stopped")
    if state != "running":
        console.print(f"[warn]{name} is not running.[/warn]")
        return

    t = Table(box=box.SIMPLE, show_header=False)
    t.add_column("Key",   style="dim",        width=20)
    t.add_column("Value", style="bold white")

    t.add_row("State", f"[success]{state}[/success]")
    if report.get("pid"):         t.add_row("PID",       str(report["pid"]))
    if report.get("cpu_percent") is not None:
        t.add_row("CPU %",        f"{report['cpu_percent']:.1f}%")
    if report.get("rss_mb"):      t.add_row("RAM (RSS)", f"{report['rss_mb']} MB")
    if report.get("uptime_s"):    t.add_row("Uptime",    _fmt_uptime(report["uptime_s"]))
    if report.get("open_files"):  t.add_row("Open files", str(report["open_files"]))

    disk_io = report.get("disk_io", {})
    if disk_io:
        t.add_row("Disk Read",  f"{disk_io.get('read_mb', 0):.1f} MB")
        t.add_row("Disk Write", f"{disk_io.get('write_mb', 0):.1f} MB")

    for bs in report.get("block_stats", []):
        t.add_row(
            f"Block [{bs['device']}]",
            f"R:{bs['rd_bytes']//1024}K  W:{bs['wr_bytes']//1024}K",
        )

    console.print(Panel(t, title=f"[bold]{name}[/bold] — Monitor", border_style="cyan"))


def _render_profiles(profiles: List[Dict]):
    t = Table(box=box.ROUNDED, border_style="magenta", header_style="bold magenta")
    t.add_column("Name",        style="bold cyan")
    t.add_column("Arch",        style="dim")
    t.add_column("Custom",      justify="center")
    t.add_column("Description", style="white")
    for p in profiles:
        custom = "[success]✓[/success]" if p.get("custom") == "True" else ""
        t.add_row(p["name"], p.get("arch", "x86_64"), custom, p.get("description", ""))
    console.print(t)


def _render_compat(result: Dict):
    name  = result.get("profile", "?")
    ok    = result.get("compatible", False)
    color = "green" if ok else "red"
    lines = [f"[bold {color}]{'✓ Compatible' if ok else '✗ Not Compatible'}[/bold {color}]\n"]

    for i in result.get("issues", []):
        lines.append(f"  [red]✗[/red] {i}")
    for w in result.get("warnings", []):
        lines.append(f"  [yellow]⚠[/yellow] {w}")
    for a in result.get("alternatives", []):
        lines.append(f"  [cyan]→[/cyan] {a}")
    if result.get("notes"):
        lines.append(f"\n  [dim]{result['notes']}[/dim]")

    host = result.get("host_summary", {})
    if host:
        lines.append(
            f"\n  [dim]Host: {host.get('cpu','?')} | "
            f"{host.get('cores','?')} cores | "
            f"{(host.get('memory_mb',0))//1024}GB RAM | "
            f"KVM: {'✓' if host.get('kvm') else '✗'} | "
            f"OVMF: {'✓' if host.get('ovmf') else '✗'}[/dim]"
        )

    console.print(Panel(
        "\n".join(lines),
        title=f"Compatibility — [bold]{name}[/bold]",
        border_style=color,
    ))


def _render_vm_failure(report: Dict):
    name  = report.get("name", "?")
    lines = []

    if report.get("diagnosis"):
        lines.append(f"[bold red]✗ {report['diagnosis']}[/bold red]")

    errors = report.get("errors", [])
    if errors:
        lines.append("")
        lines.append("[bold]Errors found:[/bold]")
        for e in errors:
            lines.append(f"  [red]✗[/red] {e['meaning']}")
            lines.append(f"    [dim]{e['line']}[/dim]")
    elif report.get("log_exists"):
        lines.append("[warn]No specific errors detected in log.[/warn]")
    else:
        lines.append("[warn]No log file — VM crashed before writing output.[/warn]")

    if report.get("suggestions"):
        lines.append("")
        lines.append("[bold]Suggested fixes:[/bold]")
        for s in report["suggestions"]:
            lines.append(f"  [cyan]→[/cyan] [bold]{s}[/bold]")

    cfg = report.get("config_summary", {})
    if cfg:
        lines.append("")
        lines.append("[bold]Config snapshot:[/bold]")
        for k, v in cfg.items():
            if v is not None and v != "" and v != []:
                lines.append(f"  [dim]{k}:[/dim] {v}")

    raw = report.get("raw_tail", "").strip()
    if raw:
        lines.append("")
        lines.append("[bold]Last log lines:[/bold]")
        for ll in raw.splitlines()[-10:]:
            colour = "red" if any(
                w in ll.lower() for w in ("error", "failed", "abort", "fatal", "segfault")
            ) else "dim"
            lines.append(f"  [{colour}]{ll}[/{colour}]")

    console.print(Panel(
        "\n".join(lines),
        title=f"[bold red]Failure Report — {name}[/bold red]",
        border_style="red",
    ))


def _render_snapshots(result: Dict):
    snaps = result.get("snapshots", [])
    if not snaps:
        console.print("[warn]No snapshots found.[/warn]")
        return
    t = Table(box=box.ROUNDED, border_style="cyan", header_style="bold cyan")
    t.add_column("ID"); t.add_column("Tag"); t.add_column("Size"); t.add_column("Date")
    for s in snaps:
        t.add_row(s.get("id", "?"), s.get("tag", "?"), s.get("vm_size", "?"), s.get("date", "?"))
    console.print(t)


def _render_system(caps: Dict):
    t = Table(box=box.SIMPLE, show_header=False)
    t.add_column("Key",   style="dim",        width=25)
    t.add_column("Value", style="bold white")
    t.add_row("Host CPU",   caps.get("host_cpu", "?"))
    t.add_row("CPU Cores",  str(caps.get("host_cpu_cores", "?")))
    t.add_row("Host RAM",   f"{(caps.get('host_memory_mb', 0)) // 1024} GB")
    t.add_row("Free Disk",  f"{caps.get('home_free_gb', '?')} GB")
    t.add_row("Arch",       caps.get("host_arch", "?"))
    t.add_row("KVM",        "[success]✓[/success]" if caps.get("kvm_available") else "[error]✗[/error]")
    t.add_row("VT-x/AMD-V", "[success]✓[/success]" if caps.get("vmx") or caps.get("svm") else "[error]✗[/error]")
    t.add_row("QEMU",       caps.get("qemu_version", "[error]not found[/error]"))
    t.add_row("qemu-arm",   "[success]✓[/success]" if caps.get("qemu_arm_installed") else "[dim]✗ not installed[/dim]")
    ovmf = caps.get("ovmf", {})
    t.add_row("OVMF Code",  ovmf.get("code") or "[warn]not found[/warn]")
    t.add_row("OVMF Vars",  ovmf.get("vars") or "[warn]not found[/warn]")
    console.print(Panel(t, title="[bold]System Capabilities[/bold]", border_style="magenta"))


def _print_banner(verbose: bool, ollama_url: str, ollama_model: str, ovmf_available: bool, ovmf_code: str):
    ovmf_line = (
        f"[success]OVMF ✓[/success]  {ovmf_code}"
        if ovmf_available
        else "[warn]OVMF ✗  SeaBIOS fallback active[/warn]"
    )
    verb_line = (
        "[success]verbose mode ON[/success]" if verbose
        else "[dim]verbose OFF  (use -v to enable)[/dim]"
    )
    console.print(Panel(
        f"[bold cyan]QEMU/KVM AI Assistant[/bold cyan]  •  Llama 3.1 via Ollama\n"
        f"Model: [bold]{ollama_model}[/bold]  |  {ollama_url}\n"
        f"{ovmf_line}\n"
        f"{verb_line}\n"
        f"[dim]Commands: 'exit' · 'clear session' · 'list' · 'system'[/dim]",
        border_style="cyan",
        title="[bold]qemu-api[/bold]",
    ))

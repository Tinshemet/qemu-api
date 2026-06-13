"""
ollama_wrapper.py — Llama 3.1 + Ollama Tool-Use Interface
Part 3 of 4: QEMU/KVM Ollama Wrapper (v3)

New:
  - Rich CLI (colours, tables, spinners, panels)
  - -v / --verbose flag: raw tool data only shown in verbose mode
  - Session memory (conversation persists across restarts)
  - Bulk operations ("stop all", "snapshot everything")
  - ISO scanner tool
  - VM clone / network isolation tools
  - Snapshot list/restore/delete tools
  - Resource limit tool
  - Dry-run support
"""

import json, os, re, sys, time, shutil
from typing import Any, Dict, List, Optional
from datetime import datetime

import requests
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich.spinner import Spinner
from rich.columns import Columns
from rich import box
from rich.theme import Theme

from qemu_config import (
    MachineConfig, DiskConfig, NetworkConfig,
    get_all_profiles, apply_profile, list_profiles,
    save_custom_profile, delete_custom_profile,
    check_profile_compatibility, check_system_capabilities,
    OVMF,
)
from qemu_manager import QemuManager

# ─────────────────────────────────────────────
#  CONSOLE SETUP
# ─────────────────────────────────────────────

THEME = Theme({
    "tool":     "bold cyan",
    "success":  "bold green",
    "error":    "bold red",
    "warn":     "bold yellow",
    "info":     "dim cyan",
    "ai":       "bold white",
    "user":     "bold blue",
    "dim":      "dim white",
    "header":   "bold magenta",
})
console = Console(theme=THEME)

OLLAMA_URL   = os.environ.get("OLLAMA_URL",   "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")

manager = QemuManager()

# Session memory file — persists conversation history
SESSION_FILE = os.path.expanduser("~/.qemu_vms/.session.json")
MAX_SESSION_HISTORY = 40  # keep last N messages to avoid context overflow


# ─────────────────────────────────────────────
#  SESSION MEMORY
# ─────────────────────────────────────────────

def load_session() -> List[Dict]:
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE) as f:
                data = json.load(f)
            return data[-MAX_SESSION_HISTORY:]
        except Exception:
            pass
    return []


def save_session(messages: List[Dict]):
    try:
        # Don't persist system prompt or tool results — just user/assistant turns
        filtered = [
            m for m in messages
            if m.get("role") in ("user", "assistant") and m.get("content")
        ][-MAX_SESSION_HISTORY:]
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w") as f:
            json.dump(filtered, f, indent=2)
    except Exception:
        pass


def clear_session():
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    console.print("[success]Session memory cleared.[/success]")


# ─────────────────────────────────────────────
#  RICH DISPLAY HELPERS
# ─────────────────────────────────────────────

def _fmt_uptime(seconds: int) -> str:
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _render_vm_list(vms: List[Dict]):
    if not vms:
        console.print("[warn]No VMs found.[/warn]")
        return
    t = Table(box=box.ROUNDED, border_style="cyan", header_style="bold cyan")
    t.add_column("#",        style="dim",          width=3)
    t.add_column("Name",     style="bold white")
    t.add_column("OS",       style="cyan")
    t.add_column("CPU",      justify="right")
    t.add_column("RAM",      justify="right")
    t.add_column("Disks",    justify="right")
    t.add_column("Status",   justify="center")
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
    state   = status.get("state", "unknown")
    colour  = "green" if state == "running" else "red"
    name    = status.get("name", "?")
    lines   = [f"[bold {colour}]{state.upper()}[/bold {colour}]"]
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
    t.add_column("Key",   style="dim", width=20)
    t.add_column("Value", style="bold white")

    t.add_row("State",   f"[success]{state}[/success]")
    if report.get("pid"):         t.add_row("PID",        str(report["pid"]))
    if report.get("cpu_percent") is not None:
        t.add_row("CPU %", f"{report['cpu_percent']:.1f}%")
    if report.get("rss_mb"):      t.add_row("RAM (RSS)",  f"{report['rss_mb']} MB")
    if report.get("uptime_s"):    t.add_row("Uptime",     _fmt_uptime(report["uptime_s"]))
    if report.get("open_files"):  t.add_row("Open files", str(report["open_files"]))

    disk_io = report.get("disk_io", {})
    if disk_io:
        t.add_row("Disk Read",  f"{disk_io.get('read_mb', 0):.1f} MB")
        t.add_row("Disk Write", f"{disk_io.get('write_mb', 0):.1f} MB")

    block = report.get("block_stats", [])
    for bs in block:
        t.add_row(
            f"Block [{bs['device']}]",
            f"R:{bs['rd_bytes']//1024}K  W:{bs['wr_bytes']//1024}K"
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

    issues = result.get("issues", [])
    for i in issues:
        lines.append(f"  [red]✗[/red] {i}")
    warnings = result.get("warnings", [])
    for w in warnings:
        lines.append(f"  [yellow]⚠[/yellow] {w}")
    alts = result.get("alternatives", [])
    for a in alts:
        lines.append(f"  [cyan]→[/cyan] {a}")
    if result.get("notes"):
        lines.append(f"\n  [dim]{result['notes']}[/dim]")

    host = result.get("host_summary", {})
    if host:
        lines.append(f"\n  [dim]Host: {host.get('cpu','?')} | "
                     f"{host.get('cores','?')} cores | "
                     f"{(host.get('memory_mb',0))//1024}GB RAM | "
                     f"KVM: {'✓' if host.get('kvm') else '✗'} | "
                     f"OVMF: {'✓' if host.get('ovmf') else '✗'}[/dim]")

    console.print(Panel("\n".join(lines), title=f"Compatibility — [bold]{name}[/bold]", border_style=color))


def _render_vm_failure(report: Dict):
    name  = report.get("name", "?")
    lines = []

    diagnosis = report.get("diagnosis", "")
    if diagnosis:
        lines.append(f"[bold red]✗ {diagnosis}[/bold red]")

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

    suggestions = report.get("suggestions", [])
    if suggestions:
        lines.append("")
        lines.append("[bold]Suggested fixes:[/bold]")
        for s in suggestions:
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
        raw_lines = raw.splitlines()[-10:]
        lines.append("")
        lines.append("[bold]Last log lines:[/bold]")
        for ll in raw_lines:
            colour = "red" if any(w in ll.lower() for w in ("error","failed","abort","fatal","segfault")) else "dim"
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
    t.add_column("ID");  t.add_column("Tag");  t.add_column("Size");  t.add_column("Date")
    for s in snaps:
        t.add_row(s.get("id","?"), s.get("tag","?"), s.get("vm_size","?"), s.get("date","?"))
    console.print(t)


def _render_system(caps: Dict):
    t = Table(box=box.SIMPLE, show_header=False)
    t.add_column("Key",   style="dim", width=25)
    t.add_column("Value", style="bold white")
    t.add_row("Host CPU",    caps.get("host_cpu", "?"))
    t.add_row("CPU Cores",   str(caps.get("host_cpu_cores", "?")))
    t.add_row("Host RAM",    f"{(caps.get('host_memory_mb',0))//1024} GB")
    t.add_row("Free Disk",   f"{caps.get('home_free_gb', '?')} GB")
    t.add_row("Arch",        caps.get("host_arch", "?"))
    t.add_row("KVM",         "[success]✓[/success]" if caps.get("kvm_available") else "[error]✗[/error]")
    t.add_row("VT-x/AMD-V",  "[success]✓[/success]" if caps.get("vmx") or caps.get("svm") else "[error]✗[/error]")
    t.add_row("QEMU",        caps.get("qemu_version", "[error]not found[/error]"))
    t.add_row("qemu-arm",    "[success]✓[/success]" if caps.get("qemu_arm_installed") else "[dim]✗ not installed[/dim]")
    ovmf = caps.get("ovmf", {})
    t.add_row("OVMF Code",   ovmf.get("code") or "[warn]not found[/warn]")
    t.add_row("OVMF Vars",   ovmf.get("vars") or "[warn]not found[/warn]")
    console.print(Panel(t, title="[bold]System Capabilities[/bold]", border_style="magenta"))


# ─────────────────────────────────────────────
#  TOOLS
# ─────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "clarify",
            "description": "Ask the user for a specific missing required piece of information. Only use for truly required fields that cannot be defaulted. Ask ONE thing at a time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options":  {"type": "array", "items": {"type": "string"}},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_system",
            "description": "Check host system capabilities: KVM, OVMF, QEMU, CPU, RAM, disk space.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_isos",
            "description": "Scan common directories for ISO files the user might want to use.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_vms",
            "description": "List all VMs with status.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_profiles",
            "description": "List all hardware profiles including custom ones.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_profile_compatibility",
            "description": "Check if a hardware profile is compatible with this system.",
            "parameters": {
                "type": "object",
                "properties": {"profile_name": {"type": "string"}},
                "required": ["profile_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_profile",
            "description": "Create and save a custom hardware profile.",
            "parameters": {
                "type": "object",
                "properties": {
                    "profile_name": {"type": "string"},
                    "description":  {"type": "string"},
                    "machine_class":{"type": "string"},
                    "machine_type": {"type": "string"},
                    "machine_arch": {"type": "string"},
                    "qemu_binary":  {"type": "string"},
                    "cpu_model":    {"type": "string"},
                    "cpu_cores":    {"type": "integer"},
                    "cpu_threads":  {"type": "integer"},
                    "memory_mb":    {"type": "integer"},
                    "gpu":          {"type": "string"},
                    "audio":        {"type": "string"},
                    "display":      {"type": "string"},
                    "battery":      {"type": "boolean"},
                    "uefi":         {"type": "boolean"},
                    "bios":         {"type": "string"},
                    "manufacturer": {"type": "string"},
                    "product_name": {"type": "string"},
                    "bios_vendor":  {"type": "string"},
                    "bios_version": {"type": "string"},
                    "notes":        {"type": "string"},
                },
                "required": ["profile_name", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_profile",
            "description": "Delete a custom hardware profile.",
            "parameters": {
                "type": "object",
                "properties": {"profile_name": {"type": "string"}},
                "required": ["profile_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_vm",
            "description": "Create a new VM. Use clarify if name is missing. All other fields have defaults.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":         {"type": "string"},
                    "os_type":      {"type": "string"},
                    "os_name":      {"type": "string"},
                    "profile":      {"type": "string"},
                    "cpu_cores":    {"type": "integer"},
                    "cpu_threads":  {"type": "integer"},
                    "memory_mb":    {"type": "integer"},
                    "disk_size_gb": {"type": "integer"},
                    "disk_format":  {"type": "string"},
                    "iso_path":     {"type": "string"},
                    "display":      {"type": "string"},
                    "gpu":          {"type": "string"},
                    "audio":        {"type": "string"},
                    "network_mode": {"type": "string"},
                    "bridge_iface": {"type": "string"},
                    "mac_address":  {"type": "string"},
                    "cpu_model":    {"type": "string"},
                    "machine_type": {"type": "string"},
                    "manufacturer": {"type": "string"},
                    "product_name": {"type": "string"},
                    "uefi":         {"type": "boolean"},
                    "kvm":          {"type": "boolean"},
                    "battery":      {"type": "boolean"},
                    "hugepages":    {"type": "boolean"},
                    "description":  {"type": "string"},
                    "extra_args":   {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "os_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clone_vm",
            "description": "Clone an existing VM into a new VM with copy-on-write disks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_name": {"type": "string"},
                    "new_name":    {"type": "string"},
                },
                "required": ["source_name", "new_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "launch_vm",
            "description": "Start a VM. Optionally override display mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":     {"type": "string"},
                    "display":  {"type": "string"},
                    "dry_run":  {"type": "boolean", "description": "Print command without running"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_vm",
            "description": "Stop a running VM. Use name='all' to stop all VMs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":  {"type": "string"},
                    "force": {"type": "boolean"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vm_status",
            "description": "Get status of a VM.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "monitor_vm",
            "description": "Deep activity report. Use name='all' for all VMs.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_config",
            "description": "Show full VM configuration.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_config",
            "description": "Update config fields of a stopped VM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":    {"type": "string"},
                    "updates": {"type": "object"},
                },
                "required": ["name", "updates"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resize_disk",
            "description": "Resize a VM disk (VM must be stopped).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":        {"type": "string"},
                    "disk_index":  {"type": "integer"},
                    "new_size_gb": {"type": "integer"},
                },
                "required": ["name", "new_size_gb"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snapshot_create",
            "description": "Create a snapshot of a running VM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":      {"type": "string"},
                    "snap_name": {"type": "string"},
                },
                "required": ["name", "snap_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snapshot_list",
            "description": "List all snapshots for a VM.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snapshot_restore",
            "description": "Restore a VM snapshot by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":      {"type": "string"},
                    "snap_name": {"type": "string"},
                },
                "required": ["name", "snap_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snapshot_delete",
            "description": "Delete a snapshot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":      {"type": "string"},
                    "snap_name": {"type": "string"},
                },
                "required": ["name", "snap_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_resource_limits",
            "description": "Limit CPU% or memory of a running VM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":        {"type": "string"},
                    "cpu_percent": {"type": "integer", "description": "0-100"},
                    "memory_mb":   {"type": "integer"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_network",
            "description": "Create a private isolated network between VMs (no internet access).",
            "parameters": {
                "type": "object",
                "properties": {"net_name": {"type": "string"}},
                "required": ["net_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_network",
            "description": "Delete an isolated network.",
            "parameters": {
                "type": "object",
                "properties": {"net_name": {"type": "string"}},
                "required": ["net_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_networks",
            "description": "List all isolated VM networks.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_vm_to_network",
            "description": "Add a VM to an isolated network.",
            "parameters": {
                "type": "object",
                "properties": {
                    "net_name": {"type": "string"},
                    "vm_name":  {"type": "string"},
                },
                "required": ["net_name", "vm_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_display",
            "description": "Open the display viewer for a running VM.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_shell",
            "description": "Open a serial console in a terminal.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_vm",
            "description": "Delete and remove a VM configuration and optionally its disk files. Use delete_disks=true to also remove disk images. Call this when user says delete, remove, kill, or destroy a VM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":         {"type": "string"},
                    "delete_disks": {"type": "boolean"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_vm_logs",
            "description": (
                "Read a VM launch log and diagnose why it failed. "
                "ALWAYS call this when a VM is stopped unexpectedly, fails to launch, "
                "crashes immediately, or the user asks why a VM failed. "
                "Returns parsed errors, root cause diagnosis, and fix suggestions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name":  {"type": "string", "description": "VM name"},
                    "lines": {"type": "integer", "description": "Log lines to read (default 50)"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_vm_logs",
            "description": (
                "Read a VM launch log and diagnose why it failed. "
                "ALWAYS call this when a VM stopped unexpectedly, failed to launch, "
                "crashed immediately, or the user asks why a VM failed or is not working. "
                "Returns parsed errors, root cause diagnosis, and fix suggestions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name":  {"type": "string", "description": "VM name"},
                    "lines": {"type": "integer", "description": "Log lines to read (default 50)"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "print_command",
            "description": "Print the full QEMU launch command without running it.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_monitor_cmd",
            "description": "Send a raw QEMU monitor command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "cmd":  {"type": "string"},
                },
                "required": ["name", "cmd"],
            },
        },
    },
]


# ─────────────────────────────────────────────
#  VM NAME RESOLVER
# ─────────────────────────────────────────────

def _resolve_vm_name(vms: List[Dict], ref: str) -> Optional[str]:
    if ref == "all":
        return "all"
    for vm in vms:
        if vm["name"] == ref:
            return vm["name"]
    m = re.match(r"^\d+$", ref.strip())
    if m:
        idx = int(m.group()) - 1
        if 0 <= idx < len(vms):
            return vms[idx]["name"]
    lower = ref.lower()
    for vm in vms:
        if lower in vm["name"].lower() or lower in vm.get("os", "").lower():
            return vm["name"]
    return None


def _resolve_iso(iso_hint: str) -> Optional[str]:
    """
    Resolve an ISO path from a vague hint or hallucinated path.
    Strategy:
      1. Exact path — use it directly
      2. Fix wrong username in path (AI hallucinates /home/user/)
      3. Fuzzy keyword scoring across all search dirs
      4. OS-keyword scan — find any ISO containing "ubuntu"/"windows" etc.
      5. Last resort — return first ISO found in Desktop/Images or Desktop/images
    """
    if not iso_hint:
        return None

    real_home = os.path.expanduser("~")

    # ── Step 1: exact path ────────────────────────────────────
    for candidate in [iso_hint, os.path.expanduser(iso_hint)]:
        if os.path.exists(candidate):
            return candidate

    # ── Step 2: fix wrong username ────────────────────────────
    # AI often hallucinates /home/user/ or /home/username/ that doesn't exist
    fixed = re.sub(r"^/home/[^/]+/", real_home + "/", iso_hint)
    if os.path.exists(fixed):
        return fixed

    # ── Build search dirs (case-insensitive dir matching) ─────
    # Include both "Images" and "images" since Linux is case-sensitive
    desktop = os.path.join(real_home, "Desktop")
    search_dirs = []
    if os.path.isdir(desktop):
        for entry in os.listdir(desktop):
            full = os.path.join(desktop, entry)
            if os.path.isdir(full) and entry.lower() in ("images", "iso", "isos", "vms"):
                search_dirs.append(full)
        search_dirs.append(desktop)
    for d in ["Downloads", "iso", "ISOs", "images", "Images"]:
        p = os.path.join(real_home, d)
        if os.path.isdir(p):
            search_dirs.append(p)
    search_dirs.append("/tmp")

    # ── Collect ALL isos across all search dirs ───────────────
    all_isos: List[str] = []
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(".iso"):
                all_isos.append(os.path.join(d, f))

    if not all_isos:
        return iso_hint  # nothing found anywhere

    # ── Step 3: keyword scoring ───────────────────────────────
    stopwords = {"iso", "the", "from", "file", "image", "images", "img",
                 "folder", "desktop", "home", "user", "and", "my", "v2",
                 "v1", "x64", "x86", "arm", "arm64", "amd64", "bit"}
    raw_words = re.split(r"[\s/\\-_.]+", iso_hint.lower())
    keywords  = [w for w in raw_words if len(w) > 2 and w not in stopwords]

    # Also pull OS-level keywords from the hint
    os_keywords = {
        "win": ["win", "windows"],
        "ubuntu": ["ubuntu"],
        "debian": ["debian"],
        "fedora": ["fedora"],
        "mint": ["mint", "linuxmint"],
        "arch": ["arch"],
        "kali": ["kali"],
        "raspios": ["raspios", "raspberry", "raspi"],
        "macos": ["macos", "osx", "darwin"],
    }
    hint_lower = iso_hint.lower()
    for key, variants in os_keywords.items():
        if any(v in hint_lower for v in variants):
            keywords += variants

    # Deduplicate keywords
    keywords = list(dict.fromkeys(keywords))

    best_match = None
    best_score = -1

    for full_path in all_isos:
        f_lower = os.path.basename(full_path).lower()
        score   = sum(1 for kw in keywords if kw in f_lower)
        # Big bonus: AI-supplied basename (minus extension) found in filename
        ai_base = os.path.basename(iso_hint).lower().replace(".iso", "").strip()
        if ai_base and len(ai_base) > 3 and ai_base in f_lower:
            score += 10
        if score > best_score:
            best_score = score
            best_match = full_path

    # Accept any match with score > 0
    if best_match and best_score > 0:
        return best_match

    # ── Step 4: return first ISO found in any search dir ─────
    # If we have ISOs but nothing matched keywords, just use the first one
    # and let the user confirm — better than silently failing
    if all_isos:
        return all_isos[0]

    return iso_hint  # absolute fallback


# ─────────────────────────────────────────────
#  TOOL EXECUTOR
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
#  HALLUCINATION SANITISER
#  Runs on every tool call before dispatch.
#  Catches and corrects the most common AI mistakes.
# ─────────────────────────────────────────────

REAL_HOME = os.path.expanduser("~")

PLACEHOLDER_VM_NAMES: set = {
    "windows-vm", "linux-vm", "ubuntu-vm", "my-vm", "vm", "myvm",
    "new-vm", "unnamed", "windows_vm", "linux_vm", "ubuntu_vm",
    "my_vm", "new_vm", "virtual-machine", "virtual_machine",
}

PLACEHOLDER_ISO_PATTERNS: set = {
    "/path/to/", "scan_isos()[0]", "<iso>", "<path>", "<file>",
    "your_iso", "your-iso", "example.iso",
}

VALID_MACHINE_TYPES: set = {
    "q35", "pc", "pc-i440fx", "microvm", "virt",
    "raspi3b", "raspi2b", "raspi0",
}

VALID_DISPLAY_MODES: set = {"sdl", "gtk", "vnc", "spice", "none", "cocoa"}
VALID_GPU_TYPES:     set = {"virtio", "qxl", "vga", "vmware", "none", "bochs"}
VALID_AUDIO_TYPES:   set = {"hda", "ich9", "ac97", "sb16", "none"}
VALID_NETWORK_MODES: set = {"nat", "bridge", "none", "user"}
VALID_DISK_FORMATS:  set = {"qcow2", "raw", "vmdk", "vdi"}
VALID_BIOS:          set = {"ovmf", "ovmf_ms", "seabios"}
VALID_MACHINE_ARCH:  set = {"x86_64", "aarch64", "arm", "armhf", "i386"}
VALID_MACHINE_CLASS: set = {"desktop", "laptop", "server", "custom", "embedded"}
VALID_OS_TYPES:      set = {"linux", "windows", "macos", "other"}

OS_TYPE_ALIASES: dict = {
    "ubuntu": "linux", "debian": "linux", "fedora": "linux",
    "mint": "linux", "arch": "linux", "kali": "linux",
    "centos": "linux", "rhel": "linux", "suse": "linux",
    "win": "windows", "win7": "windows", "win10": "windows",
    "win11": "windows", "win32": "windows", "win64": "windows",
    "windows10": "windows", "windows11": "windows", "windows7": "windows",
    "osx": "macos", "darwin": "macos", "mac": "macos", "macosx": "macos",
}

_QEMU_OUI_PREFIXES: set = {"52:54:00", "00:1a:4a"}

def _fix_path(p: str) -> str:
    """Fix hallucinated paths — wrong username, relative paths, placeholder text."""
    if not p or not isinstance(p, str):
        return p

    # ── Step 1: Check for literal code/placeholder patterns FIRST ────────────
    # Must run BEFORE username fix so scan_isos()[0] is caught before path changes
    literal_patterns = list(PLACEHOLDER_ISO_PATTERNS)
    p_lower = p.lower()
    for pat in literal_patterns:
        if pat.lower() in p_lower:
            return ""   # reject entirely — will trigger ISO scan fallback

    # ── Step 2: Fix wrong username ────────────────────────────────────────────
    # /home/user/ /home/username/ /home/anyname/ → real home
    p = re.sub(r"^/home/[^/]+/", REAL_HOME + "/", p)
    p = p.replace("~/", REAL_HOME + "/")

    # ── Step 3: Fix Windows-style paths ──────────────────────────────────────
    p = p.replace("\\", "/")
    p = p.replace("C:/", REAL_HOME + "/")
    return p

def _sanitise_args(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sanitise all args before dispatch.
    Silently corrects what it can, removes what it can't fix.
    Returns cleaned args dict.
    """
    # ── Type coercion ────────────────────────────────────────
    int_fields = {"cpu_cores","cpu_threads","memory_mb","disk_size_gb",
                  "new_size_gb","disk_index","cpu_percent","lines","vnc_port","spice_port"}
    bool_fields = {"kvm","uefi","battery","hugepages","force","delete_disks","dry_run","balloon"}

    for f in int_fields:
        if f in args and args[f] is not None:
            try:
                args[f] = int(str(args[f]).replace("GB","").replace("gb","").replace("mb","").replace("MB","").strip())
            except (ValueError, TypeError):
                args.pop(f, None)

    for f in bool_fields:
        if f in args and isinstance(args[f], str):
            args[f] = args[f].lower() in ("true","yes","1","on")

    # ── String field sanitisation ────────────────────────────
    # VM name: strip spaces, special chars
    if "name" in args and args["name"]:
        args["name"] = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(args["name"]).strip())
        # Reject clearly hallucinated/placeholder names — includes both hyphen and underscore variants
        bad_names = PLACEHOLDER_VM_NAMES
        if args["name"].lower() in bad_names:
            args["name"] = ""

    # machine_type: reject profile names used as machine type
    if "machine_type" in args and args["machine_type"]:
        mt = str(args["machine_type"]).lower().split(",")[0].strip()
        if mt not in VALID_MACHINE_TYPES and not mt.startswith("pc-"):
            # Profile name used as machine_type — auto-set profile field if not already set
            if not args.get("profile"):
                all_p = get_all_profiles()
                if mt in all_p:
                    args["profile"] = mt
                else:
                    # Try fuzzy match — e.g. "g15" matches "dell_g15_5520"
                    for pname in all_p:
                        if mt in pname or pname in mt:
                            args["profile"] = pname
                            break
            args.pop("machine_type", None)

    # Enum fields — reject invalid values, fall back to defaults
    # OS type aliases — map common hallucinated values to valid ones
    if "os_type" in args and args["os_type"]:
        alias = OS_TYPE_ALIASES.get(str(args["os_type"]).lower().strip())
        if alias:
            args["os_type"] = alias

    enum_map = {
        "display":       (VALID_DISPLAY_MODES,  "sdl"),
        "gpu":           (VALID_GPU_TYPES,       "virtio"),
        "audio":         (VALID_AUDIO_TYPES,     "hda"),
        "network_mode":  (VALID_NETWORK_MODES,   "nat"),
        "disk_format":   (VALID_DISK_FORMATS,    "qcow2"),
        "bios":          (VALID_BIOS,            "ovmf"),
        "machine_arch":  (VALID_MACHINE_ARCH,    "x86_64"),
        "machine_class": (VALID_MACHINE_CLASS,   "desktop"),
        "os_type":       (VALID_OS_TYPES,        "linux"),
    }
    for field, (valid_set, default) in enum_map.items():
        if field in args and args[field] is not None:
            val = str(args[field]).lower().strip()
            if val in valid_set:
                args[field] = val       # normalise to lowercase: NAT→nat, SDL→sdl
            else:
                args[field] = default   # invalid value — use default

    # Path fields — fix hallucinated paths
    for path_field in ("iso_path", "kernel_path", "initrd_path"):
        if path_field in args and args[path_field]:
            args[path_field] = _fix_path(str(args[path_field]))

    # raspi/ARM: force kvm=False — KVM never works for ARM guests on x86 host
    mt_lower = str(args.get("machine_type","")).lower()
    arch_lower = str(args.get("machine_arch","")).lower()
    bin_lower  = str(args.get("qemu_binary","")).lower()
    if (any(arm in mt_lower for arm in ("raspi","raspi3b","raspi2b","virt"))
            or arch_lower in ("aarch64","arm","armhf")
            or "aarch64" in bin_lower):
        args["kvm"]       = False
        args["hugepages"] = False

    # CPU model: reject ARM cpu models on x86 VMs
    if "cpu_model" in args and args["cpu_model"]:
        cpu = str(args["cpu_model"]).lower().strip()
        arm_cpus = {"cortex-a15","cortex-a53","cortex-a57","cortex-a72","cortex-a76",
                    "arm1176","arm926","cortex-m","cortex-r"}
        arch = str(args.get("machine_arch","x86_64")).lower()
        if any(arm in cpu for arm in arm_cpus) and arch == "x86_64":
            args["cpu_model"] = "host"  # fix silently

    # MAC address: validate format — must be exactly 6 octets
    if "mac_address" in args and args["mac_address"]:
        mac = str(args["mac_address"]).strip()
        if not re.match(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$", mac):
            args.pop("mac_address", None)  # invalid MAC — let system generate one

    # Bridge interface: reject raw ethernet/wifi interfaces, require a bridge
    if "bridge_iface" in args and args["bridge_iface"]:
        br = str(args["bridge_iface"]).strip()
        bad_ifaces = {"bridge","br","network","<bridge>","your_bridge","",
                      "eth0","eth1","ens33","ens3","enp","wlan0","wlan1","wifi0"}
        # Also reject any interface starting with eth/ens/enp/wlan (not a bridge)
        is_raw_iface = any(br.startswith(p) for p in ("eth","ens","enp","wlan","wlp"))
        if br in bad_ifaces or is_raw_iface:
            args["bridge_iface"] = "virbr0"  # safe default — use libvirt bridge

    # Memory sanity: cap at 90% of host RAM, minimum 512MB
    if "memory_mb" in args and args["memory_mb"]:
        try:
            host_mb = 0
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal"):
                        host_mb = int(line.split()[1]) // 1024
                        break
            # Only cap if truly excessive (>95% host RAM)
            # Allow reasonable over-commit — QEMU uses balloon to manage
            max_allowed = max(int(host_mb * 0.95), 4096)
            args["memory_mb"] = max(512, min(args["memory_mb"], max_allowed))
        except Exception:
            args["memory_mb"] = max(512, args["memory_mb"])

    # CPU cores: cap at host core count
    if "cpu_cores" in args and args["cpu_cores"]:
        try:
            import psutil
            host_cores = psutil.cpu_count(logical=True)
            args["cpu_cores"] = max(1, min(args["cpu_cores"], host_cores))
        except Exception:
            args["cpu_cores"] = max(1, args["cpu_cores"])

    # Disk size: minimum 8GB, maximum 2TB
    if "disk_size_gb" in args and args["disk_size_gb"]:
        args["disk_size_gb"] = max(8, min(int(args["disk_size_gb"]), 2048))

    # Port numbers: valid range
    for port_field in ("vnc_port", "spice_port"):
        if port_field in args and args[port_field]:
            p = int(args[port_field])
            if not (1024 <= p <= 65535):
                args.pop(port_field, None)

    # extra_args: must be a list of strings, reject anything that looks dangerous
    if "extra_args" in args and args["extra_args"]:
        if not isinstance(args["extra_args"], list):
            args["extra_args"] = []
        else:
            # Filter out non-strings and suspiciously long args
            args["extra_args"] = [
                str(a) for a in args["extra_args"]
                if isinstance(a, (str, int)) and len(str(a)) < 500
            ]

    # snap_name / snapshot names: alphanumeric only
    for snap_field in ("snap_name", "snapshot_name"):
        if snap_field in args and args[snap_field]:
            args[snap_field] = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(args[snap_field]))

    # net_name / network names
    if "net_name" in args and args["net_name"]:
        args["net_name"] = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(args["net_name"]))

    # profile_name: alphanumeric + underscore
    if "profile_name" in args and args["profile_name"]:
        args["profile_name"] = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(args["profile_name"]).lower())

    # os_name / description: strip any path-like content
    for text_field in ("os_name", "description", "hostname"):
        if text_field in args and args[text_field]:
            # Strip anything that looks like a path or command
            val = str(args[text_field])
            if "/" in val or chr(92) in val or "`" in val or "$(" in val:
                args[text_field] = re.sub(r"[/\\`$()]", "", val)

    # Remove None values and empty strings for optional fields
    optional_removable = {"bridge_iface","mac_address","iso_path","kernel_path",
                          "initrd_path","bios_version","bios_vendor","serial_number",
                          "product_name","manufacturer","hostname"}
    for f in optional_removable:
        if f in args and (args[f] is None or args[f] == ""):
            args.pop(f, None)

    return args


def execute_tool(tool_name: str, args: Dict[str, Any], verbose: bool = False) -> Any:
    # ── Sanitise all args first ──────────────────────────────
    args = _sanitise_args(tool_name, args)

    # Resolve VM names
    if "name" in args and tool_name not in ("create_vm","create_profile","clone_vm","create_network"):
        vms      = manager.list_vms()
        resolved = _resolve_vm_name(vms, str(args["name"]))
        if resolved:
            args["name"] = resolved

    # ── clarify ──
    if tool_name == "clarify":
        return {"clarify": True, "question": args.get("question",""), "options": args.get("options",[])}

    # ── system ──
    elif tool_name == "check_system":
        caps = check_system_capabilities()
        caps["ovmf_paths"] = OVMF
        if not verbose:
            _render_system(caps)
        return caps

    elif tool_name == "scan_isos":
        return manager.scan_isos()

    elif tool_name == "list_vms":
        vms = manager.list_vms()
        if not verbose:
            _render_vm_list(vms)
        return vms

    elif tool_name == "list_profiles":
        profiles = list_profiles()
        if not verbose:
            _render_profiles(profiles)
        return profiles

    elif tool_name == "check_profile_compatibility":
        result = check_profile_compatibility(args["profile_name"])
        if not verbose:
            _render_compat(result)
        return result

    elif tool_name == "create_profile":
        pname = args.pop("profile_name")
        notes = args.pop("notes", "")
        if notes: args["_notes"] = notes
        result = save_custom_profile(pname, args)
        if result["success"]:
            result["compatibility"] = check_profile_compatibility(result["profile_name"])
        return result

    elif tool_name == "delete_profile":
        return delete_custom_profile(args["profile_name"])

    elif tool_name == "create_vm":
        raw_name = args.get("name","") or ""
        name = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(raw_name).strip())
        # Reject placeholder/empty/generic names — these are hallucinations
        placeholder_names = PLACEHOLDER_VM_NAMES
        if not name or name.lower() in placeholder_names:
            return {
                "success": False,
                "clarify": True,
                "question": "What would you like to name this VM?",
                "options": ["my-windows-vm", "dev-machine", "test-ubuntu"],
                "error": "VM name is required — please provide a unique name.",
                "needs_clarification": "name"
            }
        args["name"] = name

        cfg = MachineConfig(
            name=name,
            os_type=args.get("os_type","linux"),
            os_name=args.get("os_name",""),
            description=args.get("description",""),
        )
        profile = args.get("profile")
        if not profile:
            product = (args.get("product_name","") + " " + args.get("manufacturer","")).lower()
            for pname, pdata in get_all_profiles().items():
                pp = (pdata.get("product_name","") + " " + pdata.get("manufacturer","")).lower()
                if any(kw in pp for kw in product.split() if len(kw) > 3):
                    profile = pname
                    break
        if profile:
            try: cfg = apply_profile(cfg, profile)
            except ValueError as e: return {"success": False, "error": str(e)}

        for f in ("machine_class","cpu_model","cpu_cores","cpu_threads","memory_mb",
                  "display","gpu","audio","manufacturer","product_name","bios_version",
                  "uefi","kvm","battery","hugepages","machine_type","os_type","os_name"):
            if f in args and args[f] is not None and args[f] != "":
                setattr(cfg, f, args[f])

        if args.get("extra_args"):
            cfg.extra_args = args["extra_args"]

        # Validate machine_type — reject profile names used by mistake
        valid_machine_types = {"q35", "pc", "pc-i440fx", "microvm", "virt",
                               "raspi3b", "raspi2b", "raspi0"}
        if cfg.machine_type:
            mt = cfg.machine_type.lower().split(",")[0].strip()
            if mt not in valid_machine_types and not mt.startswith("pc-"):
                # Profile name used as machine type — fix it
                cfg.machine_type = "q35"  # safe default for x86

        # Windows 11 MUST have UEFI + q35 — enforce regardless of what AI sent
        if "windows" in cfg.os_type.lower() or "windows" in cfg.os_name.lower():
            cfg.uefi = True
            cfg.bios = "ovmf"
            if cfg.machine_type not in ("q35",):
                cfg.machine_type = "q35"

        # Fix ARM cpu_model on x86 VM (second safety net after sanitiser)
        arm_cpu_prefixes = ("cortex", "arm1", "arm9", "arm11")
        if cfg.machine_arch == "x86_64" and any(
            cfg.cpu_model.lower().startswith(p) for p in arm_cpu_prefixes
        ):
            cfg.cpu_model = "host"

        # Auto-detect architecture from ISO filename
        iso_hint = args.get("iso_path", "")
        if iso_hint:
            iso_lower = os.path.basename(iso_hint).lower()
            if any(k in iso_lower for k in ("arm64","aarch64","_arm_","arm_v")):
                # ARM64 ISO detected — auto-set correct arch
                cfg.machine_arch  = "aarch64"
                cfg.qemu_binary   = "qemu-system-aarch64"
                cfg.kvm           = False
                cfg.machine_type  = cfg.machine_type if cfg.machine_type in ("virt","raspi3b") else "virt"
                cfg.bios          = "seabios"
                cfg.uefi          = False
                cfg.hugepages     = False
                if not verbose:
                    console.print(f"  [yellow]⚠ ARM64 ISO detected — switched to aarch64 VM[/yellow]")

        # Detect ISO/VM architecture mismatch before creating
        iso_hint = args.get("iso_path", "")
        if iso_hint:
            iso_lower = os.path.basename(iso_hint).lower()
            is_iso_arm = any(k in iso_lower for k in ("arm64", "aarch64", "arm_", "_arm"))
            is_iso_x86 = any(k in iso_lower for k in ("amd64", "x86_64", "x64", "i386", "i686"))
            if is_iso_arm and cfg.machine_arch == "x86_64":
                return {
                    "success": False,
                    "error": (
                        f"Architecture mismatch — '{os.path.basename(iso_hint)}' is an ARM64 ISO "
                        f"but this VM is x86_64. "
                        f"Either use an x86_64 Windows 11 ISO or create an aarch64 VM. "
                        f"Download x86 ISO: https://www.microsoft.com/software-download/windows11"
                    )
                }
            if is_iso_x86 and cfg.machine_arch in ("aarch64", "arm"):
                return {
                    "success": False,
                    "error": (
                        f"Architecture mismatch — '{os.path.basename(iso_hint)}' is an x86_64 ISO "
                        f"but this VM is ARM. Use an ARM64 ISO instead."
                    )
                }

        disk_size   = int(args.get("disk_size_gb", 60))
        disk_format = args.get("disk_format", "qcow2")
        disk_path   = os.path.expanduser(f"~/.qemu_vms/{cfg.name}/disk0.{disk_format}")
        cfg.disks   = [DiskConfig(path=disk_path, size_gb=disk_size, format=disk_format)]

        net = NetworkConfig(
            mode=args.get("network_mode","nat"),
            bridge=args.get("bridge_iface","virbr0") or "virbr0",
        )
        if args.get("mac_address"): net.mac = args["mac_address"]
        cfg.networks = [net]

        if args.get("iso_path"):
            cfg.iso_path = _resolve_iso(args["iso_path"])
        if cfg.machine_class == "laptop" or args.get("battery"):
            cfg.battery = True
        if "windows" in cfg.os_type.lower() and not profile:
            cfg.bios = "ovmf"; cfg.uefi = True

        return manager.create_vm(cfg)

    elif tool_name == "clone_vm":
        return manager.clone_vm(args["source_name"], args["new_name"])

    elif tool_name == "launch_vm":
        return manager.launch_vm(args["name"], display=args.get("display"), dry_run=args.get("dry_run", False))

    elif tool_name == "stop_vm":
        if args["name"] == "all":
            return manager.stop_all()
        return manager.stop_vm(args["name"], force=args.get("force", False))

    elif tool_name == "vm_status":
        result = manager.vm_status(args["name"])
        if not verbose:
            _render_status(result)
        return result

    elif tool_name == "monitor_vm":
        if args["name"] == "all":
            result = manager.monitor_all()
            if not verbose:
                for r in result.values():
                    _render_monitor(r)
            return result
        result = manager.monitor_vm(args["name"])
        if not verbose:
            _render_monitor(result)
        return result

    elif tool_name == "show_config":
        return manager.show_config(args["name"])

    elif tool_name == "update_config":
        return manager.update_config(args["name"], args.get("updates", {}))

    elif tool_name == "resize_disk":
        return manager.resize_disk(args["name"], args.get("disk_index", 0), args["new_size_gb"])

    elif tool_name == "snapshot_create":
        return manager.snapshot_create(args["name"], args.get("snap_name","snap1"))

    elif tool_name == "snapshot_list":
        result = manager.snapshot_list(args["name"])
        if not verbose:
            _render_snapshots(result)
        return result

    elif tool_name == "snapshot_restore":
        return manager.snapshot_restore(args["name"], args["snap_name"])

    elif tool_name == "snapshot_delete":
        return manager.snapshot_delete(args["name"], args["snap_name"])

    elif tool_name == "set_resource_limits":
        return manager.set_resource_limits(
            args["name"],
            cpu_percent=args.get("cpu_percent"),
            memory_mb=args.get("memory_mb"),
        )

    elif tool_name == "create_network":
        return manager.create_network(args["net_name"])

    elif tool_name == "delete_network":
        return manager.delete_network(args["net_name"])

    elif tool_name == "list_networks":
        return manager.list_networks()

    elif tool_name == "add_vm_to_network":
        return manager.add_vm_to_network(args["net_name"], args["vm_name"])

    elif tool_name == "open_display":
        return manager.open_display(args["name"])

    elif tool_name == "open_shell":
        return manager.open_shell(args["name"])

    elif tool_name == "delete_vm":
        return manager.delete_vm(args["name"], delete_disks=args.get("delete_disks", False))

    elif tool_name == "get_vm_logs":
        result = manager.get_vm_logs(args["name"], lines=int(args.get("lines", 50)))
        if not verbose:
            _render_vm_failure(result)
        return result

    elif tool_name == "print_command":
        result = manager.print_command(args["name"])
        if result.get("success") and not verbose:
            console.print(Panel(result["command"], title="QEMU Command", border_style="cyan"))
        return result

    elif tool_name == "send_monitor_cmd":
        return manager.send_monitor_cmd(args["name"], args.get("cmd","info status"))

    else:
        return {"success": False, "error": f"Unknown tool: {tool_name}"}


# ─────────────────────────────────────────────
#  OLLAMA
# ─────────────────────────────────────────────

def _call_ollama(messages: List[Dict]) -> Dict:
    payload = {
        "model":   OLLAMA_MODEL,
        "messages": messages,
        "tools":   TOOLS,
        "stream":  False,
        "options": {"temperature": 0.1, "num_ctx": 8192},
    }
    try:
        resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=120)
        resp.raise_for_status()
        return resp.json()
    except requests.ConnectionError:
        console.print(Panel(
            f"[error]Cannot connect to Ollama at {OLLAMA_URL}[/error]\n"
            f"  → Start: [bold]ollama serve[/bold]\n"
            f"  → Pull:  [bold]ollama pull {OLLAMA_MODEL}[/bold]",
            border_style="red"
        ))
        sys.exit(1)


def _build_system_prompt() -> str:
    profiles     = [p["name"] for p in list_profiles()]
    ovmf_status  = "AVAILABLE" if OVMF["available"] else "NOT FOUND (SeaBIOS fallback active)"
    model        = OLLAMA_MODEL.lower()
    custom_note  = "\nCUSTOM MODE ACTIVE (-cu): product_name and manufacturer can be any fictional values. Skip all warnings about unverifiable hardware." if _CUSTOM_MODE else ""
    return f"""You are an expert KVM/QEMU virtual machine assistant running on Linux Mint.
You manage virtual machines using QEMU/KVM. Respond concisely and use tools immediately.{custom_note}
You help the user create, launch, monitor, and manage QEMU/KVM virtual machines.

SYSTEM: OVMF={ovmf_status} | Profiles={profiles}

═══ CRITICAL: ACT vs ASK ═══
For clear requests, call the tool IMMEDIATELY. Do not ask for confirmation or missing optional info.
Examples of when to ACT without asking:
  "create a Windows 11 VM called win11" → call create_vm right now
  "create a VM called X with NAT"       → call create_vm right now
  "list my VMs"                         → call list_vms right now
  "launch X"                            → call launch_vm right now
Only use the clarify tool if the VM NAME is completely absent from the user's message.

═══ DEFAULTS (never ask for these) ═══
display=sdl | disk=60GB qcow2 | network=nat | kvm=true | cpu=host
Windows → uefi=true + bios=ovmf + machine_type=q35 (always)
Linux   → machine_type=q35
ARM/Pi  → kvm=false + qemu_binary=qemu-system-aarch64 + machine_type=virt

═══ RULES ═══
1. NAME: Only use a name the user explicitly said. Never invent "windows-vm", "linux-vm" etc.
   If name is missing, call clarify ONCE. If name is given, call create_vm immediately.

2. MACHINE TYPE: Only valid values: q35, pc, pc-i440fx, microvm, virt, raspi3b.
   Profile names (office_laptop, dell_g15_5520) go in the "profile" field, NOT machine_type.

3. CPU: x86_64 VMs: host/kvm64/Haswell/Skylake/IceLake/EPYC only. NEVER cortex-*/arm*.
   aarch64 VMs: cortex-a72/cortex-a53 etc.

4. ISO: call scan_isos FIRST when user mentions any ISO or OS to install.
   Use exact path from scan_isos. NEVER construct /home/user/... or /path/to/... paths.
   ARM64 ISO filename (arm64/Arm64/aarch64) → auto-set machine_arch=aarch64.

5. MULTI-STEP: "create and launch" → call create_vm then launch_vm (two tool calls, no pause).

6. FAILURE: "why did it fail" or VM stopped → call get_vm_logs immediately.

7. DELETE: "delete/kill/remove VM" → call delete_vm with delete_disks=true.

8. BRIDGE: bridge_iface must be a bridge (virbr0, br0). Never use eth0/ens33/wlan0.

9. RESPONSES: 1-2 sentences max. UI already shows tables. Say "Done — X is running."

10. PROFILES: Match real device names to profiles (Dell G15 → dell_g15_5520).
    Raspi3b → serial console only, no display, kvm=false.
    Always check_profile_compatibility for ARM/raspi before creating.
"""


# ─────────────────────────────────────────────
#  PRE-FLIGHT VALIDATOR
#  Runs BEFORE execute_tool on every tool call.
#  Returns one of three actions:
#    "ok"        — proceed normally
#    "auto_fix"  — silently correct and proceed
#    "ask_user"  — pause and ask the user
#    "abort"     — block and tell the AI to retry
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
#  INTERNET VALIDATOR
#  Cross-checks AI assumptions against real-world
#  data. Uses a local cache to avoid repeat fetches.
#  All network calls are non-blocking with short
#  timeouts — if offline, gracefully skips.
# ─────────────────────────────────────────────

import hashlib
import urllib.request
import urllib.parse
import urllib.error

_NET_CACHE: Dict[str, Any] = {}          # in-memory cache for this session
_NET_TIMEOUT  = 4                         # seconds per request
_NET_ENABLED  = True                      # set False to disable all net checks
_CUSTOM_MODE  = False                     # set True via -cu to skip product verification


def _net_get(url: str, headers: Dict = None) -> Optional[Dict]:
    """Fetch JSON from a URL with caching and timeout. Returns None on failure."""
    if not _NET_ENABLED:
        return None
    cache_key = hashlib.md5(url.encode()).hexdigest()
    if cache_key in _NET_CACHE:
        return _NET_CACHE[cache_key]
    try:
        req = urllib.request.Request(url, headers=headers or {
            "User-Agent": "qemu-api/1.0 (pre-flight validator)"
        })
        with urllib.request.urlopen(req, timeout=_NET_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            _NET_CACHE[cache_key] = data
            return data
    except Exception:
        _NET_CACHE[cache_key] = None
        return None


def _net_head(url: str) -> bool:
    """Check if a URL exists (HEAD request). Returns False on failure."""
    if not _NET_ENABLED:
        return False
    try:
        req = urllib.request.Request(url, method="HEAD", headers={
            "User-Agent": "qemu-api/1.0"
        })
        with urllib.request.urlopen(req, timeout=_NET_TIMEOUT):
            return True
    except Exception:
        return False


# ── Query QEMU locally for supported machine types and CPUs ───────────────────

_QEMU_MACHINES_CACHE: Optional[set] = None
_QEMU_CPUS_CACHE:     Optional[set] = None


def _get_qemu_machine_types(binary: str = "qemu-system-x86_64") -> set:
    """Ask the local QEMU binary what machine types it supports."""
    global _QEMU_MACHINES_CACHE
    if _QEMU_MACHINES_CACHE is not None:
        return _QEMU_MACHINES_CACHE
    try:
        import subprocess
        result = subprocess.run(
            [binary, "-machine", "help"],
            capture_output=True, text=True, timeout=5
        )
        machines = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            if parts:
                machines.add(parts[0].lower().rstrip(","))
        _QEMU_MACHINES_CACHE = machines
        return machines
    except Exception:
        return set()


def _get_qemu_cpu_models(binary: str = "qemu-system-x86_64") -> set:
    """Ask the local QEMU binary what CPU models it supports."""
    global _QEMU_CPUS_CACHE
    if _QEMU_CPUS_CACHE is not None:
        return _QEMU_CPUS_CACHE
    try:
        import subprocess
        result = subprocess.run(
            [binary, "-cpu", "help"],
            capture_output=True, text=True, timeout=5
        )
        cpus = set()
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if parts and not parts[0].startswith("-"):
                cpus.add(parts[0].lower())
        _QEMU_CPUS_CACHE = cpus
        return cpus
    except Exception:
        return set()


# ── Product lookup via DuckDuckGo Instant Answer API ─────────────────────────

def _lookup_product(manufacturer: str, product: str) -> Dict[str, Any]:
    """
    Use DuckDuckGo Instant Answer API to verify a product exists
    and gather key specs. Returns {} if not found or offline.
    """
    query  = f"{manufacturer} {product} laptop desktop specifications"
    params = urllib.parse.urlencode({"q": query, "format": "json", "no_html": "1"})
    url    = f"https://api.duckduckgo.com/?{params}"
    data   = _net_get(url)
    if not data:
        return {}
    return {
        "found":   bool(data.get("AbstractText") or data.get("Answer")),
        "summary": (data.get("AbstractText") or data.get("Answer") or "")[:300],
        "source":  data.get("AbstractSource", ""),
    }


# ── CPU architecture lookup via WikiData ──────────────────────────────────────

# Known ARM CPU prefixes — local lookup, no network needed
_ARM_CPU_PREFIXES = (
    "cortex-a", "cortex-m", "cortex-r",
    "arm1", "arm7", "arm9", "arm11",
    "neoverse", "ampere", "apple m",
    "qualcomm", "snapdragon",
)

_X86_CPU_NAMES = {
    "haswell", "broadwell", "skylake", "kabylake", "coffeelake",
    "cannonlake", "icelake", "tigerlake", "alderlake", "raptorlake",
    "sandybridge", "ivybridge", "westmere", "nehalem", "penryn",
    "opteron", "epyc", "zen", "zen2", "zen3", "zen4",
    "kvm64", "host", "qemu64", "qemu32",
}


def _is_arm_cpu(cpu_model: str) -> bool:
    lower = cpu_model.lower()
    return any(lower.startswith(p) for p in _ARM_CPU_PREFIXES)


def _is_x86_cpu(cpu_model: str) -> bool:
    lower = cpu_model.lower().replace("-", "").replace("_", "")
    return any(x86 in lower for x86 in _X86_CPU_NAMES)


# ── Windows ISO verification ──────────────────────────────────────────────────

# Microsoft's official ISO landing page — we just check if it's reachable
_MS_WINDOWS_ISO_PAGE = "https://www.microsoft.com/software-download/windows11"


def _check_windows_iso_url() -> bool:
    """Verify Microsoft's ISO download page is reachable."""
    return _net_head(_MS_WINDOWS_ISO_PAGE)


# ── Main internet validation function ────────────────────────────────────────

def _validate_with_internet(args: Dict[str, Any], verbose: bool = False) -> List[Dict]:
    """
    Cross-check AI-provided hardware assumptions against real-world data.
    Called once per create_vm, non-blocking — all failures are warnings not errors.
    Returns list of issues in same format as _validate_profile_for_host.
    """
    issues = []
    qemu_binary = args.get("qemu_binary", "qemu-system-x86_64")

    # ── 1. Machine type — check against installed QEMU ────────────────────────
    machine_type = str(args.get("machine_type", "q35")).lower().split(",")[0].strip()
    if machine_type and machine_type not in ("", "none"):
        supported_machines = _get_qemu_machine_types(qemu_binary)
        if supported_machines and machine_type not in supported_machines:
            # Suggest closest match
            close = [m for m in supported_machines if machine_type[:3] in m][:3]
            issues.append({
                "severity":  "error",
                "message":   f"Machine type '{machine_type}' is not supported by your installed QEMU",
                "fix":       f"Supported types include: {close or list(supported_machines)[:5]}",
                "auto_fix":  False,
                "source":    "local_qemu",
            })

    # ── 2. CPU model — check against installed QEMU ───────────────────────────
    cpu_model = str(args.get("cpu_model", "host")).strip()
    if cpu_model and cpu_model not in ("host", "kvm64", "qemu64", "max"):
        supported_cpus = _get_qemu_cpu_models(qemu_binary)
        if supported_cpus:
            cpu_lower = cpu_model.lower()
            # Check exact match or if it's a named model QEMU knows
            if cpu_lower not in supported_cpus and not _is_arm_cpu(cpu_model):
                # Only warn — user might be using an alias or variant
                close = [c for c in supported_cpus if cpu_lower[:4] in c][:3]
                if not close:
                    issues.append({
                        "severity":  "warning",
                        "message":   f"CPU model '{cpu_model}' not found in QEMU's cpu list",
                        "fix":       f"Try: host, kvm64, or a named model. Run: qemu-system-x86_64 -cpu help",
                        "auto_fix":  True,
                        "fix_field": "cpu_model",
                        "fix_value": "host",
                        "source":    "local_qemu",
                    })

    # ── 3. CPU arch consistency ───────────────────────────────────────────────
    machine_arch = str(args.get("machine_arch", "x86_64")).lower()
    if _is_arm_cpu(cpu_model) and machine_arch == "x86_64":
        issues.append({
            "severity":  "error",
            "message":   f"CPU '{cpu_model}' is an ARM processor but VM arch is x86_64",
            "fix":       "Either use an x86 CPU model or set machine_arch=aarch64",
            "auto_fix":  True,
            "fix_field": "cpu_model",
            "fix_value": "host",
            "source":    "local_knowledge",
        })
    elif _is_x86_cpu(cpu_model) and machine_arch in ("aarch64", "arm"):
        issues.append({
            "severity":  "error",
            "message":   f"CPU '{cpu_model}' is an x86 processor but VM arch is {machine_arch}",
            "fix":       "Either use an ARM CPU model (cortex-a72) or set machine_arch=x86_64",
            "auto_fix":  True,
            "fix_field": "cpu_model",
            "fix_value": "cortex-a72",
            "source":    "local_knowledge",
        })

    # ── 4. Product name / manufacturer cross-check ───────────────────────────
    manufacturer = str(args.get("manufacturer", "")).strip()
    product_name = str(args.get("product_name", "")).strip()

    if manufacturer and product_name and _NET_ENABLED and not _CUSTOM_MODE:
        result = _lookup_product(manufacturer, product_name)
        if result and not result.get("found"):
            issues.append({
                "severity":  "warning",
                "message":   f"Could not verify '{manufacturer} {product_name}' as a real product online",
                "fix":       "Check manufacturer and product_name — SMBIOS spoofing works best with real product names",
                "auto_fix":  False,
                "source":    "duckduckgo",
            })
        elif result and result.get("found") and verbose:
            console.print(f"  [dim]✓ Product verified: {result['summary'][:80]}[/dim]")
    elif _CUSTOM_MODE and manufacturer and product_name and verbose:
        console.print(f"  [dim]⚙ Custom mode — skipping product verification for '{manufacturer} {product_name}'[/dim]")

    # ── 5. Memory sanity vs known product specs ───────────────────────────────
    memory_mb = int(args.get("memory_mb", 0))
    if memory_mb and product_name and not _CUSTOM_MODE:
        prod_lower = product_name.lower()
        if "g15" in prod_lower or "thinkpad" in prod_lower or "inspiron" in prod_lower:
            if memory_mb > 65536:
                issues.append({
                    "severity":  "warning",
                    "message":   f"'{product_name}' typically supports max 32-64GB RAM, got {memory_mb//1024}GB",
                    "fix":       "Reduce memory_mb to match the actual product's maximum",
                    "auto_fix":  False,
                    "source":    "local_knowledge",
                })

    # ── 6. Windows ISO architecture hint ─────────────────────────────────────
    os_type = str(args.get("os_type", "")).lower()
    iso_path = str(args.get("iso_path", ""))
    if ("windows" in os_type or "win" in os_type) and iso_path:
        iso_lower = os.path.basename(iso_path).lower()
        is_arm_iso = any(k in iso_lower for k in ("arm64","aarch64","arm_"))
        is_x86_iso = any(k in iso_lower for k in ("x64","amd64","x86_64"))
        if is_arm_iso and machine_arch == "x86_64":
            issues.append({
                "severity": "error",
                "message":  f"ARM64 Windows ISO with x86_64 VM — will not boot",
                "fix":      f"Get x86_64 ISO from: {_MS_WINDOWS_ISO_PAGE}",
                "auto_fix": False,
                "source":   "iso_filename",
            })

    return issues


def _validate_profile_for_host(profile_name: str) -> List[Dict[str, Any]]:
    """
    Validate any profile (built-in or custom) against the current host.
    Returns a list of issues: {"severity": "error"|"warning", "message": ...,
                               "fix": ..., "auto_fix": bool,
                               "fix_field": ..., "fix_value": ...}
    Called automatically for every create_vm that uses a profile.
    """
    from qemu_config import check_system_capabilities, OVMF, get_all_profiles
    import shutil

    issues = []
    all_profiles = get_all_profiles()
    profile = all_profiles.get(profile_name)

    if not profile:
        # Unknown profile — not necessarily an error, might be set later
        return []

    caps = check_system_capabilities()

    # ── Architecture / binary check ───────────────────────────────────────────
    arch   = profile.get("machine_arch", "x86_64")
    binary = profile.get("qemu_binary", "qemu-system-x86_64")

    if arch in ("aarch64", "arm") and not caps.get("qemu_arm_installed"):
        issues.append({
            "severity": "error",
            "message":  f"Profile '{profile_name}' needs qemu-system-aarch64 which is not installed",
            "fix":      "sudo apt install qemu-system-arm",
            "auto_fix": False,
        })

    if binary and not shutil.which(binary):
        issues.append({
            "severity": "error",
            "message":  f"Required QEMU binary '{binary}' not found on this system",
            "fix":      f"sudo apt install {'qemu-system-arm' if 'aarch64' in binary else 'qemu-system-x86'}",
            "auto_fix": False,
        })

    # ── KVM on wrong arch ─────────────────────────────────────────────────────
    profile_kvm  = profile.get("kvm", True)
    host_arch    = caps.get("host_arch", "x86_64")
    if profile_kvm and arch in ("aarch64", "arm") and host_arch == "x86_64":
        issues.append({
            "severity":  "warning",
            "message":   f"Profile '{profile_name}' has kvm=True but ARM guests can't use KVM on x86 host",
            "fix":       "kvm will be forced to False automatically",
            "auto_fix":  True,
            "fix_field": "kvm",
            "fix_value": False,
        })

    # ── UEFI / OVMF check ────────────────────────────────────────────────────
    if profile.get("uefi") and not OVMF["available"]:
        bios = profile.get("bios", "ovmf")
        if bios in ("ovmf", "ovmf_ms"):
            issues.append({
                "severity":  "warning",
                "message":   f"Profile '{profile_name}' requires UEFI but OVMF firmware not found",
                "fix":       "sudo apt install ovmf — or VM will fall back to SeaBIOS automatically",
                "auto_fix":  True,
                "fix_field": "bios",
                "fix_value": "seabios",
            })

    # ── Hugepages check ───────────────────────────────────────────────────────
    if profile.get("hugepages"):
        try:
            with open("/proc/sys/vm/nr_hugepages") as f:
                nr = int(f.read().strip())
            if nr == 0:
                issues.append({
                    "severity":  "error",
                    "message":   f"Profile '{profile_name}' uses hugepages but none are allocated (nr_hugepages=0)",
                    "fix":       "sudo sysctl vm.nr_hugepages=2048  (or disable hugepages in profile)",
                    "auto_fix":  True,
                    "fix_field": "hugepages",
                    "fix_value": False,
                })
        except Exception:
            pass

    # ── Memory check ─────────────────────────────────────────────────────────
    profile_mem = int(profile.get("memory_mb", 2048))
    host_mem    = caps.get("host_memory_mb", 0)
    if host_mem > 0 and profile_mem > host_mem:
        issues.append({
            "severity":  "warning",
            "message":   f"Profile requests {profile_mem}MB RAM but host only has {host_mem}MB",
            "fix":       f"Reduce memory_mb to {host_mem // 2} or less",
            "auto_fix":  True,
            "fix_field": "memory_mb",
            "fix_value": min(profile_mem, int(host_mem * 0.85)),
        })

    # ── CPU core check ────────────────────────────────────────────────────────
    profile_cores = int(profile.get("cpu_cores", 2))
    host_cores    = caps.get("host_cpu_cores", 1)
    if profile_cores > host_cores * 2:
        issues.append({
            "severity":  "warning",
            "message":   f"Profile requests {profile_cores} cores but host only has {host_cores} — heavy over-commit",
            "fix":       f"Reduce cpu_cores to {host_cores} or less",
            "auto_fix":  True,
            "fix_field": "cpu_cores",
            "fix_value": host_cores,
        })

    # ── Disk space check ──────────────────────────────────────────────────────
    free_gb = caps.get("home_free_gb", 999)
    # Estimate disk usage from profile (default 60GB if not specified)
    est_disk = 60
    if free_gb < 10:
        issues.append({
            "severity": "error",
            "message":  f"Only {free_gb}GB free in home directory — may not have space for VM disk image",
            "fix":      "Free up disk space before creating the VM",
            "auto_fix": False,
        })
    elif free_gb < est_disk:
        issues.append({
            "severity": "warning",
            "message":  f"Only {free_gb}GB free — VM disk image may exceed available space",
            "fix":      "Use a smaller disk_size_gb or free up space",
            "auto_fix": False,
        })

    # ── Machine type vs binary mismatch ──────────────────────────────────────
    mt = profile.get("machine_type", "q35")
    if "raspi" in mt and "aarch64" not in binary:
        issues.append({
            "severity":  "error",
            "message":   f"Profile '{profile_name}' uses raspi machine type but qemu_binary is not aarch64",
            "fix":       "Set qemu_binary=qemu-system-aarch64 in the profile",
            "auto_fix":  True,
            "fix_field": "qemu_binary",
            "fix_value": "qemu-system-aarch64",
        })

    # ── Profile-specific notes ────────────────────────────────────────────────
    notes = profile.get("_notes", "")
    if notes and "slow" in notes.lower():
        issues.append({
            "severity": "warning",
            "message":  f"Profile note: {notes}",
            "fix":      "",
            "auto_fix": False,
        })

    # ── Custom profile extra checks ───────────────────────────────────────────
    if profile.get("_custom"):
        # Check that cpu_model is valid for the arch
        cpu_model = profile.get("cpu_model", "host")
        arm_prefixes = ("cortex", "arm1", "arm9", "arm11")
        is_arm_cpu  = any(cpu_model.lower().startswith(p) for p in arm_prefixes)
        if is_arm_cpu and arch == "x86_64":
            issues.append({
                "severity":  "error",
                "message":   f"Custom profile '{profile_name}' has ARM cpu_model='{cpu_model}' but machine_arch=x86_64",
                "fix":       f"Change cpu_model to 'host' or set machine_arch=aarch64",
                "auto_fix":  True,
                "fix_field": "cpu_model",
                "fix_value": "host",
            })

        # Check that required fields are present
        required_for_smbios = ["manufacturer", "product_name"]
        missing = [f for f in required_for_smbios if not profile.get(f)]
        if missing:
            issues.append({
                "severity": "warning",
                "message":  f"Custom profile '{profile_name}' is missing SMBIOS fields: {missing}",
                "fix":      "Add manufacturer and product_name for better hardware spoofing",
                "auto_fix": False,
            })

    return issues


def _preflight_check(
    tool_name: str,
    args: Dict[str, Any],
    messages: List[Dict],
    verbose: bool = False,
) -> Dict[str, Any]:
    """
    Validate tool call args before execution.
    Returns {"action": ..., "reason": ..., "correction": ..., "fixed_args": ...}
    """
    ok = {"action": "ok"}

    # ── Only validate tools that touch real resources ──────────
    if tool_name not in (
        "create_vm", "launch_vm", "delete_vm", "resize_disk",
        "clone_vm", "snapshot_restore", "snapshot_delete",
        "set_resource_limits", "send_monitor_cmd",
    ):
        return ok

    # ── create_vm checks ───────────────────────────────────────
    if tool_name == "create_vm":
        name     = str(args.get("name", "")).strip()
        iso_path = str(args.get("iso_path", "")).strip()
        os_type  = str(args.get("os_type", "")).lower()
        mt       = str(args.get("machine_type", "")).lower()

        # 1. Name missing or is a placeholder
        placeholder_names = PLACEHOLDER_VM_NAMES
        if not name or name.lower() in placeholder_names:
            return {
                "action":    "ask_user",
                "reason":    f"VM name is missing or looks invented (got: '{name}')",
                "question":  "What would you like to name this VM?",
                "fix_field": "name",
                "options":   ["my-windows-vm", "dev-box", "test-ubuntu"],
            }

        # 2. VM already exists
        vm_dir = os.path.join(os.path.expanduser("~"), ".qemu_vms", name)
        if os.path.exists(vm_dir):
            return {
                "action":     "ask_user",
                "reason":     f"A VM named '{name}' already exists",
                "question":   f"A VM called '{name}' already exists. Overwrite it, or use a different name?",
                "fix_field":  "name",
                "options":    [f"{name}-2", f"{name}-new", "overwrite"],
                "correction": "Use a different name or delete the existing VM first.",
            }

        # 3. machine_type is a profile name
        if mt and mt not in VALID_MACHINE_TYPES and not mt.startswith("pc-"):
            fixed = dict(args)
            if mt in get_all_profiles():
                fixed["profile"]      = mt
            fixed.pop("machine_type", None)
            return {
                "action":     "auto_fix",
                "reason":     f"machine_type='{mt}' is a profile name, not a machine type",
                "correction": f"Set profile='{mt}' and removed invalid machine_type",
                "fixed_args": fixed,
            }

        # 4. ISO path hallucinated
        if iso_path:
            bad_path = any([
                "/home/user/" in iso_path,
                "/path/to/"  in iso_path,
                "scan_isos"  in iso_path,
                "<" in iso_path,
                not os.path.exists(os.path.expanduser(
                    re.sub(r"^/home/[^/]+/", REAL_HOME + "/", iso_path)
                )),
            ])
            if bad_path:
                # Try to auto-resolve
                resolved = _resolve_iso(iso_path)
                if resolved and os.path.exists(resolved):
                    fixed = dict(args)
                    fixed["iso_path"] = resolved
                    return {
                        "action":     "auto_fix",
                        "reason":     f"ISO path '{iso_path}' doesn't exist — auto-resolved to '{resolved}'",
                        "correction": f"iso_path corrected to: {resolved}",
                        "fixed_args": fixed,
                    }
                else:
                    # Scan and present options
                    isos = manager.scan_isos()
                    if isos:
                        opts = [iso["name"] for iso in isos[:4]]
                        return {
                            "action":    "ask_user",
                            "reason":    f"ISO '{iso_path}' not found on disk",
                            "question":  f"Can't find that ISO. Which file did you mean?",
                            "fix_field": "iso_path",
                            "options":   opts + ["skip ISO"],
                            "iso_list":  isos,
                        }
                    else:
                        fixed = dict(args)
                        fixed.pop("iso_path", None)
                        return {
                            "action":     "auto_fix",
                            "reason":     f"ISO '{iso_path}' not found — no ISOs found anywhere",
                            "correction": "Removed iso_path. VM will be created without an install ISO.",
                            "fixed_args": fixed,
                        }

        # 5. ARM64 ISO + x86 VM
        if iso_path and os.path.exists(iso_path):
            iso_lower = os.path.basename(iso_path).lower()
            is_arm64  = any(k in iso_lower for k in ("arm64","aarch64"))
            is_x86_vm = str(args.get("machine_arch","x86_64")).lower() == "x86_64"
            if is_arm64 and is_x86_vm:
                return {
                    "action":    "ask_user",
                    "reason":    f"ARM64 ISO '{os.path.basename(iso_path)}' with x86_64 VM — they're incompatible",
                    "question":  "This is an ARM64 ISO. Do you want an ARM64 VM, or an x86_64 ISO instead?",
                    "fix_field": None,
                    "options":   ["Use ARM64 VM", "Get x86_64 ISO instead"],
                    "correction": (
                        "For ARM64 VM: the system will use qemu-system-aarch64. "
                        "For x86_64: download Windows 11 x64 from microsoft.com"
                    ),
                }

        # 6. Windows 11 without UEFI
        is_win = "windows" in os_type or "win" in os_type
        if is_win and args.get("uefi") is False:
            fixed = dict(args)
            fixed["uefi"] = True
            fixed["bios"] = "ovmf"
            return {
                "action":     "auto_fix",
                "reason":     "Windows 11 requires UEFI but uefi=False was set",
                "correction": "Forced uefi=True and bios=ovmf for Windows compatibility",
                "fixed_args": fixed,
            }

        # 7. Disk size suspiciously small for Windows
        disk_gb = int(args.get("disk_size_gb", 60))
        if is_win and disk_gb < 40:
            fixed = dict(args)
            fixed["disk_size_gb"] = 64
            return {
                "action":     "auto_fix",
                "reason":     f"Windows 11 needs at least 64GB disk, got {disk_gb}GB",
                "correction": f"Increased disk_size_gb from {disk_gb} to 64",
                "fixed_args": fixed,
            }

        # 8. Internet + local QEMU validation — checks assumptions against reality
        internet_issues = _validate_with_internet(args, verbose=verbose)
        if internet_issues:
            blockers  = [i for i in internet_issues if i["severity"] == "error"]
            auto_fixes = [i for i in internet_issues if i.get("auto_fix") and i["severity"] != "error"]
            warnings  = [i for i in internet_issues if i["severity"] == "warning" and not i.get("auto_fix")]

            if blockers:
                blocker_text = " | ".join(i["message"] for i in blockers)
                fix_text     = " | ".join(i["fix"] for i in blockers if i.get("fix"))
                return {
                    "action":     "ask_user",
                    "reason":     blocker_text,
                    "question":   "Pre-flight found issues with this VM config. Proceed anyway or fix first?",
                    "fix_field":  None,
                    "options":    ["Proceed anyway", "Cancel and fix"],
                    "correction": fix_text,
                    "issues":     internet_issues,
                    "source":     [i.get("source","") for i in blockers],
                }
            if auto_fixes:
                fixed = dict(args)
                fix_notes = []
                for issue in auto_fixes:
                    if issue.get("fix_field") and issue.get("fix_value") is not None:
                        fixed[issue["fix_field"]] = issue["fix_value"]
                        fix_notes.append(f"{issue['fix_field']}={issue['fix_value']!r}")
                return {
                    "action":     "auto_fix",
                    "reason":     "Internet/QEMU validation auto-fixed: " + ", ".join(fix_notes),
                    "correction": " | ".join(i["message"] for i in auto_fixes),
                    "fixed_args": fixed,
                    "warnings":   [i["message"] for i in warnings],
                }
            if warnings and verbose:
                for w in warnings:
                    console.print(f"  [yellow]⚠ {w['message']}[/yellow]")

        # 9. Profile validation — runs for ALL profiles including new custom ones
        profile_name = args.get("profile") or mt  # mt already stripped to profile if matched
        if profile_name:
            profile_issues = _validate_profile_for_host(profile_name)
            if profile_issues:
                # Classify: hard blockers vs soft warnings
                blockers = [i for i in profile_issues if i["severity"] == "error"]
                warnings = [i for i in profile_issues if i["severity"] == "warning"]
                auto_fixes = [i for i in profile_issues if i.get("auto_fix")]

                if blockers:
                    # Hard block — can't run this profile at all
                    blocker_text = " | ".join(i["message"] for i in blockers)
                    fix_text     = " | ".join(i["fix"] for i in blockers if i.get("fix"))
                    return {
                        "action":     "ask_user",
                        "reason":     f"Profile '{profile_name}' has compatibility issues: {blocker_text}",
                        "question":   f"Profile '{profile_name}' may not work on this system. Proceed anyway or cancel?",
                        "fix_field":  None,
                        "options":    ["Proceed anyway", "Cancel", "Use minimal profile instead"],
                        "correction": fix_text or "Check system compatibility before proceeding.",
                        "issues":     profile_issues,
                    }
                elif auto_fixes:
                    # Auto-fix what we can
                    fixed = dict(args)
                    fix_notes = []
                    for issue in auto_fixes:
                        if issue.get("fix_field") and issue.get("fix_value") is not None:
                            fixed[issue["fix_field"]] = issue["fix_value"]
                            fix_notes.append(f"{issue['fix_field']}={issue['fix_value']}")
                    return {
                        "action":     "auto_fix",
                        "reason":     f"Profile '{profile_name}': auto-fixed " + ", ".join(fix_notes),
                        "correction": " | ".join(i["message"] for i in auto_fixes),
                        "fixed_args": fixed,
                        "warnings":   [i["message"] for i in warnings],
                    }
                elif warnings:
                    # Just warn, don't block
                    warn_text = " | ".join(i["message"] for i in warnings)
                    if verbose:
                        console.print(f"  [yellow]⚠ Profile warnings: {warn_text}[/yellow]")

    # ── launch_vm checks ───────────────────────────────────────
    elif tool_name == "launch_vm":
        name = str(args.get("name", "")).strip()

        # VM must exist
        vm_dir = os.path.join(os.path.expanduser("~"), ".qemu_vms", name)
        if name and not os.path.exists(vm_dir):
            # Maybe the user meant a different VM — list candidates
            all_vms = manager.list_vms()
            candidates = [v["name"] for v in all_vms if name.lower() in v["name"].lower()]
            if candidates:
                return {
                    "action":     "abort",
                    "reason":     f"VM '{name}' not found. Did you mean: {candidates}?",
                    "correction": f"Use one of these names: {candidates}",
                }
            return {
                "action":     "abort",
                "reason":     f"VM '{name}' doesn't exist. Create it first.",
                "correction": "Call create_vm before launch_vm.",
            }

        # Check ISO exists if set
        try:
            cfg = MachineConfig.load(name)
            if cfg.iso_path and not os.path.exists(cfg.iso_path):
                return {
                    "action":    "ask_user",
                    "reason":    f"ISO file missing: {cfg.iso_path}",
                    "question":  f"The ISO '{os.path.basename(cfg.iso_path)}' is missing. Launch without ISO, or fix the path?",
                    "fix_field": None,
                    "options":   ["Launch anyway (no ISO)", "Cancel"],
                }
        except Exception:
            pass

    # ── delete_vm checks ───────────────────────────────────────
    elif tool_name == "delete_vm":
        name = str(args.get("name", "")).strip()
        if name:
            return {
                "action":    "ask_user",
                "reason":    f"Destructive operation: delete VM '{name}'",
                "question":  f"Are you sure you want to delete '{name}'?",
                "fix_field": None,
                "options":   ["Yes, delete it", "No, keep it"],
                "correction": "Deletion cannot be undone without recreating the VM.",
            }

    # ── resize_disk checks ─────────────────────────────────────
    elif tool_name == "resize_disk":
        name     = str(args.get("name", "")).strip()
        new_size = int(args.get("new_size_gb", 0))
        if name and new_size:
            vm_dir = os.path.join(os.path.expanduser("~"), ".qemu_vms", name)
            if not os.path.exists(vm_dir):
                return {
                    "action":     "abort",
                    "reason":     f"VM '{name}' does not exist — cannot resize disk",
                    "correction": "Create the VM first with create_vm, then resize.",
                }
            try:
                cfg = MachineConfig.load(name)
                if cfg.disks:
                    current = cfg.disks[0].size_gb
                    if new_size < current:
                        return {
                            "action":     "abort",
                            "reason":     f"Cannot shrink disk from {current}GB to {new_size}GB — QEMU doesn't support shrinking",
                            "correction": f"new_size_gb must be >= current size ({current}GB)",
                        }
            except FileNotFoundError:
                return {
                    "action":     "abort",
                    "reason":     f"VM '{name}' config not found — cannot resize disk",
                    "correction": "Check the VM name with list_vms.",
                }
            except Exception:
                pass

    # ── send_monitor_cmd safety check ─────────────────────────
    elif tool_name == "send_monitor_cmd":
        cmd = str(args.get("cmd", "")).strip().lower()
        dangerous = ["quit", "system_reset", "powerdown", "eject", "device_del"]
        if any(d in cmd for d in dangerous):
            return {
                "action":    "ask_user",
                "reason":    f"Potentially destructive monitor command: '{cmd}'",
                "question":  f"Run QEMU monitor command '{cmd}'? This may affect the running VM.",
                "fix_field": None,
                "options":   ["Yes, run it", "No, cancel"],
            }

    # ── snapshot_restore/delete safety ────────────────────────
    elif tool_name in ("snapshot_restore", "snapshot_delete"):
        name      = str(args.get("name", "")).strip()
        snap_name = str(args.get("snap_name", "")).strip()
        verb      = "restore" if tool_name == "snapshot_restore" else "delete"
        return {
            "action":    "ask_user",
            "reason":    f"Snapshot {verb}: '{snap_name}' on VM '{name}'",
            "question":  f"Confirm {verb} snapshot '{snap_name}' on '{name}'?",
            "fix_field": None,
            "options":   [f"Yes, {verb} it", "No, cancel"],
            "correction": "Snapshot restore replaces current VM state. Snapshot delete is permanent.",
        }

    return ok


def _show_preflight_warning(preflight: Dict):
    """Display a pre-flight warning panel and ask the user to confirm or fix."""
    reason   = preflight.get("reason", "")
    question = preflight.get("question", "Confirm?")
    options  = preflight.get("options", [])
    correction = preflight.get("correction", "")

    lines = [f"[yellow]⚠[/yellow] {reason}"]
    if correction:
        lines.append(f"[dim]{correction}[/dim]")

    console.print(Panel(
        "\n".join(lines),
        title="[bold yellow]Pre-flight Check[/bold yellow]",
        border_style="yellow",
    ))

    if options:
        opts_str = "  ".join(f"[dim][{o}][/dim]" for o in options)
        console.print(f"\n[ai]Assistant:[/ai] {question}  {opts_str}\n")
    else:
        console.print(f"\n[ai]Assistant:[/ai] {question}\n")


# ─────────────────────────────────────────────
#  BANNER
# ─────────────────────────────────────────────

def _print_banner(verbose: bool):
    ovmf_line = (
        f"[success]OVMF ✓[/success]  {OVMF.get('code','')}"
        if OVMF["available"]
        else "[warn]OVMF ✗  SeaBIOS fallback active[/warn]"
    )
    verb_line = "[success]verbose mode ON[/success]" if verbose else "[dim]verbose OFF  (use -v to enable)[/dim]"
    console.print(Panel(
        f"[bold cyan]QEMU/KVM AI Assistant[/bold cyan]  •  Llama 3.1 via Ollama\n"
        f"Model: [bold]{OLLAMA_MODEL}[/bold]  |  {OLLAMA_URL}\n"
        f"{ovmf_line}\n"
        f"{verb_line}\n"
        f"[dim]Commands: 'exit' · 'clear session' · 'list' · 'system'[/dim]",
        border_style="cyan",
        title="[bold]qemu-api[/bold]",
    ))


# ─────────────────────────────────────────────
#  CHAT LOOP
# ─────────────────────────────────────────────

def chat_loop(verbose: bool = False):
    """Main interactive chat loop."""
    _print_banner(verbose=verbose)
    messages = load_session()

    while True:
        try:
            user_input = console.input("\n[bold cyan]You:[/bold cyan] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if not user_input:
            continue

        # ── Built-in shortcuts (before AI and before vagueness layer) ──────────
        _ui = user_input.lower().strip()
        if _ui in ("exit","quit","bye","goodbye","q",":q",":q!",
                   "stop","leave","close","done","end","out",
                   "exit()","quit()","i want to exit","i want to quit",
                   "please exit","please quit"):
            console.print("[dim]Goodbye.[/dim]")
            break

        if _ui in ("list","vms"):
            result = execute_tool("list_vms", {}, verbose)
            console.print(result.get("output",""))
            continue

        if _ui == "system":
            result = execute_tool("check_system", {}, verbose)
            console.print(result.get("output",""))
            continue

        if _ui == "profiles":
            result = execute_tool("list_profiles", {}, verbose)
            console.print(result.get("output",""))
            continue

        if _ui in ("clear session","forget"):
            clear_session()
            messages = []
            console.print("[dim]Session cleared.[/dim]")
            continue

        messages.append({"role": "user", "content": user_input})

        # ── Agentic loop ──────────────────────────────────────────────────────
        for _ in range(15):
            response = _call_ollama(messages)
            if not response:
                console.print("[warn]No response from Ollama.[/warn]")
                break

            msg = response.get("message", {})
            assistant_msg = {"role": "assistant", "content": msg.get("content",""),
                             "tool_calls": msg.get("tool_calls",[])}
            messages.append(assistant_msg)

            tool_calls = msg.get("tool_calls", [])
            if not tool_calls:
                # Plain text response
                text = msg.get("content","").strip()
                if text:
                    console.print(f"\n[bold green]Assistant:[/bold green] {text}\n")
                break

            for tc in tool_calls:
                fn        = tc.get("function", {})
                tool_name = fn.get("name", "")
                raw_args  = fn.get("arguments", {})
                if isinstance(raw_args, str):
                    try:
                        raw_args = json.loads(raw_args)
                    except Exception:
                        raw_args = {}

                if verbose:
                    console.print(f"  [tool]→ {tool_name}[/tool]  [dim]{json.dumps(raw_args)}[/dim]")

                result = execute_tool(tool_name, raw_args, verbose)
                messages.append({
                    "role": "tool",
                    "content": json.dumps(result, default=str),
                })

                if isinstance(result, dict) and result.get("clarify"):
                    q    = result.get("question","Please provide more detail.")
                    opts = result.get("options",[])
                    if opts:
                        console.print(f"[yellow]?[/yellow] {q}  " +
                                      "  ".join(f"[{o}]" for o in opts))
                    else:
                        console.print(f"[yellow]?[/yellow] {q}")
                    try:
                        clarified = console.input("[bold cyan]You:[/bold cyan] ").strip()
                    except (KeyboardInterrupt, EOFError):
                        console.print("\n[dim]Goodbye.[/dim]")
                        break
                    if clarified:
                        messages.append({"role": "user", "content": clarified})

        save_session(messages)


# ─────────────────────────────────────────────
#  DIRECT CLI
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
#  -tf FINGERPRINT REPORT
#  Simulates what inxi would report from inside
#  the guest, checks each field against known
#  VM fingerprint signatures, scores the result.
# ─────────────────────────────────────────────

_VM_BIOS_VENDORS   = {"seabios", "ovmf", "tianocore", "edk ii", "bochs"}
_VM_CHASSIS_TYPES  = {"other", "unspecified", ""}


def _tf_report(name: str) -> None:
    """
    Simulate inxi -M -N -C -D -A -G output for a VM config.
    Report what a fingerprinting tool would see from inside the guest.
    """
    try:
        cfg = MachineConfig.load(name)
    except Exception as e:
        console.print(f"[error]Cannot load VM '{name}': {e}[/error]")
        return

    checks = []

    # ── inxi -M: Machine / SMBIOS ─────────────────────────────────────────────
    mfr = (cfg.manufacturer or "").strip()
    if not mfr:
        checks.append({"section": "inxi -M", "field": "System manufacturer",
                        "value": "(not set)", "status": "fail",
                        "detail": "QEMU default 'QEMU' string exposed — immediately detectable"})
    elif mfr.lower() in ("qemu", "bochs", "vmware", "virtualbox", "xen"):
        checks.append({"section": "inxi -M", "field": "System manufacturer",
                        "value": mfr, "status": "fail",
                        "detail": f"'{mfr}' is a known hypervisor string"})
    else:
        checks.append({"section": "inxi -M", "field": "System manufacturer",
                        "value": mfr, "status": "ok",
                        "detail": "Looks like real hardware manufacturer"})

    prod = (cfg.product_name or "").strip()
    if not prod:
        checks.append({"section": "inxi -M", "field": "System product",
                        "value": "(not set)", "status": "fail",
                        "detail": "QEMU default 'Standard PC' string exposed"})
    elif "qemu" in prod.lower() or "standard pc" in prod.lower():
        checks.append({"section": "inxi -M", "field": "System product",
                        "value": prod, "status": "fail",
                        "detail": "QEMU default product name exposed"})
    else:
        checks.append({"section": "inxi -M", "field": "System product",
                        "value": prod, "status": "ok",
                        "detail": "Looks like real hardware product"})

    bv = (cfg.bios_vendor or "").strip()
    if not bv:
        checks.append({"section": "inxi -M", "field": "BIOS vendor",
                        "value": "(not set)", "status": "warn",
                        "detail": "Missing — inxi will show SeaBIOS or OVMF default"})
    elif any(v in bv.lower() for v in _VM_BIOS_VENDORS):
        checks.append({"section": "inxi -M", "field": "BIOS vendor",
                        "value": bv, "status": "fail",
                        "detail": f"'{bv}' is a known VM firmware vendor"})
    else:
        checks.append({"section": "inxi -M", "field": "BIOS vendor",
                        "value": bv, "status": "ok",
                        "detail": "Looks like real BIOS vendor"})

    bver = (cfg.bios_version or "").strip()
    if not bver:
        checks.append({"section": "inxi -M", "field": "BIOS version",
                        "value": "(not set)", "status": "warn",
                        "detail": "Missing — firmware default string will be exposed"})
    else:
        checks.append({"section": "inxi -M", "field": "BIOS version",
                        "value": bver, "status": "ok",
                        "detail": "BIOS version string set"})

    sn = (cfg.serial_number or "").strip()
    if not sn:
        checks.append({"section": "inxi -M", "field": "Serial number",
                        "value": "(not set)", "status": "warn",
                        "detail": "Missing — inxi shows empty or 'Not Specified'"})
    else:
        checks.append({"section": "inxi -M", "field": "Serial number",
                        "value": sn, "status": "ok",
                        "detail": "Serial number string set"})

    ct = (cfg.smbios_type or "").strip()
    if not ct or ct.lower() in _VM_CHASSIS_TYPES:
        checks.append({"section": "inxi -M", "field": "Chassis type",
                        "value": ct or "(not set)", "status": "warn",
                        "detail": "Default chassis type — inxi may show 'Other' or blank"})
    else:
        checks.append({"section": "inxi -M", "field": "Chassis type",
                        "value": ct, "status": "ok",
                        "detail": "Chassis type set (e.g. Notebook, Desktop)"})

    # ── inxi -N: Network / MAC ────────────────────────────────────────────────
    mac = (cfg.mac_address or "").strip().upper()
    if not mac:
        checks.append({"section": "inxi -N", "field": "MAC OUI prefix",
                        "value": "(auto-generated)", "status": "warn",
                        "detail": "QEMU will assign 52:54:00 prefix — known QEMU OUI"})
    else:
        oui = ":".join(mac.split(":")[:3]).lower()
        if any(oui.startswith(q.lower()) for q in _QEMU_OUI_PREFIXES):
            checks.append({"section": "inxi -N", "field": "MAC OUI prefix",
                            "value": oui.upper(), "status": "fail",
                            "detail": f"'{oui.upper()}' is the QEMU OUI — universally recognised as a VM"})
        else:
            checks.append({"section": "inxi -N", "field": "MAC OUI prefix",
                            "value": oui.upper(), "status": "ok",
                            "detail": "OUI not in known QEMU range"})

    if cfg.network_mode not in ("none", ""):
        checks.append({"section": "inxi -N", "field": "NIC driver",
                        "value": "virtio-net-pci", "status": "warn",
                        "detail": "virtio-net is a paravirtual driver — VM indicator (e1000 is less detectable)"})

    # ── inxi -C: CPU ──────────────────────────────────────────────────────────
    cpu = (cfg.cpu_model or "host").lower()
    if cfg.kvm:
        checks.append({"section": "inxi -C", "field": "Hypervisor flag",
                        "value": "KVM (kvm=True)", "status": "warn",
                        "detail": "KVM hypercall flags visible in /proc/cpuinfo — hypervisor flag set"})
    else:
        checks.append({"section": "inxi -C", "field": "Hypervisor flag",
                        "value": "none (kvm=False)", "status": "ok",
                        "detail": "KVM flags not exposed to guest"})

    if cpu in ("kvm64", "qemu64", "qemu32"):
        checks.append({"section": "inxi -C", "field": "CPU model string",
                        "value": cpu, "status": "fail",
                        "detail": f"'{cpu}' is a virtual CPU model — immediately identifiable as VM"})
    elif cpu == "host":
        checks.append({"section": "inxi -C", "field": "CPU model string",
                        "value": "host (passes real CPU)", "status": "ok",
                        "detail": "CPU model passes through host CPU — looks like real hardware"})
    else:
        checks.append({"section": "inxi -C", "field": "CPU model string",
                        "value": cpu, "status": "ok",
                        "detail": "Named CPU model set"})

    # ── inxi -D: Disk ─────────────────────────────────────────────────────────
    checks.append({"section": "inxi -D", "field": "Disk interface",
                    "value": "virtio-blk (vda/vdb)", "status": "warn",
                    "detail": "virtio-blk device names (vda, vdb) are a strong VM indicator"})
    checks.append({"section": "inxi -D", "field": "Disk model string",
                    "value": "QEMU HARDDISK (default)", "status": "fail",
                    "detail": "QEMU sets disk model to 'QEMU HARDDISK' — add model= to extra_args to override"})

    # ── inxi -A: Audio ────────────────────────────────────────────────────────
    audio = (cfg.audio or "none").lower()
    if audio == "none":
        checks.append({"section": "inxi -A", "field": "Audio chip",
                        "value": "none", "status": "ok",
                        "detail": "No audio device — nothing to fingerprint"})
    else:
        checks.append({"section": "inxi -A", "field": "Audio chip",
                        "value": f"Intel HDA ({audio})", "status": "warn",
                        "detail": "Intel HDA is common in real hardware but device ID may still indicate VM"})

    # ── inxi -G: GPU ──────────────────────────────────────────────────────────
    gpu = (cfg.gpu or "none").lower()
    if "virtio" in gpu:
        checks.append({"section": "inxi -G", "field": "GPU driver",
                        "value": gpu, "status": "fail",
                        "detail": "virtio-gpu is a paravirtual GPU — immediately identifies VM"})
    elif "qxl" in gpu:
        checks.append({"section": "inxi -G", "field": "GPU driver",
                        "value": "qxl", "status": "fail",
                        "detail": "QXL is a SPICE/QEMU-specific GPU — obvious VM indicator"})
    elif "vmware" in gpu:
        checks.append({"section": "inxi -G", "field": "GPU driver",
                        "value": "vmware-svga", "status": "fail",
                        "detail": "VMware SVGA driver is a VM indicator even in QEMU"})
    elif gpu == "none":
        checks.append({"section": "inxi -G", "field": "GPU driver",
                        "value": "none", "status": "ok",
                        "detail": "No GPU — nothing to fingerprint"})
    else:
        checks.append({"section": "inxi -G", "field": "GPU driver",
                        "value": gpu, "status": "ok",
                        "detail": "Non-standard GPU type"})

    # ── Score ─────────────────────────────────────────────────────────────────
    n_ok   = sum(1 for c in checks if c["status"] == "ok")
    n_warn = sum(1 for c in checks if c["status"] == "warn")
    n_fail = sum(1 for c in checks if c["status"] == "fail")
    total  = len(checks)
    pct    = int(n_ok / total * 100) if total else 0

    status_map = {
        "ok":   "[green]clean    [/green]",
        "warn": "[yellow]detectable[/yellow]",
        "fail": "[red]VM tell  [/red]",
    }

    # ── Render ────────────────────────────────────────────────────────────────
    score_colour = "green" if pct >= 80 else "yellow" if pct >= 50 else "red"
    console.print()
    header = (
        f"[bold]Fingerprint Report: {name}[/bold]\n"
        f"Simulates: [cyan]inxi -M -N -C -D -A -G[/cyan]\n"
        f"Score: [{score_colour}]{pct}% look like real hardware[/{score_colour}]"
        f"  ({n_ok} clean  {n_warn} detectable  {n_fail} VM tells)"
    )
    console.print(Panel(header, border_style="cyan", title="-tf Fingerprint Analysis"))

    sections = {}
    for c in checks:
        sections.setdefault(c["section"], []).append(c)

    for section, items in sections.items():
        t = Table(box=box.SIMPLE, border_style="dim", show_header=True,
                  header_style="bold dim")
        t.add_column(f"[bold cyan]{section}[/bold cyan]", width=22)
        t.add_column("Value", width=30)
        t.add_column("Status", width=12, justify="center")
        t.add_column("Detail")
        for item in items:
            t.add_row(item["field"], item["value"],
                      status_map[item["status"]],
                      f"[dim]{item['detail']}[/dim]")
        console.print(t)

    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    if fails or warns:
        lines = []
        if fails:
            lines.append("[bold red]Critical (fix these first):[/bold red]")
            for c in fails:
                lines.append(f"  [red]*[/red] {c['field']}: {c['detail']}")
        if warns:
            lines.append("[bold yellow]Warnings:[/bold yellow]")
            for c in warns:
                lines.append(f"  [yellow]*[/yellow] {c['field']}: {c['detail']}")
        console.print(Panel(
            "\n".join(lines),
            title="Recommendations",
            border_style="yellow",
        ))
    console.print()

def cli_direct(args: List[str], verbose: bool = False):
    def pp(data):
        if verbose:
            console.print_json(json.dumps(data, default=str))

    cmd  = args[0]
    rest = args[1:]

    if   cmd == "list":
        vms = manager.list_vms()
        _render_vm_list(vms)
        if verbose: pp(vms)

    elif cmd == "status" and rest:
        r = manager.vm_status(rest[0])
        _render_status(r)
        if verbose: pp(r)

    elif cmd == "monitor":
        name = rest[0] if rest else "all"
        r = manager.monitor_all() if name == "all" else manager.monitor_vm(name)
        if isinstance(r, dict) and "state" in r:
            _render_monitor(r)
        else:
            for v in r.values(): _render_monitor(v)
        if verbose: pp(r)

    elif cmd == "launch" and rest:
        r = manager.launch_vm(rest[0], display=rest[1] if len(rest) > 1 else None)
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

    elif cmd == "stop" and rest:
        r = manager.stop_vm(rest[0])
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

    elif cmd == "config" and rest:
        r = manager.show_config(rest[0])
        if r.get("success"):
            console.print_json(json.dumps(r["config"], default=str))
        else:
            console.print(f"[error]{r['error']}[/error]")

    elif cmd == "resize" and len(rest) >= 2:
        r = manager.resize_disk(rest[0], 0, int(rest[1]))
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

    elif cmd == "clone" and len(rest) >= 2:
        r = manager.clone_vm(rest[0], rest[1])
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

    elif cmd == "snapshot" and len(rest) >= 2:
        sub = rest[0]
        if sub == "list" and len(rest) >= 2:
            r = manager.snapshot_list(rest[1])
            _render_snapshots(r)
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
            r = manager.create_network(rest[1])
            console.print_json(json.dumps(r, default=str))
        elif sub == "delete" and len(rest) >= 2:
            r = manager.delete_network(rest[1])
            console.print_json(json.dumps(r, default=str))
        elif sub == "add" and len(rest) >= 3:
            r = manager.add_vm_to_network(rest[1], rest[2])
            console.print_json(json.dumps(r, default=str))

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
        _render_profiles(list_profiles())

    elif cmd == "check-profile" and rest:
        _render_compat(check_profile_compatibility(rest[0]))

    elif cmd == "system":
        caps = check_system_capabilities()
        caps["ovmf_paths"] = OVMF
        _render_system(caps)

    elif cmd == "isos":
        isos = manager.scan_isos()
        if isos:
            t = Table(box=box.ROUNDED, border_style="cyan")
            t.add_column("File"); t.add_column("Size"); t.add_column("Path", style="dim")
            for iso in isos:
                t.add_row(iso["name"], f"{iso['size_gb']}GB", iso["path"])
            console.print(t)
        else:
            console.print("[warn]No ISOs found in common locations.[/warn]")

    elif cmd == "show-cmd" and rest:
        r = manager.print_command(rest[0])
        if r.get("success"):
            console.print(Panel(r["command"], title="QEMU Command", border_style="cyan"))

    elif cmd == "clear-session":
        clear_session()

    elif cmd == "-tf" and rest:
        _tf_report(rest[0])

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
            "  qemu-api -tf <name>\n\n"
            "Add [bold]-v[/bold] anywhere for verbose/raw output.\n"
            "Add [bold]-cu[/bold] to AI chat to skip product verification for custom machines.",
            border_style="cyan", title="qemu-api help"
        ))


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    args    = sys.argv[1:]
    verbose = "-v" in args or "--verbose" in args
    args    = [a for a in args if a not in ("-v", "--verbose")]

    # -cu flag: custom machine mode — skip product verification
    if "-cu" in args:
        _CUSTOM_MODE = True
        args = [a for a in args if a != "-cu"]
        console.print("[dim]Custom mode active — product verification disabled[/dim]")

    if args:
        cli_direct(args, verbose=verbose)
    else:
        chat_loop(verbose=verbose)

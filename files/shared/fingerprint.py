"""
fingerprint.py — VM Fingerprint Analysis Layer

Simulates what inxi would report from inside a VM guest. Checks each
SMBIOS/hardware field against known VM signatures and scores the result.
"""

import json
import os
from typing import Dict

from shared.display import console
from rich import box
from rich.panel import Panel
from rich.table import Table

_CFG_PATH = os.path.join(os.path.dirname(__file__), "..", "server", "ai", "config.json")
try:
    _CFG = json.load(open(_CFG_PATH))
    _FP  = _CFG.get("fingerprint", {})
except FileNotFoundError:
    _FP  = {}

_VM_BIOS_VENDORS   = set(_FP.get("vm_bios_vendors",   ["seabios", "ovmf", "tianocore", "edk ii", "bochs"]))
_VM_CHASSIS_TYPES  = set(_FP.get("vm_chassis_types",   ["other", "unspecified", ""]))
_QEMU_OUI_PREFIXES = set(_FP.get("qemu_oui_prefixes",  ["52:54:00", "00:1a:4a"]))
_SCORE_GOOD        = _FP.get("score_good", 80)
_SCORE_WARN        = _FP.get("score_warn", 50)

try:
    from shared.api.qemu_config import MachineConfig
except ImportError:
    MachineConfig = None  # type: ignore[assignment,misc]


# Simulates what inxi -M -N -C -D -A -G would report from inside the guest, scoring each field and printing a recommendation panel.
# In: str VM name → Out: nothing (console output)
def _tf_report(name: str, summary: bool = False) -> dict:
    if MachineConfig is None:
        return {"success": False, "error": "fingerprint not available in provider-only mode"}

    try:
        cfg = MachineConfig.load(name)
    except Exception as e:
        console.print(f"[error]Cannot load VM '{name}': {e}[/error]")
        return {"success": False, "error": str(e)}

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
    mac = ""
    network_mode = ""
    if cfg.networks:
        mac          = (cfg.networks[0].mac  or "").strip().upper()
        network_mode = (cfg.networks[0].mode or "").strip()

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

    _VM_NIC_MODELS = {"virtio-net-pci", "virtio-net", "vmxnet3"}
    nic_model = ""
    if cfg.networks:
        nic_model = (cfg.networks[0].model or "").strip()
    if network_mode not in ("none", ""):
        if nic_model in _VM_NIC_MODELS:
            checks.append({"section": "inxi -N", "field": "NIC driver",
                            "value": nic_model, "status": "warn",
                            "detail": f"'{nic_model}' is a paravirtual driver — VM indicator; use e1000e or rtl8139"})
        elif nic_model:
            checks.append({"section": "inxi -N", "field": "NIC driver",
                            "value": nic_model, "status": "ok",
                            "detail": f"'{nic_model}' emulates real hardware NIC"})
        else:
            checks.append({"section": "inxi -N", "field": "NIC driver",
                            "value": "(default)", "status": "ok",
                            "detail": "e1000e emulates real Intel NIC"})

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
    disk = cfg.disks[0] if cfg.disks else None
    disk_bus = (disk.bus if disk else "virtio").lower()
    if disk_bus == "virtio":
        checks.append({"section": "inxi -D", "field": "Disk interface",
                        "value": "virtio-blk (vda/vdb)", "status": "warn",
                        "detail": "virtio-blk device names (vda, vdb) are a strong VM indicator; use sata, nvme, or scsi"})
    else:
        checks.append({"section": "inxi -D", "field": "Disk interface",
                        "value": disk_bus, "status": "ok",
                        "detail": f"'{disk_bus}' uses real hardware device names (sda/nvme0n1)"})
    disk_model = (disk.disk_model if disk else "").strip()
    if not disk_model:
        checks.append({"section": "inxi -D", "field": "Disk model string",
                        "value": "QEMU HARDDISK (default)", "status": "fail",
                        "detail": "QEMU default disk model is 'QEMU HARDDISK' — set disk_model to a real drive name"})
    elif disk_bus == "virtio":
        checks.append({"section": "inxi -D", "field": "Disk model string",
                        "value": disk_model, "status": "warn",
                        "detail": f"Model set to '{disk_model}' but virtio-blk does not expose model to guest — switch bus to sata/nvme/scsi"})
    else:
        checks.append({"section": "inxi -D", "field": "Disk model string",
                        "value": disk_model, "status": "ok",
                        "detail": f"'{disk_model}' will be reported by inxi -D"})

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

    # ── Hardened / host security ──────────────────────────────────────────────
    if getattr(cfg, "hardened", False):
        checks.append({"section": "host security", "field": "QEMU seccomp sandbox",
                        "value": "enabled", "status": "ok",
                        "detail": "QEMU process is seccomp-sandboxed — guest code execution in QEMU cannot make dangerous syscalls"})
        checks.append({"section": "host security", "field": "Hypervisor CPUID bit",
                        "value": "hidden (-hypervisor, kvm=off)", "status": "ok",
                        "detail": "Guest cannot detect KVM via CPUID; KVM acceleration still active for performance"})
        checks.append({"section": "host security", "field": "CPU speculation mitigations",
                        "value": "spec-ctrl, ssbd, md-clear, ibrs, stibp", "status": "ok",
                        "detail": "Spectre/Meltdown mitigation flags exposed to guest — reduces cross-VM leakage risk"})
        checks.append({"section": "host security", "field": "SMM (ring-below-ring0)",
                        "value": "disabled (smm=off)", "status": "ok",
                        "detail": "System Management Mode disabled — removes a common firmware-level escape vector"})
    else:
        checks.append({"section": "host security", "field": "Hardened mode",
                        "value": "off", "status": "warn",
                        "detail": "hardened=True adds seccomp sandbox, hides hypervisor CPUID, disables SMM, and adds speculation mitigations"})

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

    result = {
        "success": True,
        "name":    name,
        "score":   pct,
        "clean":   n_ok,
        "detectable": n_warn,
        "vm_tells":   n_fail,
    }
    if summary:
        return result

    # ── Render ────────────────────────────────────────────────────────────────
    score_colour = "green" if pct >= _SCORE_GOOD else "yellow" if pct >= _SCORE_WARN else "red"
    console.print()
    header = (
        f"[bold]Fingerprint Report: {name}[/bold]\n"
        f"Simulates: [cyan]inxi -M -N -C -D -A -G[/cyan]\n"
        f"Score: [{score_colour}]{pct}% look like real hardware[/{score_colour}]"
        f"  ({n_ok} clean  {n_warn} detectable  {n_fail} VM tells)"
    )
    console.print(Panel(header, border_style="cyan", title="-tf Fingerprint Analysis"))

    sections: Dict = {}
    for c in checks:
        sections.setdefault(c["section"], []).append(c)

    for section, items in sections.items():
        t = Table(box=box.SIMPLE, border_style="dim", show_header=True,
                  header_style="bold dim")
        t.add_column(f"[bold cyan]{section}[/bold cyan]", width=22)
        t.add_column("Value",  width=30)
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
    return result

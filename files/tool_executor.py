"""
tool_executor.py — Tool Execution Dispatch Layer

Single entry point execute_tool() that sanitises args, resolves VM
names, then dispatches to QemuManager or the config layer. Also owns
the manager singleton so all other modules share one instance.
"""

import json
import os
import re
from typing import Any, Dict

from qemu_config import (
    MachineConfig, DiskConfig, NetworkConfig,
    OVMF, apply_profile, check_profile_compatibility,
    check_system_capabilities, delete_custom_profile,
    get_all_profiles, list_profiles, save_custom_profile,
)
from qemu_manager import QemuManager
from sanitizer   import (
    PLACEHOLDER_VM_NAMES,
    _resolve_iso, _resolve_vm_name, _sanitise_args,
)
from display import (
    console,
    _render_compat, _render_monitor, _render_profiles,
    _render_snapshots, _render_status, _render_system,
    _render_vm_failure, _render_vm_list,
)
from rich.panel import Panel

manager = QemuManager()


def execute_tool(tool_name: str, args: Dict[str, Any], verbose: bool = False) -> Any:
    args = _sanitise_args(tool_name, args)

    # Resolve VM names by fuzzy match / index
    if "name" in args and tool_name not in (
        "create_vm", "create_profile", "clone_vm", "create_network"
    ):
        vms      = manager.list_vms()
        resolved = _resolve_vm_name(vms, str(args["name"]))
        if resolved:
            args["name"] = resolved

    # ── clarify ───────────────────────────────────────────────────────────────
    if tool_name == "clarify":
        return {"clarify": True, "question": args.get("question", ""), "options": args.get("options", [])}

    # ── system info ───────────────────────────────────────────────────────────
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
        if notes:
            args["_notes"] = notes
        result = save_custom_profile(pname, args)
        if result["success"]:
            result["compatibility"] = check_profile_compatibility(result["profile_name"])
        return result

    elif tool_name == "delete_profile":
        return delete_custom_profile(args["profile_name"])

    # ── create_vm ─────────────────────────────────────────────────────────────
    elif tool_name == "create_vm":
        raw_name = args.get("name", "") or ""
        name     = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(raw_name).strip())
        if not name or name.lower() in PLACEHOLDER_VM_NAMES:
            return {
                "success": False,
                "clarify": True,
                "question": "What would you like to name this VM?",
                "options":  ["my-windows-vm", "dev-machine", "test-ubuntu"],
                "error":    "VM name is required — please provide a unique name.",
                "needs_clarification": "name",
            }
        args["name"] = name

        cfg = MachineConfig(
            name=name,
            os_type=args.get("os_type", "linux"),
            os_name=args.get("os_name", ""),
            description=args.get("description", ""),
        )

        profile = args.get("profile")
        if not profile:
            product = (args.get("product_name", "") + " " + args.get("manufacturer", "")).lower()
            for pname, pdata in get_all_profiles().items():
                pp = (pdata.get("product_name", "") + " " + pdata.get("manufacturer", "")).lower()
                if any(kw in pp for kw in product.split() if len(kw) > 3):
                    profile = pname
                    break
        if profile:
            try:
                cfg = apply_profile(cfg, profile)
            except ValueError as e:
                return {"success": False, "error": str(e)}

        for f in ("machine_class", "cpu_model", "cpu_cores", "cpu_threads", "memory_mb",
                  "display", "gpu", "audio", "manufacturer", "product_name", "bios_version",
                  "uefi", "kvm", "battery", "hugepages", "machine_type", "os_type", "os_name"):
            if f in args and args[f] is not None and args[f] != "":
                setattr(cfg, f, args[f])

        if args.get("extra_args"):
            cfg.extra_args = args["extra_args"]

        # Reject profile names used as machine_type
        valid_machine_types = {"q35", "pc", "pc-i440fx", "microvm", "virt", "raspi3b", "raspi2b", "raspi0"}
        if cfg.machine_type:
            mt = cfg.machine_type.lower().split(",")[0].strip()
            if mt not in valid_machine_types and not mt.startswith("pc-"):
                cfg.machine_type = "q35"

        # Windows 11 requires UEFI + q35
        if "windows" in cfg.os_type.lower() or "windows" in cfg.os_name.lower():
            cfg.uefi = True
            cfg.bios = "ovmf"
            if cfg.machine_type not in ("q35",):
                cfg.machine_type = "q35"

        # Reject ARM CPU on x86 VM
        arm_cpu_prefixes = ("cortex", "arm1", "arm9", "arm11")
        if cfg.machine_arch == "x86_64" and any(
            cfg.cpu_model.lower().startswith(p) for p in arm_cpu_prefixes
        ):
            cfg.cpu_model = "host"

        # Auto-detect architecture from ISO filename
        iso_hint = args.get("iso_path", "")
        if iso_hint:
            iso_lower = os.path.basename(iso_hint).lower()
            if any(k in iso_lower for k in ("arm64", "aarch64", "_arm_", "arm_v")):
                cfg.machine_arch  = "aarch64"
                cfg.qemu_binary   = "qemu-system-aarch64"
                cfg.kvm           = False
                cfg.machine_type  = cfg.machine_type if cfg.machine_type in ("virt", "raspi3b") else "virt"
                cfg.bios          = "seabios"
                cfg.uefi          = False
                cfg.hugepages     = False
                if not verbose:
                    console.print("  [yellow]⚠ ARM64 ISO detected — switched to aarch64 VM[/yellow]")

        # Block cross-arch ISO/VM mismatch
        iso_hint = args.get("iso_path", "")
        if iso_hint:
            iso_lower  = os.path.basename(iso_hint).lower()
            is_iso_arm = any(k in iso_lower for k in ("arm64", "aarch64", "arm_", "_arm"))
            is_iso_x86 = any(k in iso_lower for k in ("amd64", "x86_64", "x64", "i386", "i686"))
            if is_iso_arm and cfg.machine_arch == "x86_64":
                return {
                    "success": False,
                    "error": (
                        f"Architecture mismatch — '{os.path.basename(iso_hint)}' is an ARM64 ISO "
                        f"but this VM is x86_64. "
                        f"Either use an x86_64 Windows 11 ISO or create an aarch64 VM."
                    ),
                }
            if is_iso_x86 and cfg.machine_arch in ("aarch64", "arm"):
                return {
                    "success": False,
                    "error": (
                        f"Architecture mismatch — '{os.path.basename(iso_hint)}' is an x86_64 ISO "
                        f"but this VM is ARM. Use an ARM64 ISO instead."
                    ),
                }

        disk_size   = int(args.get("disk_size_gb", 60))
        disk_format = args.get("disk_format", "qcow2")
        disk_path   = os.path.expanduser(f"~/.qemu_vms/{cfg.name}/disk0.{disk_format}")
        cfg.disks   = [DiskConfig(path=disk_path, size_gb=disk_size, format=disk_format)]

        net = NetworkConfig(
            mode=args.get("network_mode", "nat"),
            bridge=args.get("bridge_iface", "virbr0") or "virbr0",
        )
        if args.get("mac_address"):
            net.mac = args["mac_address"]
        cfg.networks = [net]

        if args.get("iso_path"):
            cfg.iso_path = _resolve_iso(args["iso_path"])
        if cfg.machine_class == "laptop" or args.get("battery"):
            cfg.battery = True
        if "windows" in cfg.os_type.lower() and not profile:
            cfg.bios = "ovmf"
            cfg.uefi = True

        return manager.create_vm(cfg)

    # ── VM lifecycle ──────────────────────────────────────────────────────────
    elif tool_name == "clone_vm":
        return manager.clone_vm(args["source_name"], args["new_name"])

    elif tool_name == "launch_vm":
        return manager.launch_vm(
            args["name"],
            display=args.get("display"),
            dry_run=args.get("dry_run", False),
        )

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
        return manager.resize_disk(
            args["name"], args.get("disk_index", 0), args["new_size_gb"]
        )

    elif tool_name == "snapshot_create":
        return manager.snapshot_create(args["name"], args.get("snap_name", "snap1"))

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
        return manager.send_monitor_cmd(args["name"], args.get("cmd", "info status"))

    else:
        return {"success": False, "error": f"Unknown tool: {tool_name}"}

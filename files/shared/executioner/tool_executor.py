"""
tool_executor.py — Tool Execution Dispatch Layer

Single entry point execute_tool() that sanitises args, resolves VM
names, then dispatches to QemuManager or the config layer. Also owns
the manager singleton so all other modules share one instance.
"""

import json
import os
import re
import sys
from typing import Any, Dict

_CFG                 = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_VM_DEFS             = _CFG["create_vm_defaults"]
_TOOL_DEFS           = _CFG["tool_defaults"]
_VALID_MACHINE_TYPES = set(_CFG["valid_machine_types"])
_ARM_CPU_PREFIXES    = tuple(_CFG["arm_cpu_prefixes"])

from shared.api.qemu_config import (
    MachineConfig, DiskConfig, NetworkConfig,
    OVMF, apply_profile, check_profile_compatibility,
    check_system_capabilities, delete_custom_profile,
    get_all_profiles, list_profiles, save_custom_profile,
)
from shared.api.qemu_manager import QemuManager
from shared.sanitizer.sanitizer import (
    PLACEHOLDER_VM_NAMES,
    _resolve_iso, _resolve_vm_name, _sanitise_args,
)
from shared.sanitizer.context_gate import gate_check
from shared.preflight.validator import _preflight_check, _show_preflight_warning
from shared.display import (
    console,
    _render_compat, _render_monitor, _render_profiles,
    _render_snapshots, _render_status, _render_system,
    _render_vm_failure, _render_vm_list,
)
from shared.fingerprint import _tf_report
from rich.panel import Panel

manager = QemuManager()

# Stores the inverse action for the last reversible tool call.
# None means nothing to revert (either no tool ran yet or last tool was irreversible).
_last_revert_action: Dict[str, Any] = {}

# Tools that manage _last_revert_action themselves (set it on success, or
# explicitly clear it) — excluded from the blanket clear below so a failed
# attempt doesn't wipe out a still-valid revert from an earlier success.
_REVERT_AWARE_TOOLS = {
    "revert", "create_vm", "clone_vm", "launch_vm", "stop_vm",
    "create_profile", "update_config", "snapshot_create", "create_network",
    "resize_disk", "snapshot_restore", "snapshot_delete", "delete_network",
    "delete_vm",
}


def _set_revert(tool: str, args: dict, description: str) -> None:
    global _last_revert_action
    _last_revert_action = {"tool": tool, "args": args, "description": description}


def _clear_revert() -> None:
    global _last_revert_action
    _last_revert_action = {}


# Sanitizes args, resolves VM names, dispatches to the manager or config layer, and triggers Rich rendering.
# In: str tool_name, dict args, bool verbose → Out: Any result
def execute_tool(tool_name: str, args: Dict[str, Any], verbose: bool = False, skip_gate: bool = False) -> Any:
    _raw_os_type = args.get("os_type", "")  # capture before alias conversion
    args = _sanitise_args(tool_name, args)

    # Context gate — block execution and ask for missing required args
    if not skip_gate:
        gate_result = gate_check(tool_name, args)
        if gate_result:
            return gate_result

    # Resolve VM names by fuzzy match / index
    if "name" in args and tool_name not in (
        "create_vm", "create_profile", "clone_vm", "create_network"
    ):
        vms      = manager.list_vms()
        resolved = _resolve_vm_name(vms, str(args["name"]))
        if resolved:
            args["name"] = resolved

    # A revert action is only meaningful immediately after the call that set
    # it — any unrelated tool call in between means "undo my last action"
    # would target something the caller probably isn't thinking about
    # anymore, so drop it. Tools that manage the state themselves are
    # exempted (they set/clear it explicitly based on their own outcome).
    if tool_name not in _REVERT_AWARE_TOOLS:
        _clear_revert()

    # ── revert ────────────────────────────────────────────────────────────────
    if tool_name == "revert":
        if not _last_revert_action:
            return {"success": False, "error": "No reversible action to revert."}
        rev = dict(_last_revert_action)
        console.print(f"\n[yellow]↩ Revert: {rev['description']}[/yellow]")
        if not sys.stdin.isatty():
            console.print("[dim]Cancelled (no interactive terminal to confirm).[/dim]")
            return {"success": False, "error": "Revert cancelled: not running interactively."}
        try:
            answer = console.input("[bold cyan]Proceed? (y/n):[/bold cyan] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Cancelled.[/dim]")
            return {"success": False, "error": "Revert cancelled by user."}
        if answer != "y":
            return {"success": False, "error": "Revert cancelled by user."}
        _clear_revert()
        return execute_tool(rev["tool"], rev["args"], verbose)

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
        force = args.pop("force", False)
        if notes:
            args["_notes"] = notes

        if not force:
            preflight = _preflight_check(
                "create_profile", {"profile_name": pname, **args}, manager, verbose
            )
            action = preflight.get("action", "ok")

            if action == "abort":
                return {
                    "success":    False,
                    "error":      preflight.get("reason", "Pre-flight check failed"),
                    "correction": preflight.get("correction"),
                }

            if action == "ask_user":
                if not verbose:
                    _show_preflight_warning(preflight, console)
                return {
                    "success":    False,
                    "clarify":    True,
                    "question":   preflight.get("question"),
                    "options":    preflight.get("options", []),
                    "reason":     preflight.get("reason"),
                    "correction": preflight.get("correction"),
                    "issues":     preflight.get("issues", []),
                    "hint":       "To save anyway, call create_profile again with force=true",
                }

            if action == "auto_fix":
                fixed = preflight.get("fixed_args", {})
                args.update({k: v for k, v in fixed.items() if k not in ("profile_name", "force")})
                if not verbose:
                    console.print(f"  [yellow]⚠ Pre-flight auto-fixed: {preflight.get('reason')}[/yellow]")
                    for w in preflight.get("warnings", []):
                        console.print(f"  [dim]  ↳ {w}[/dim]")

        result = save_custom_profile(pname, args)
        if result["success"]:
            result["compatibility"] = check_profile_compatibility(result["profile_name"])
            _set_revert("delete_profile", {"profile_name": pname}, f"undo create_profile '{pname}'")
        return result

    elif tool_name == "delete_profile":
        _clear_revert()
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

        # Handle overwrite: delete the existing VM before recreating.
        if args.get("overwrite"):
            vm_dir = os.path.expanduser(f"~/.qemu_vms/{name}")
            if os.path.exists(vm_dir):
                result = manager.delete_vm(name, delete_disks=True)
                if not result.get("success"):
                    return {"success": False, "error": f"Could not overwrite '{name}': {result.get('error')}"}

        cfg = MachineConfig(
            name=name,
            os_type=args.get("os_type", "linux"),
            os_name=args.get("os_name", ""),
            description=args.get("description", ""),
        )

        # When explicit SMBIOS fingerprinting fields are present the user is doing
        # manual identity — suppress ALL profile application (auto-matched or AI-passed)
        # so the profile can't override machine_type, cpu_model, or other settings.
        _manual_smbios = any(args.get(f) for f in ("serial_number", "bios_vendor", "chassis_type", "smbios_type"))
        profile = None if _manual_smbios else args.get("profile")
        if not profile and not _manual_smbios:
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
                  "serial_number", "board_product", "bios_vendor", "smbios_type",
                  "uefi", "kvm", "battery", "hugepages", "machine_type", "os_type", "os_name",
                  "hardened", "stealth", "tpm", "bios"):
            if f in args and args[f] is not None and args[f] != "":
                setattr(cfg, f, args[f])

        if args.get("chassis_type"):
            cfg.smbios_type = args["chassis_type"]

        if args.get("extra_args"):
            cfg.extra_args = args["extra_args"]

        # stealth implies hardened — __post_init__ only runs at construction so
        # stealth applied via setattr or profile won't have triggered it yet.
        if cfg.stealth:
            cfg.hardened = True

        # Windows 11 requires TPM 2.0 — auto-enable unless explicitly disabled.
        if "windows" in cfg.os_type.lower() and not args.get("tpm") is False:
            cfg.tpm = True

        # hardened mode requires q35 (smm=off is only valid on q35);
        # also persist the settings that _harden() enforces at build time.
        if cfg.hardened:
            cfg.machine_type = "q35"
            cfg.balloon      = False
            cfg.hugepages    = False

        # Reject profile names used as machine_type
        if cfg.machine_type:
            mt = cfg.machine_type.lower().split(",")[0].strip()
            if mt not in _VALID_MACHINE_TYPES and not mt.startswith("pc-"):
                cfg.machine_type = "q35"

        # Windows 11 requires UEFI + q35
        if "windows" in cfg.os_type.lower() or "windows" in cfg.os_name.lower():
            cfg.uefi = True
            # Preserve ovmf_ms (Secure Boot) if explicitly set; fall back to plain ovmf
            if cfg.bios not in ("ovmf", "ovmf_ms"):
                cfg.bios = "ovmf"
            if cfg.machine_type not in ("q35",):
                cfg.machine_type = "q35"

        # Reject ARM CPU on x86 VM
        if cfg.machine_arch == "x86_64" and any(
            cfg.cpu_model.lower().startswith(p) for p in _ARM_CPU_PREFIXES
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

        disk_size   = int(args.get("disk_size_gb", _VM_DEFS["disk_size_gb"]))
        disk_format = args.get("disk_format", _VM_DEFS["disk_format"])
        disk_path   = os.path.expanduser(f"~/.qemu_vms/{cfg.name}/disk0.{disk_format}")
        is_windows  = "windows" in cfg.os_type.lower() or "windows" in cfg.os_name.lower()
        disk_bus    = args.get("disk_bus", "sata" if is_windows else _VM_DEFS.get("disk_bus", "virtio"))
        disk_model  = args.get("disk_model", "")
        cfg.disks   = [DiskConfig(path=disk_path, size_gb=disk_size, format=disk_format, bus=disk_bus, disk_model=disk_model)]

        net = NetworkConfig(
            mode=args.get("network_mode", _VM_DEFS["network_mode"]),
            bridge=args.get("bridge_iface", _VM_DEFS["bridge"]) or _VM_DEFS["bridge"],
            manufacturer_hint=args.get("manufacturer", ""),
        )
        if args.get("mac_address"):
            net.mac = args["mac_address"]
        cfg.networks = [net]

        # Auto-find ISO from distro name when no iso_path was given.
        # os_name is preferred; fall back to the raw os_type before alias conversion
        # (e.g. the AI passes os_type="mint" which sanitizer converts to "linux").
        _GENERIC_OS_NAMES = {"linux", "windows", "macos", "other", ""}
        _distro_hint = (cfg.os_name or _raw_os_type or "").lower().strip()
        if _distro_hint and _distro_hint not in _GENERIC_OS_NAMES and not cfg.os_name:
            cfg.os_name = _distro_hint
        if not args.get("iso_path") and _distro_hint and _distro_hint not in _GENERIC_OS_NAMES:
            resolved = _resolve_iso(_distro_hint)
            if resolved and os.path.exists(resolved):
                _fname = os.path.basename(resolved).lower()
                _arm_markers = ("arm64", "aarch64", "_arm_", "-arm-")
                _x86_markers = ("amd64", "x86_64", "x64", "i386", "i686", "64bit")
                _iso_is_arm  = any(m in _fname for m in _arm_markers)
                _iso_is_x86  = any(m in _fname for m in _x86_markers)
                _arch_ok = not (
                    (cfg.machine_arch == "x86_64" and _iso_is_arm) or
                    (cfg.machine_arch in ("aarch64", "arm") and _iso_is_x86)
                )
                if _arch_ok:
                    cfg.iso_path = resolved
                    if not verbose:
                        console.print(f"  [cyan]↳ Auto-found ISO for '{_distro_hint}': {os.path.basename(resolved)}[/cyan]")

        if args.get("iso_path"):
            cfg.iso_path = _resolve_iso(args["iso_path"])
        if cfg.machine_class == "laptop" or args.get("battery"):
            cfg.battery = True
        if "windows" in cfg.os_type.lower() and not profile:
            if cfg.bios not in ("ovmf", "ovmf_ms"):
                cfg.bios = "ovmf"
            cfg.uefi = True

        result = manager.create_vm(cfg)
        if not verbose:
            if result.get("success"):
                console.print(f"[green]✓ VM '{result['name']}' created at {result['vm_dir']}[/green]")
                if cfg.stealth:
                    manager.generate_guest_setup(name)
                    console.print(f"[dim]  Stealth guest setup script ready — will prompt automatically on first launch.[/dim]")
            else:
                console.print(f"[red]✗ create_vm failed: {result.get('error', 'unknown error')}[/red]")
        if result.get("success"):
            _set_revert("delete_vm", {"name": name}, f"undo create_vm '{name}'")
        return result

    # ── VM lifecycle ──────────────────────────────────────────────────────────
    elif tool_name == "clone_vm":
        result = manager.clone_vm(args["source_name"], args["new_name"])
        if result.get("success"):
            _set_revert("delete_vm", {"name": args["new_name"]}, f"undo clone_vm '{args['new_name']}'")
        return result

    elif tool_name == "launch_vm":
        result = manager.launch_vm(
            args["name"],
            display=args.get("display"),
            dry_run=args.get("dry_run", False),
        )
        if result.get("success"):
            _set_revert("stop_vm", {"name": args["name"], "force": True}, f"undo launch_vm '{args['name']}'")
        return result

    elif tool_name == "stop_vm":
        if args["name"] == "all":
            _clear_revert()
            return manager.stop_all()
        result = manager.stop_vm(args["name"], force=args.get("force", False))
        if result.get("success"):
            _set_revert("launch_vm", {"name": args["name"]}, f"undo stop_vm '{args['name']}'")
        return result

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
        # Capture old values before applying so we can revert
        _old_cfg = manager.show_config(args["name"])
        _updates = args.get("updates", {})
        result = manager.update_config(args["name"], _updates)
        if result.get("success") and _old_cfg.get("success"):
            _old_vals = {k: _old_cfg["config"].get(k) for k in _updates}
            _set_revert(
                "update_config",
                {"name": args["name"], "updates": _old_vals},
                f"undo update_config '{args['name']}' fields {list(_updates.keys())}",
            )
        return result

    elif tool_name == "resize_disk":
        _clear_revert()
        return manager.resize_disk(
            args["name"], args.get("disk_index", 0), args["new_size_gb"]
        )

    elif tool_name == "snapshot_create":
        _snap = args.get("snap_name", _TOOL_DEFS["snap_name"])
        result = manager.snapshot_create(args["name"], _snap)
        if result.get("success"):
            _set_revert(
                "snapshot_delete",
                {"name": args["name"], "snap_name": _snap},
                f"undo snapshot_create '{_snap}' on '{args['name']}'",
            )
        return result

    elif tool_name == "snapshot_list":
        result = manager.snapshot_list(args["name"])
        if not verbose:
            _render_snapshots(result)
        return result

    elif tool_name == "snapshot_restore":
        _clear_revert()
        return manager.snapshot_restore(args["name"], args["snap_name"])

    elif tool_name == "snapshot_delete":
        _clear_revert()
        return manager.snapshot_delete(args["name"], args["snap_name"])

    elif tool_name == "set_resource_limits":
        return manager.set_resource_limits(
            args["name"],
            cpu_percent=args.get("cpu_percent"),
            memory_mb=args.get("memory_mb"),
        )

    elif tool_name == "create_network":
        result = manager.create_network(args["net_name"])
        if result.get("success"):
            _set_revert("delete_network", {"net_name": args["net_name"]}, f"undo create_network '{args['net_name']}'")
        return result

    elif tool_name == "delete_network":
        _clear_revert()
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
        _clear_revert()
        return manager.delete_vm(args["name"], delete_disks=True)

    elif tool_name == "check_disk":
        return manager.check_disk(args["name"])

    elif tool_name == "get_vm_logs":
        result = manager.get_vm_logs(args["name"], lines=int(args.get("lines", _TOOL_DEFS["log_lines"])))
        if not verbose:
            _render_vm_failure(result)
        return result

    elif tool_name == "print_command":
        result = manager.print_command(args["name"])
        if result.get("success") and not verbose:
            console.print(Panel(result["command"], title="QEMU Command", border_style="cyan"))
            return {"success": True, "command": result["command"]}
        return result

    elif tool_name == "fingerprint_vm":
        return _tf_report(args["name"], summary=bool(args.get("summary", False)))

    elif tool_name == "send_monitor_cmd":
        return manager.send_monitor_cmd(args["name"], args.get("cmd", "info status"))

    else:
        return {"success": False, "error": f"Unknown tool: {tool_name}"}

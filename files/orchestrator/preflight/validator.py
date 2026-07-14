"""
validator.py — Pre-flight and Internet Validation Layer

Cross-checks AI tool call arguments against real-world data before
dispatch: local QEMU capability queries, DuckDuckGo product lookup,
CPU architecture consistency, and a pre-flight gate that can auto-fix
or ask the user before a destructive operation runs.

_preflight_check and _show_preflight_warning are called from
orchestrator.pipeline.execute_tool via _run() in the tool dispatch layer.
"""

import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from orchestrator.executor_client import (
    get_ovmf as _get_ovmf, get_capabilities as check_system_capabilities, get_all_profiles,
)
from orchestrator.sanitizer.sanitizer import PLACEHOLDER_VM_NAMES, REAL_HOME, VALID_MACHINE_TYPES, _resolve_iso
from .host_probe import (  # host/internet probing (extracted from this file)
    # used by the pre-flight logic below:
    set_custom_mode, _validate_profile_for_host,
    _validate_with_internet, _stealth_infer_from_product,
    # re-exported: set_custom_mode for external callers; the rest for tests:
    _get_qemu_machine_types, _get_qemu_cpu_models,
    _is_arm_cpu, _is_x86_cpu, _net_get, _net_head,
)

_CFG = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_THRESHOLDS = _CFG["thresholds"]

# ── Pre-flight constants ───────────────────────────────────────────────────────

_QEMU_CPU_MODELS        = set(_CFG["qemu_cpu_models"])
_LAPTOP_TYPE_KEYWORDS   = tuple(_CFG["laptop_type_keywords"])
_PREFLIGHT_TOOLS        = set(_CFG["preflight_tools"])
_PREFLIGHT_HW_FIELDS    = set(_CFG["preflight_hw_fields"])
_DESTRUCTIVE_MON_CMDS   = _CFG["destructive_monitor_cmds"]
_BAD_ISO_PATH_PATTERNS  = _CFG["bad_iso_path_patterns"]


# ── Pre-flight gate ────────────────────────────────────────────────────────────


def _validate_stealth_args(args: Dict[str, Any]) -> List[Dict]:
    """
    Return a list of issue dicts for a stealth VM config.
    Severity "error"   = directly exposes the VM (inxi / lspci / dmidecode will detect it).
    Severity "warning" = weakens stealth but doesn't break core detection bypass.
    """
    issues = []

    product_name = str(args.get("product_name", "")).strip()
    inferred     = _stealth_infer_from_product(product_name) if product_name else {}

    # ── SMBIOS identity fields ────────────────────────────────────────────────

    # manufacturer + product_name drive the inxi "System:" line.
    # Blank fields are themselves a detection signal.
    # If product_name was given and we can infer manufacturer, auto-fix rather than block.
    if not str(args.get("manufacturer", "")).strip():
        if inferred.get("manufacturer"):
            issues.append({
                "severity":  "auto_fix",
                "message":   f"stealth VM missing 'manufacturer' — inferred '{inferred['manufacturer']}' from product_name",
                "fix":       f"manufacturer set to '{inferred['manufacturer']}'",
                "fix_field": "manufacturer",
                "fix_value": inferred["manufacturer"],
                "auto_fix":  True,
            })
        else:
            issues.append({
                "severity":  "error",
                "message":   "stealth VM missing 'manufacturer' — inxi System line will be blank, a VM signal",
                "fix":       "Set manufacturer to match the spoofed hardware (e.g. 'Dell Inc.')",
                "fix_field": "manufacturer",
            })

    if not product_name:
        issues.append({
            "severity":  "error",
            "message":   "stealth VM missing 'product_name' — inxi product field will be blank, a VM signal",
            "fix":       "Set product_name to match the spoofed hardware (e.g. 'Latitude 5530')",
            "fix_field": "product_name",
        })

    # smbios_type drives chassis_type byte injection.
    # No smbios_type + no machine_class → chassis defaults to Desktop (type=3).
    # That is fine for a desktop profile, but a laptop fingerprint requires type=9 (Notebook).
    smbios_type   = str(args.get("smbios_type", "")).lower()
    machine_class = str(args.get("machine_class", "desktop")).lower()
    if not smbios_type:
        if inferred.get("smbios_type"):
            issues.append({
                "severity":  "auto_fix",
                "message":   f"stealth VM missing 'smbios_type' — inferred '{inferred['smbios_type']}' from product_name",
                "fix":       f"smbios_type set to '{inferred['smbios_type']}'",
                "fix_field": "smbios_type",
                "fix_value": inferred["smbios_type"],
                "auto_fix":  True,
            })
        elif any(k in machine_class for k in _LAPTOP_TYPE_KEYWORDS):
            issues.append({
                "severity":  "warning",
                "message":   "stealth VM has laptop machine_class but no smbios_type — chassis defaults to Desktop (type=3) not Laptop (type=9); inxi may call check_vm()",
                "fix":       "Set smbios_type='Notebook' to inject chassis_type=9",
                "fix_field": "smbios_type",
            })

    # bios_vendor spoofs SMBIOS type=0 (BIOS info).
    # Without it QEMU's "EFI Development Kit II" or "SeaBIOS" leaks through.
    if not str(args.get("bios_vendor", "")).strip():
        if inferred.get("bios_vendor"):
            issues.append({
                "severity":  "auto_fix",
                "message":   f"stealth VM missing 'bios_vendor' — inferred '{inferred['bios_vendor']}' from product_name",
                "fix":       f"bios_vendor set to '{inferred['bios_vendor']}'",
                "fix_field": "bios_vendor",
                "fix_value": inferred["bios_vendor"],
                "auto_fix":  True,
            })
        else:
            issues.append({
                "severity":  "warning",
                "message":   "stealth VM missing 'bios_vendor' — BIOS vendor will show QEMU/EFI defaults in dmidecode",
                "fix":       "Set bios_vendor matching the spoofed hardware (e.g. 'Dell Inc.')",
                "fix_field": "bios_vendor",
            })

    # bios_version appears in SMBIOS type=0 and is visible in dmidecode + WMI.
    # OVMF default is something like "0.0.0" or an edk2 build string.
    if not str(args.get("bios_version", "")).strip():
        issues.append({
            "severity":  "warning",
            "message":   "stealth VM missing 'bios_version' — OVMF default version string leaks in SMBIOS type=0",
            "fix":       "Set bios_version matching the spoofed hardware (e.g. '1.15.0' for a Dell)",
            "fix_field": "bios_version",
        })

    # serial_number is in SMBIOS type=1 — not checked by inxi but visible to
    # browser fingerprinting tools that read WMI (Windows) or dmidecode (Linux).
    if not str(args.get("serial_number", "")).strip():
        issues.append({
            "severity":  "warning",
            "message":   "stealth VM missing 'serial_number' — SMBIOS chassis/system serial will be empty (visible via dmidecode/WMI)",
            "fix":       "Set serial_number matching the spoofed hardware",
            "fix_field": "serial_number",
        })

    # ── GPU / display ─────────────────────────────────────────────────────────

    # virtio-vga / virtio-vga-gl carries PCI vendor 0x1af4 (Red Hat/QEMU).
    # lspci inside the guest shows this; it is one of the most common VM detection
    # vectors. create_vm auto-sets gpu="none" for stealth VMs that don't request a
    # specific GPU (qemu_arg_builder then picks vmware-svga on Linux / VGA on
    # Windows) — so this is informational only, not a blocking ask_user prompt.
    gpu = str(args.get("gpu", "virtio")).lower()
    if gpu == "virtio" and "gpu" in args:
        stealth_gpu_device = "VGA" if str(args.get("os_type", "")).lower() == "windows" else "vmware-svga"
        issues.append({
            "severity":  "warning",
            "message":   (
                "stealth VM GPU is 'virtio' (virtio-vga) — PCI vendor 0x1af4 (Red Hat/QEMU) "
                "is trivially detectable via lspci"
            ),
            "fix":       f"Remove the explicit gpu='virtio' to get the stealth default ({stealth_gpu_device}), or set gpu='qxl'",
            "fix_field": "gpu",
        })

    # SPICE display requires the SPICE guest agent and virtio-serial inside the VM.
    # The SPICE agent package name and the virtio-serial PCI device both reveal the VM.
    if str(args.get("display", "")).lower() == "spice":
        issues.append({
            "severity":  "error",
            "message":   "stealth VM using SPICE display — SPICE requires virtio-serial (PCI 0x1af4) and guest agent, both are VM signals",
            "fix":       "Use display='sdl' or display='gtk' instead",
            "fix_field": "display",
        })

    # ── Firmware ──────────────────────────────────────────────────────────────

    # UEFI=False means SeaBIOS. SeaBIOS sets BIOS vendor to "SeaBIOS" and
    # version to a build string — both are unambiguous VM signals in dmidecode.
    if args.get("uefi") is False:
        issues.append({
            "severity":  "error",
            "message":   "stealth VM has uefi=False — SeaBIOS sets BIOS vendor 'SeaBIOS', a clear VM signal in SMBIOS type=0",
            "fix":       "Use uefi=True (OVMF) so BIOS vendor/version can be spoofed via smbios args",
            "fix_field": "uefi",
        })

    # ── CPU model ─────────────────────────────────────────────────────────────

    # QEMU-named CPU models (qemu64, kvm64, etc.) expose a CPU model string that
    # doesn't match any real hardware — detectable via /proc/cpuinfo and CPUID tools.
    cpu_model = str(args.get("cpu_model", "host")).lower()
    if cpu_model in _QEMU_CPU_MODELS:
        issues.append({
            "severity":  "error",
            "message":   f"stealth VM cpu_model='{cpu_model}' is a QEMU synthetic model — /proc/cpuinfo will show a non-existent CPU, detectable by any fingerprinting tool",
            "fix":       "Use cpu_model='host' to pass through the real host CPU identity",
            "fix_field": "cpu_model",
        })

    return issues


def _triage(issues: List[Dict]) -> tuple:
    """Split an issue list into (blockers, auto_fixes, warnings).

    Args:
        issues: List of issue dicts, each with ``"severity"`` and optionally
                ``"auto_fix"`` keys.

    Returns:
        ``(blockers, auto_fixes, warnings)`` — three lists partitioned by
        severity and whether the issue can be fixed automatically.

    Example::

        blockers, fixes, warns = _triage([
            {"severity": "error",   "message": "no KVM"},
            {"severity": "warning", "auto_fix": True, "message": "low RAM"},
            {"severity": "warning", "message": "no OVMF"},
        ])
        # blockers → [{"severity": "error", ...}]
        # fixes    → [{"severity": "warning", "auto_fix": True, ...}]
        # warns    → [{"severity": "warning", "message": "no OVMF"}]
    """
    return (
        [i for i in issues if i["severity"] == "error"],
        [i for i in issues if i.get("auto_fix") and i["severity"] != "error"],
        [i for i in issues if i["severity"] == "warning" and not i.get("auto_fix")],
    )


def _preflight_create_vm(args: Dict[str, Any], manager: object, verbose: bool,
                         stateless_only: bool) -> Dict[str, Any]:
    """Pre-flight validation for create_vm (name/ISO/arch/memory/profile checks
    and the destructive-unattended confirmation gate). Extracted from
    _preflight_check() to keep the per-tool dispatch readable.

    Returns an action dict — {"action": "ok"} when nothing needs attention,
    else "auto_fix" / "ask_user" / "abort" with the relevant fields.

    Example::
        _preflight_create_vm({"name": "dev", "os_type": "linux"}, mgr, False, False)
        # -> {"action": "ok"}
    """
    name     = str(args.get("name", "")).strip()
    iso_path = str(args.get("iso_path", "")).strip()
    os_type  = str(args.get("os_type", "")).lower()
    mt       = str(args.get("machine_type", "")).lower()

    if not name or name.lower() in PLACEHOLDER_VM_NAMES:
        return {"action":"ask_user","reason":f"VM name is missing or looks invented (got: '{name}')","question":"What would you like to name this VM?","fix_field":"name","options":["my-windows-vm","dev-box","test-ubuntu"]}

    if not stateless_only:
        try:
            _known = {v.get("name") for v in (manager.list_vms() if manager else [])}
        except Exception:
            _known = set()
        if name in _known:
            return {"action":"ask_user","reason":f"A VM named '{name}' already exists","question":f"A VM called '{name}' already exists. Overwrite it, or use a different name?","fix_field":"name","original_name":name,"options":[f"{name}-2",f"{name}-new","overwrite"],"correction":"Use a different name or delete the existing VM first."}

    # Destructive opt-in: unattended install WIPES the target disk — confirm first
    # (bypassed by force=true, like delete_vm). Windows normally also auto-creates
    # a local admin account unless unattended_skip_user leaves that step manual;
    # Linux's autoinstall/preseed always leaves account creation for a human.
    _is_win = "windows" in os_type or "windows" in str(args.get("os_name", "")).lower()
    if args.get("unattended") and not args.get("force"):
        if _is_win and not args.get("unattended_skip_user"):
            _acct = args.get("unattended_username") or "user"
            return {"action":"ask_user","reason":"Unattended install wipes the target disk and auto-creates a local admin account","question":f"Unattended Windows install will WIPE this VM's disk and auto-create local admin '{_acct}'. Proceed?","fix_field":None,"options":["Yes, wipe and install","No, cancel"],"correction":"On 'Yes' the client re-runs with force=true; the disk is erased/repartitioned and a known-password admin account is created."}
        else:
            _os_label = "Windows" if _is_win else "Linux"
            return {"action":"ask_user","reason":"Unattended install wipes the target disk, stopping at account creation for you to set up manually","question":f"Unattended {_os_label} install will WIPE this VM's disk and auto-partition it, stopping at the account-creation screen. Proceed?","fix_field":None,"options":["Yes, wipe and install","No, cancel"],"correction":"On 'Yes' the client re-runs with force=true; the disk is erased/repartitioned."}

    if mt and mt not in VALID_MACHINE_TYPES and not mt.startswith("pc-"):
        fixed = dict(args)
        if mt in get_all_profiles():
            fixed["profile"] = mt
        fixed.pop("machine_type", None)
        return {"action":"auto_fix","reason":f"machine_type='{mt}' is a profile name, not a machine type","correction":f"Set profile='{mt}' and removed invalid machine_type","fixed_args":fixed}

    if iso_path and not stateless_only:
        bad_path = any([
            any(p in iso_path for p in _BAD_ISO_PATH_PATTERNS),
            not os.path.exists(os.path.expanduser(re.sub(r"^/home/[^/]+/", REAL_HOME+"/", iso_path))),
        ])
        if bad_path:
            resolved = _resolve_iso(iso_path)
            if resolved and os.path.exists(resolved):
                fixed = dict(args); fixed["iso_path"] = resolved
                return {"action":"auto_fix","reason":f"ISO path '{iso_path}' doesn't exist — auto-resolved to '{resolved}'","correction":f"iso_path corrected to: {resolved}","fixed_args":fixed}
            else:
                isos = manager.scan_isos()
                if isos:
                    return {"action":"ask_user","reason":f"ISO '{iso_path}' not found on disk","question":"Can't find that ISO. Which file did you mean?","fix_field":"iso_path","options":[iso["name"] for iso in isos[:4]]+["skip ISO"],"iso_list":isos}
                else:
                    fixed = dict(args); fixed.pop("iso_path", None)
                    return {"action":"auto_fix","reason":f"ISO '{iso_path}' not found — no ISOs found anywhere","correction":"Removed iso_path. VM will be created without an install ISO.","fixed_args":fixed}

    if iso_path and not stateless_only and os.path.exists(iso_path):
        iso_lower = os.path.basename(iso_path).lower()
        if any(k in iso_lower for k in ("arm64","aarch64")) and str(args.get("machine_arch","x86_64")).lower() == "x86_64":
            return {"action":"ask_user","reason":f"ARM64 ISO '{os.path.basename(iso_path)}' with x86_64 VM — incompatible","question":"This is an ARM64 ISO. Do you want an ARM64 VM, or an x86_64 ISO instead?","fix_field":None,"options":["Use ARM64 VM","Get x86_64 ISO instead"],"correction":f"For x86_64: download Windows 11 x64 from microsoft.com"}

    is_win = "windows" in os_type or "win" in os_type
    if is_win and args.get("uefi") is False:
        fixed = dict(args); fixed["uefi"] = True; fixed["bios"] = "ovmf"
        return {"action":"auto_fix","reason":"Windows 11 requires UEFI but uefi=False was set","correction":"Forced uefi=True and bios=ovmf","fixed_args":fixed}

    disk_gb = int(args.get("disk_size_gb", _THRESHOLDS["min_windows_disk_gb"]))
    if is_win and disk_gb < _THRESHOLDS["min_windows_disk_gb"]:
        _win_disk = _THRESHOLDS["auto_windows_disk_gb"]
        fixed = dict(args); fixed["disk_size_gb"] = _win_disk
        return {"action":"auto_fix","reason":f"Windows 11 needs at least {_win_disk}GB disk, got {disk_gb}GB","correction":f"Increased disk_size_gb from {disk_gb} to {_win_disk}","fixed_args":fixed}

    if is_win and args.get("tpm") is not False and not stateless_only:
        import shutil
        if not shutil.which("swtpm"):
            return {
                "action":     "ask_user",
                "reason":     "Windows 11 requires TPM 2.0 but swtpm is not installed",
                "question":   "Install swtpm for TPM 2.0 support, or proceed without it (Windows 11 setup will block)?",
                "fix_field":  None,
                "options":    ["Install swtpm first (sudo apt install swtpm)", "Proceed without TPM (bypass during install)"],
                "correction": "sudo apt install swtpm",
            }

    if args.get("stealth"):
        stealth_issues            = _validate_stealth_args(args)
        blockers, auto_fixes, warnings = _triage(stealth_issues)

        # Apply auto-fixes first (inferred from product_name)
        if auto_fixes:
            fixed = dict(args)
            for issue in auto_fixes:
                if issue.get("fix_field") and issue.get("fix_value") is not None:
                    fixed.setdefault(issue["fix_field"], issue["fix_value"])
            fix_notes = [f"{i['fix_field']}={i['fix_value']!r}" for i in auto_fixes]
            return {
                "action":     "auto_fix",
                "reason":     "Stealth preflight inferred missing SMBIOS fields from product_name: " + ", ".join(fix_notes),
                "correction": " | ".join(i["fix"] for i in auto_fixes),
                "fixed_args": fixed,
                "warnings":   [w["message"] for w in warnings],
            }

        if blockers:
            return {
                "action":     "ask_user",
                "reason":     " | ".join(i["message"] for i in blockers),
                "question":   "Stealth mode requires hardware identity fields to spoof SMBIOS. Provide them or proceed with partial masking?",
                "fix_field":  blockers[0].get("fix_field"),
                "options":    ["Provide the missing fields", "Proceed anyway (partial masking)"],
                "correction": " | ".join(i["fix"] for i in blockers if i.get("fix")),
                "issues":     stealth_issues,
            }
        if warnings:
            # Non-blocking — surface as warnings in the result but continue
            args = dict(args)
            args.setdefault("_stealth_warnings", [w["message"] for w in warnings])

    if not stateless_only:
        internet_issues = _validate_with_internet(args, verbose=verbose)
        if internet_issues:
            blockers, auto_fixes, warnings = _triage(internet_issues)
            if blockers:
                return {"action":"ask_user","reason":" | ".join(i["message"] for i in blockers),"question":"Pre-flight found issues with this VM config. Proceed anyway or fix first?","fix_field":None,"options":["Proceed anyway","Cancel and fix"],"correction":" | ".join(i["fix"] for i in blockers if i.get("fix")),"issues":internet_issues}
            if auto_fixes:
                fixed = dict(args); fix_notes = []
                for issue in auto_fixes:
                    if issue.get("fix_field") and issue.get("fix_value") is not None:
                        fixed[issue["fix_field"]] = issue["fix_value"]
                        fix_notes.append(f"{issue['fix_field']}={issue['fix_value']!r}")
                return {"action":"auto_fix","reason":"Internet/QEMU validation auto-fixed: "+", ".join(fix_notes),"correction":" | ".join(i["message"] for i in auto_fixes),"fixed_args":fixed,"warnings":[i["message"] for i in warnings]}

        profile_name = args.get("profile") or mt
        if profile_name:
            profile_issues = _validate_profile_for_host(profile_name)
            if profile_issues:
                blockers, auto_fixes, warnings = _triage(profile_issues)
                if blockers:
                    return {"action":"ask_user","reason":f"Profile '{profile_name}' has compatibility issues: {' | '.join(i['message'] for i in blockers)}","question":f"Profile '{profile_name}' may not work on this system. Proceed anyway or cancel?","fix_field":None,"options":["Proceed anyway","Cancel","Use minimal profile instead"],"correction":" | ".join(i["fix"] for i in blockers if i.get("fix")),"issues":profile_issues}
                if auto_fixes:
                    fixed = dict(args); fix_notes = []
                    for issue in auto_fixes:
                        if issue.get("fix_field") and issue.get("fix_value") is not None:
                            fixed[issue["fix_field"]] = issue["fix_value"]
                            fix_notes.append(f"{issue['fix_field']}={issue['fix_value']}")
                    return {"action":"auto_fix","reason":f"Profile '{profile_name}': auto-fixed "+", ".join(fix_notes),"correction":" | ".join(i["message"] for i in auto_fixes),"fixed_args":fixed,"warnings":[i["message"] for i in warnings]}

    return {"action": "ok"}


def _preflight_check(
    tool_name:     str,
    args:          Dict[str, Any],
    manager: object,                       # QemuManager — passed in to avoid circular import
    verbose:       bool = False,
    stateless_only: bool = False,  # True on the AI provider (remote mode): skip checks
                                   # that require real filesystem/binary/manager state.
                                   # The client machine always runs the full check.
) -> Dict[str, Any]:
    """
    Validate tool call args before execution.
    Returns {"action": "ok"|"auto_fix"|"ask_user"|"abort", ...}

    stateless_only=True  — shape/type/logic checks only (no fs, no manager, no subprocess).
                           Used by the AI provider in remote mode so it can still catch
                           AI hallucinations early without needing the client's real state.
    stateless_only=False — full check including real VM/disk/binary state.
                           Always used in local mode and by the client machine.
    """
    ok = {"action": "ok"}

    if tool_name not in _PREFLIGHT_TOOLS:
        return ok

    if tool_name == "create_vm":
        return _preflight_create_vm(args, manager, verbose, stateless_only)

    elif tool_name == "create_profile":
        profile_name  = str(args.get("profile_name", "")).strip()
        profile_data  = {k: v for k, v in args.items() if k not in ("profile_name", "force")}

        if not any(f in profile_data for f in _PREFLIGHT_HW_FIELDS):
            return {
                "action":     "abort",
                "reason":     f"Profile '{profile_name}' has no hardware configuration — only a description was provided.",
                "correction": "Provide at least cpu_model, machine_type, and memory_mb when creating a profile.",
            }

        profile_issues = [] if stateless_only else (
            _validate_profile_for_host(profile_name, profile_data=profile_data)
            + _validate_with_internet(profile_data, verbose=verbose)
        )
        if profile_issues:
            blockers, auto_fixes, warnings = _triage(profile_issues)
            if blockers:
                return {
                    "action":      "ask_user",
                    "reason":      " | ".join(i["message"] for i in blockers),
                    "question":    f"Profile '{profile_name}' has compatibility issues. Save anyway or cancel?",
                    "fix_field":   None,
                    "options":     ["Save anyway", "Cancel"],
                    "correction":  " | ".join(i["fix"] for i in blockers if i.get("fix")),
                    "issues":      profile_issues,
                }
            if auto_fixes:
                fixed      = dict(args)
                fix_notes  = []
                for issue in auto_fixes:
                    if issue.get("fix_field") and issue.get("fix_value") is not None:
                        fixed[issue["fix_field"]] = issue["fix_value"]
                        fix_notes.append(f"{issue['fix_field']}={issue['fix_value']!r}")
                return {
                    "action":      "auto_fix",
                    "reason":      "Pre-flight auto-fixed: " + ", ".join(fix_notes),
                    "correction":  " | ".join(i["message"] for i in auto_fixes),
                    "fixed_args":  fixed,
                    "warnings":    [i["message"] for i in warnings],
                }

    elif tool_name == "launch_vm" and not stateless_only:
        name = str(args.get("name", "")).strip()
        if name:
            # Check VM existence via manager (works in both local and split/remote mode)
            vm_exists = False
            candidates = []
            try:
                all_vms = manager.list_vms() if hasattr(manager, "list_vms") else []
                vm_names = [v["name"] for v in all_vms if isinstance(v, dict) and "name" in v]
                vm_exists = name in vm_names
                if not vm_exists:
                    candidates = [n for n in vm_names if name.lower() in n.lower()]
            except Exception:
                vm_dir = os.path.join(os.path.expanduser("~"), ".qemu_vms", name)
                vm_exists = os.path.exists(vm_dir)
            if not vm_exists:
                if candidates:
                    return {"action":"abort","reason":f"VM '{name}' not found. Did you mean: {candidates}?","correction":f"Use one of these names: {candidates}"}
                return {"action":"abort","reason":f"VM '{name}' doesn't exist. Create it first.","correction":"Call create_vm before launch_vm."}
        try:
            from executor.api.qemu_config import MachineConfig
            cfg = MachineConfig.load(name)
            if cfg.iso_path and not os.path.exists(cfg.iso_path):
                return {"action":"ask_user","reason":f"ISO file missing: {cfg.iso_path}","question":f"The ISO '{os.path.basename(cfg.iso_path)}' is missing. Launch without ISO, or fix the path?","fix_field":None,"options":["Launch anyway (no ISO)","Cancel"]}
        except Exception:
            pass  # preflight is advisory — unreadable config skips the ISO check rather than blocking launch

    elif tool_name == "delete_vm":
        name = str(args.get("name", "")).strip()
        if name and not args.get("force"):
            return {"action":"ask_user","reason":f"Destructive operation: delete VM '{name}'","question":f"Are you sure you want to delete '{name}'?","fix_field":None,"options":["Yes, delete it","No, keep it"],"correction":"Deletion cannot be undone without recreating the VM."}

    elif tool_name == "remove_template":
        name = str(args.get("name", "")).strip()
        if name and not args.get("force"):
            return {"action":"ask_user","reason":f"Remove template '{name}'","question":f"Delete the template copy for '{name}'? This permanently removes the golden disk copy.","fix_field":None,"options":["Yes, remove it","No, cancel"],"correction":"The golden disk copy cannot be recovered without re-marking the source VM (if it still exists)."}

    elif tool_name == "resize_disk" and not stateless_only:
        name     = str(args.get("name", "")).strip()
        new_size = int(args.get("new_size_gb", 0))
        if name and new_size:
            # Check VM existence via manager (works in both local and remote/split mode)
            try:
                known_vms = {v.get("name") for v in (manager.list_vms() if manager else [])}
                if name not in known_vms:
                    return {"action":"abort","reason":f"VM '{name}' does not exist — cannot resize disk","correction":"Create the VM first with create_vm, then resize."}
            except Exception:
                pass  # advisory check — if listing VMs fails, skip existence check rather than block resize
            # Attempt to read disk size for shrink guard (local mode only; silently skipped in split mode)
            try:
                from executor.api.qemu_config import MachineConfig
                cfg = MachineConfig.load(name)
                if cfg.disks:
                    current = cfg.disks[0].size_gb
                    if new_size < current:
                        return {"action":"abort","reason":f"Cannot shrink disk from {current}GB to {new_size}GB — QEMU doesn't support shrinking","correction":f"new_size_gb must be >= current size ({current}GB)"}
            except Exception:
                pass  # advisory check — unreadable config skips the shrink check rather than blocking resize

    elif tool_name == "send_monitor_cmd":
        cmd = str(args.get("cmd", "")).strip().lower()
        if any(d in cmd for d in _DESTRUCTIVE_MON_CMDS):
            return {"action":"ask_user","reason":f"Potentially destructive monitor command: '{cmd}'","question":f"Run QEMU monitor command '{cmd}'? This may affect the running VM.","fix_field":None,"options":["Yes, run it","No, cancel"]}

    elif tool_name in ("snapshot_restore", "snapshot_delete"):
        name      = str(args.get("name", "")).strip()
        snap_name = str(args.get("snap_name", "")).strip()
        if not args.get("force"):
            verb = "restore" if tool_name == "snapshot_restore" else "delete"
            return {"action":"ask_user","reason":f"Snapshot {verb}: '{snap_name}' on VM '{name}'","question":f"Confirm {verb} snapshot '{snap_name}' on '{name}'?","fix_field":None,"options":[f"Yes, {verb} it","No, cancel"],"correction":"Snapshot restore replaces current VM state. Snapshot delete is permanent."}

    return ok


# Renders a yellow warning panel and presents the pre-flight options to the user.
# In: dict preflight, Console → Out: nothing (console output)
def _show_preflight_warning(preflight: Dict, console: object) -> None:
    """Display a pre-flight warning panel and present options to the user."""
    from rich.panel import Panel
    reason     = preflight.get("reason", "")
    question   = preflight.get("question", "Confirm?")
    options    = preflight.get("options", [])
    correction = preflight.get("correction", "")

    lines = [f"[yellow]⚠[/yellow] {reason}"]
    if correction:
        lines.append(f"[dim]{correction}[/dim]")

    console.print(Panel(
        "\n".join(lines),
        title="[bold yellow]Pre-flight Check[/bold yellow]",
        border_style="yellow",
    ))

    opts_str = "  ".join(f"[dim][{o}][/dim]" for o in options) if options else ""
    console.print(f"\n[ai]Assistant:[/ai] {question}  {opts_str}\n")

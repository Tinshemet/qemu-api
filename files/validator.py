"""
validator.py — Pre-flight and Internet Validation Layer

Cross-checks AI tool call arguments against real-world data before
dispatch: local QEMU capability queries, DuckDuckGo product lookup,
CPU architecture consistency, and a pre-flight gate that can auto-fix
or ask the user before a destructive operation runs.

Note: _preflight_check and _show_preflight_warning are fully implemented
but not yet wired into the chat loop. They are ready to be called from
tool_executor.execute_tool when integrated.
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

from qemu_config import OVMF, check_system_capabilities, get_all_profiles
from sanitizer   import PLACEHOLDER_VM_NAMES, REAL_HOME, VALID_MACHINE_TYPES, _resolve_iso

# ── Global flags ───────────────────────────────────────────────────────────────

_NET_CACHE:   Dict[str, Any] = {}
_NET_TIMEOUT  = 4
_NET_ENABLED  = True
_CUSTOM_MODE  = False   # set True via set_custom_mode() to skip product verification

# Cache for QEMU capability queries (expensive — run once per session)
_QEMU_MACHINES_CACHE: Optional[set] = None
_QEMU_CPUS_CACHE:     Optional[set] = None


def set_custom_mode(enabled: bool):
    global _CUSTOM_MODE
    _CUSTOM_MODE = enabled


# ── Network utilities ──────────────────────────────────────────────────────────

def _net_get(url: str, headers: Dict = None) -> Optional[Dict]:
    """Fetch JSON from a URL with session caching and timeout. Returns None on failure."""
    if not _NET_ENABLED:
        return None
    cache_key = hashlib.md5(url.encode()).hexdigest()
    if cache_key in _NET_CACHE:
        return _NET_CACHE[cache_key]
    try:
        req = urllib.request.Request(url, headers=headers or {"User-Agent": "qemu-api/1.0"})
        with urllib.request.urlopen(req, timeout=_NET_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            _NET_CACHE[cache_key] = data
            return data
    except Exception:
        _NET_CACHE[cache_key] = None
        return None


def _net_head(url: str) -> bool:
    """Check if a URL exists via HEAD request. Returns False on failure."""
    if not _NET_ENABLED:
        return False
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "qemu-api/1.0"})
        with urllib.request.urlopen(req, timeout=_NET_TIMEOUT):
            return True
    except Exception:
        return False


# ── Local QEMU capability queries ──────────────────────────────────────────────

def _get_qemu_machine_types(binary: str = "qemu-system-x86_64") -> set:
    """Ask the local QEMU binary what machine types it supports."""
    global _QEMU_MACHINES_CACHE
    if _QEMU_MACHINES_CACHE is not None:
        return _QEMU_MACHINES_CACHE
    try:
        result = subprocess.run([binary, "-machine", "help"], capture_output=True, text=True, timeout=5)
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
        result = subprocess.run([binary, "-cpu", "help"], capture_output=True, text=True, timeout=5)
        cpus = set()
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if parts and not parts[0].startswith("-"):
                cpus.add(parts[0].lower())
        _QEMU_CPUS_CACHE = cpus
        return cpus
    except Exception:
        return set()


# ── CPU architecture classification ───────────────────────────────────────────

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


# ── DuckDuckGo product lookup ──────────────────────────────────────────────────

def _lookup_product(manufacturer: str, product: str) -> Dict[str, Any]:
    query  = f"{manufacturer} {product} laptop desktop specifications"
    params = urllib.parse.urlencode({"q": query, "format": "json", "no_html": "1"})
    data   = _net_get(f"https://api.duckduckgo.com/?{params}")
    if not data:
        return {}
    return {
        "found":   bool(data.get("AbstractText") or data.get("Answer")),
        "summary": (data.get("AbstractText") or data.get("Answer") or "")[:300],
        "source":  data.get("AbstractSource", ""),
    }


_MS_WINDOWS_ISO_PAGE = "https://www.microsoft.com/software-download/windows11"


# ── Internet / local QEMU validation ──────────────────────────────────────────

def _validate_with_internet(args: Dict[str, Any], verbose: bool = False) -> List[Dict]:
    """
    Cross-check AI-provided hardware assumptions against real-world data.
    Non-blocking — all failures are warnings, not hard errors.
    Returns list of issue dicts with severity / auto_fix / fix_field / fix_value.
    """
    issues      = []
    qemu_binary = args.get("qemu_binary", "qemu-system-x86_64")

    # 1. Machine type
    machine_type = str(args.get("machine_type", "q35")).lower().split(",")[0].strip()
    if machine_type and machine_type not in ("", "none"):
        supported = _get_qemu_machine_types(qemu_binary)
        if supported and machine_type not in supported:
            close = [m for m in supported if machine_type[:3] in m][:3]
            issues.append({
                "severity": "error",
                "message":  f"Machine type '{machine_type}' is not supported by your installed QEMU",
                "fix":      f"Supported types include: {close or list(supported)[:5]}",
                "auto_fix": False, "source": "local_qemu",
            })

    # 2. CPU model
    cpu_model = str(args.get("cpu_model", "host")).strip()
    if cpu_model and cpu_model not in ("host", "kvm64", "qemu64", "max"):
        supported_cpus = _get_qemu_cpu_models(qemu_binary)
        if supported_cpus:
            cpu_lower = cpu_model.lower()
            if cpu_lower not in supported_cpus and not _is_arm_cpu(cpu_model):
                close = [c for c in supported_cpus if cpu_lower[:4] in c][:3]
                if not close:
                    issues.append({
                        "severity":  "warning",
                        "message":   f"CPU model '{cpu_model}' not found in QEMU's cpu list",
                        "fix":       "Try: host, kvm64, or a named model. Run: qemu-system-x86_64 -cpu help",
                        "auto_fix":  True, "fix_field": "cpu_model", "fix_value": "host",
                        "source":    "local_qemu",
                    })

    # 3. CPU / arch consistency
    machine_arch = str(args.get("machine_arch", "x86_64")).lower()
    if _is_arm_cpu(cpu_model) and machine_arch == "x86_64":
        issues.append({
            "severity":  "error",
            "message":   f"CPU '{cpu_model}' is an ARM processor but VM arch is x86_64",
            "fix":       "Either use an x86 CPU model or set machine_arch=aarch64",
            "auto_fix":  True, "fix_field": "cpu_model", "fix_value": "host",
            "source":    "local_knowledge",
        })
    elif _is_x86_cpu(cpu_model) and machine_arch in ("aarch64", "arm"):
        issues.append({
            "severity":  "error",
            "message":   f"CPU '{cpu_model}' is an x86 processor but VM arch is {machine_arch}",
            "fix":       "Either use an ARM CPU model (cortex-a72) or set machine_arch=x86_64",
            "auto_fix":  True, "fix_field": "cpu_model", "fix_value": "cortex-a72",
            "source":    "local_knowledge",
        })

    # 4. Product verification via DuckDuckGo
    manufacturer = str(args.get("manufacturer", "")).strip()
    product_name = str(args.get("product_name", "")).strip()
    if manufacturer and product_name and _NET_ENABLED and not _CUSTOM_MODE:
        result = _lookup_product(manufacturer, product_name)
        if result and not result.get("found"):
            issues.append({
                "severity": "warning",
                "message":  f"Could not verify '{manufacturer} {product_name}' as a real product online",
                "fix":      "Check manufacturer and product_name — SMBIOS spoofing works best with real product names",
                "auto_fix": False, "source": "duckduckgo",
            })

    # 5. Memory sanity vs known product specs
    memory_mb = int(args.get("memory_mb", 0))
    if memory_mb and product_name and not _CUSTOM_MODE:
        prod_lower = product_name.lower()
        if any(k in prod_lower for k in ("g15", "thinkpad", "inspiron")) and memory_mb > 65536:
            issues.append({
                "severity": "warning",
                "message":  f"'{product_name}' typically supports max 32-64GB RAM, got {memory_mb//1024}GB",
                "fix":      "Reduce memory_mb to match the actual product's maximum",
                "auto_fix": False, "source": "local_knowledge",
            })

    # 6. Windows ISO architecture hint
    os_type  = str(args.get("os_type", "")).lower()
    iso_path = str(args.get("iso_path", ""))
    if ("windows" in os_type or "win" in os_type) and iso_path:
        iso_lower = os.path.basename(iso_path).lower()
        if any(k in iso_lower for k in ("arm64", "aarch64", "arm_")) and machine_arch == "x86_64":
            issues.append({
                "severity": "error",
                "message":  "ARM64 Windows ISO with x86_64 VM — will not boot",
                "fix":      f"Get x86_64 ISO from: {_MS_WINDOWS_ISO_PAGE}",
                "auto_fix": False, "source": "iso_filename",
            })

    return issues


def _validate_profile_for_host(profile_name: str) -> List[Dict[str, Any]]:
    """
    Validate any profile (built-in or custom) against the current host.
    Returns a list of issue dicts.
    """
    import shutil as _shutil

    issues       = []
    all_profiles = get_all_profiles()
    profile      = all_profiles.get(profile_name)
    if not profile:
        return []

    caps   = check_system_capabilities()
    arch   = profile.get("machine_arch", "x86_64")
    binary = profile.get("qemu_binary", "qemu-system-x86_64")

    if arch in ("aarch64", "arm") and not caps.get("qemu_arm_installed"):
        issues.append({"severity":"error","message":f"Profile '{profile_name}' needs qemu-system-aarch64 which is not installed","fix":"sudo apt install qemu-system-arm","auto_fix":False})
    if binary and not _shutil.which(binary):
        issues.append({"severity":"error","message":f"Required QEMU binary '{binary}' not found","fix":f"sudo apt install {'qemu-system-arm' if 'aarch64' in binary else 'qemu-system-x86'}","auto_fix":False})

    if profile.get("kvm", True) and arch in ("aarch64","arm") and caps.get("host_arch","x86_64") == "x86_64":
        issues.append({"severity":"warning","message":f"Profile '{profile_name}' has kvm=True but ARM guests can't use KVM on x86","fix":"kvm will be forced to False automatically","auto_fix":True,"fix_field":"kvm","fix_value":False})

    if profile.get("uefi") and not OVMF["available"] and profile.get("bios","ovmf") in ("ovmf","ovmf_ms"):
        issues.append({"severity":"warning","message":f"Profile '{profile_name}' requires UEFI but OVMF not found","fix":"sudo apt install ovmf","auto_fix":True,"fix_field":"bios","fix_value":"seabios"})

    if profile.get("hugepages"):
        try:
            with open("/proc/sys/vm/nr_hugepages") as f:
                if int(f.read().strip()) == 0:
                    issues.append({"severity":"error","message":f"Profile '{profile_name}' uses hugepages but none are allocated","fix":"sudo sysctl vm.nr_hugepages=2048","auto_fix":True,"fix_field":"hugepages","fix_value":False})
        except Exception:
            pass

    profile_mem = int(profile.get("memory_mb", 2048))
    host_mem    = caps.get("host_memory_mb", 0)
    if host_mem > 0 and profile_mem > host_mem:
        issues.append({"severity":"warning","message":f"Profile requests {profile_mem}MB RAM but host only has {host_mem}MB","fix":f"Reduce memory_mb to {host_mem//2} or less","auto_fix":True,"fix_field":"memory_mb","fix_value":min(profile_mem, int(host_mem*0.85))})

    profile_cores = int(profile.get("cpu_cores", 2))
    host_cores    = caps.get("host_cpu_cores", 1)
    if profile_cores > host_cores * 2:
        issues.append({"severity":"warning","message":f"Profile requests {profile_cores} cores but host only has {host_cores} — heavy over-commit","fix":f"Reduce cpu_cores to {host_cores} or less","auto_fix":True,"fix_field":"cpu_cores","fix_value":host_cores})

    free_gb = caps.get("home_free_gb", 999)
    if free_gb < 10:
        issues.append({"severity":"error","message":f"Only {free_gb}GB free in home directory","fix":"Free up disk space before creating the VM","auto_fix":False})
    elif free_gb < 60:
        issues.append({"severity":"warning","message":f"Only {free_gb}GB free — VM disk image may exceed available space","fix":"Use a smaller disk_size_gb or free up space","auto_fix":False})

    mt = profile.get("machine_type", "q35")
    if "raspi" in mt and "aarch64" not in binary:
        issues.append({"severity":"error","message":f"Profile '{profile_name}' uses raspi machine type but qemu_binary is not aarch64","fix":"Set qemu_binary=qemu-system-aarch64 in the profile","auto_fix":True,"fix_field":"qemu_binary","fix_value":"qemu-system-aarch64"})

    notes = profile.get("_notes", "")
    if notes and "slow" in notes.lower():
        issues.append({"severity":"warning","message":f"Profile note: {notes}","fix":"","auto_fix":False})

    if profile.get("_custom"):
        cpu_model   = profile.get("cpu_model", "host")
        arm_prefixes = ("cortex", "arm1", "arm9", "arm11")
        if any(cpu_model.lower().startswith(p) for p in arm_prefixes) and arch == "x86_64":
            issues.append({"severity":"error","message":f"Custom profile '{profile_name}' has ARM cpu_model='{cpu_model}' but machine_arch=x86_64","fix":"Change cpu_model to 'host' or set machine_arch=aarch64","auto_fix":True,"fix_field":"cpu_model","fix_value":"host"})
        missing = [f for f in ("manufacturer","product_name") if not profile.get(f)]
        if missing:
            issues.append({"severity":"warning","message":f"Custom profile '{profile_name}' is missing SMBIOS fields: {missing}","fix":"Add manufacturer and product_name for better hardware spoofing","auto_fix":False})

    return issues


# ── Pre-flight gate ────────────────────────────────────────────────────────────

def _preflight_check(
    tool_name:  str,
    args:       Dict[str, Any],
    manager,                    # QemuManager — passed in to avoid circular import
    verbose:    bool = False,
) -> Dict[str, Any]:
    """
    Validate tool call args before execution.
    Returns {"action": "ok"|"auto_fix"|"ask_user"|"abort", ...}
    """
    ok = {"action": "ok"}

    if tool_name not in (
        "create_vm", "launch_vm", "delete_vm", "resize_disk",
        "clone_vm", "snapshot_restore", "snapshot_delete",
        "set_resource_limits", "send_monitor_cmd",
    ):
        return ok

    if tool_name == "create_vm":
        name     = str(args.get("name", "")).strip()
        iso_path = str(args.get("iso_path", "")).strip()
        os_type  = str(args.get("os_type", "")).lower()
        mt       = str(args.get("machine_type", "")).lower()

        if not name or name.lower() in PLACEHOLDER_VM_NAMES:
            return {"action":"ask_user","reason":f"VM name is missing or looks invented (got: '{name}')","question":"What would you like to name this VM?","fix_field":"name","options":["my-windows-vm","dev-box","test-ubuntu"]}

        vm_dir = os.path.join(os.path.expanduser("~"), ".qemu_vms", name)
        if os.path.exists(vm_dir):
            return {"action":"ask_user","reason":f"A VM named '{name}' already exists","question":f"A VM called '{name}' already exists. Overwrite it, or use a different name?","fix_field":"name","options":[f"{name}-2",f"{name}-new","overwrite"],"correction":"Use a different name or delete the existing VM first."}

        if mt and mt not in VALID_MACHINE_TYPES and not mt.startswith("pc-"):
            fixed = dict(args)
            if mt in get_all_profiles():
                fixed["profile"] = mt
            fixed.pop("machine_type", None)
            return {"action":"auto_fix","reason":f"machine_type='{mt}' is a profile name, not a machine type","correction":f"Set profile='{mt}' and removed invalid machine_type","fixed_args":fixed}

        if iso_path:
            bad_path = any([
                "/home/user/" in iso_path, "/path/to/" in iso_path,
                "scan_isos" in iso_path, "<" in iso_path,
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

        if iso_path and os.path.exists(iso_path):
            iso_lower = os.path.basename(iso_path).lower()
            if any(k in iso_lower for k in ("arm64","aarch64")) and str(args.get("machine_arch","x86_64")).lower() == "x86_64":
                return {"action":"ask_user","reason":f"ARM64 ISO '{os.path.basename(iso_path)}' with x86_64 VM — incompatible","question":"This is an ARM64 ISO. Do you want an ARM64 VM, or an x86_64 ISO instead?","fix_field":None,"options":["Use ARM64 VM","Get x86_64 ISO instead"],"correction":f"For x86_64: download Windows 11 x64 from microsoft.com"}

        is_win = "windows" in os_type or "win" in os_type
        if is_win and args.get("uefi") is False:
            fixed = dict(args); fixed["uefi"] = True; fixed["bios"] = "ovmf"
            return {"action":"auto_fix","reason":"Windows 11 requires UEFI but uefi=False was set","correction":"Forced uefi=True and bios=ovmf","fixed_args":fixed}

        disk_gb = int(args.get("disk_size_gb", 60))
        if is_win and disk_gb < 40:
            fixed = dict(args); fixed["disk_size_gb"] = 64
            return {"action":"auto_fix","reason":f"Windows 11 needs at least 64GB disk, got {disk_gb}GB","correction":f"Increased disk_size_gb from {disk_gb} to 64","fixed_args":fixed}

        internet_issues = _validate_with_internet(args, verbose=verbose)
        if internet_issues:
            blockers   = [i for i in internet_issues if i["severity"] == "error"]
            auto_fixes = [i for i in internet_issues if i.get("auto_fix") and i["severity"] != "error"]
            warnings   = [i for i in internet_issues if i["severity"] == "warning" and not i.get("auto_fix")]
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
                blockers   = [i for i in profile_issues if i["severity"] == "error"]
                warnings   = [i for i in profile_issues if i["severity"] == "warning"]
                auto_fixes = [i for i in profile_issues if i.get("auto_fix")]
                if blockers:
                    return {"action":"ask_user","reason":f"Profile '{profile_name}' has compatibility issues: {' | '.join(i['message'] for i in blockers)}","question":f"Profile '{profile_name}' may not work on this system. Proceed anyway or cancel?","fix_field":None,"options":["Proceed anyway","Cancel","Use minimal profile instead"],"correction":" | ".join(i["fix"] for i in blockers if i.get("fix")),"issues":profile_issues}
                if auto_fixes:
                    fixed = dict(args); fix_notes = []
                    for issue in auto_fixes:
                        if issue.get("fix_field") and issue.get("fix_value") is not None:
                            fixed[issue["fix_field"]] = issue["fix_value"]
                            fix_notes.append(f"{issue['fix_field']}={issue['fix_value']}")
                    return {"action":"auto_fix","reason":f"Profile '{profile_name}': auto-fixed "+", ".join(fix_notes),"correction":" | ".join(i["message"] for i in auto_fixes),"fixed_args":fixed,"warnings":[i["message"] for i in warnings]}

    elif tool_name == "launch_vm":
        name   = str(args.get("name", "")).strip()
        vm_dir = os.path.join(os.path.expanduser("~"), ".qemu_vms", name)
        if name and not os.path.exists(vm_dir):
            candidates = [v["name"] for v in manager.list_vms() if name.lower() in v["name"].lower()]
            if candidates:
                return {"action":"abort","reason":f"VM '{name}' not found. Did you mean: {candidates}?","correction":f"Use one of these names: {candidates}"}
            return {"action":"abort","reason":f"VM '{name}' doesn't exist. Create it first.","correction":"Call create_vm before launch_vm."}
        try:
            from qemu_config import MachineConfig
            cfg = MachineConfig.load(name)
            if cfg.iso_path and not os.path.exists(cfg.iso_path):
                return {"action":"ask_user","reason":f"ISO file missing: {cfg.iso_path}","question":f"The ISO '{os.path.basename(cfg.iso_path)}' is missing. Launch without ISO, or fix the path?","fix_field":None,"options":["Launch anyway (no ISO)","Cancel"]}
        except Exception:
            pass

    elif tool_name == "delete_vm":
        name = str(args.get("name", "")).strip()
        if name:
            return {"action":"ask_user","reason":f"Destructive operation: delete VM '{name}'","question":f"Are you sure you want to delete '{name}'?","fix_field":None,"options":["Yes, delete it","No, keep it"],"correction":"Deletion cannot be undone without recreating the VM."}

    elif tool_name == "resize_disk":
        name     = str(args.get("name", "")).strip()
        new_size = int(args.get("new_size_gb", 0))
        if name and new_size:
            vm_dir = os.path.join(os.path.expanduser("~"), ".qemu_vms", name)
            if not os.path.exists(vm_dir):
                return {"action":"abort","reason":f"VM '{name}' does not exist — cannot resize disk","correction":"Create the VM first with create_vm, then resize."}
            try:
                from qemu_config import MachineConfig
                cfg = MachineConfig.load(name)
                if cfg.disks:
                    current = cfg.disks[0].size_gb
                    if new_size < current:
                        return {"action":"abort","reason":f"Cannot shrink disk from {current}GB to {new_size}GB — QEMU doesn't support shrinking","correction":f"new_size_gb must be >= current size ({current}GB)"}
            except FileNotFoundError:
                return {"action":"abort","reason":f"VM '{name}' config not found","correction":"Check the VM name with list_vms."}
            except Exception:
                pass

    elif tool_name == "send_monitor_cmd":
        cmd = str(args.get("cmd", "")).strip().lower()
        if any(d in cmd for d in ["quit","system_reset","powerdown","eject","device_del"]):
            return {"action":"ask_user","reason":f"Potentially destructive monitor command: '{cmd}'","question":f"Run QEMU monitor command '{cmd}'? This may affect the running VM.","fix_field":None,"options":["Yes, run it","No, cancel"]}

    elif tool_name in ("snapshot_restore", "snapshot_delete"):
        name      = str(args.get("name", "")).strip()
        snap_name = str(args.get("snap_name", "")).strip()
        verb      = "restore" if tool_name == "snapshot_restore" else "delete"
        return {"action":"ask_user","reason":f"Snapshot {verb}: '{snap_name}' on VM '{name}'","question":f"Confirm {verb} snapshot '{snap_name}' on '{name}'?","fix_field":None,"options":[f"Yes, {verb} it","No, cancel"],"correction":"Snapshot restore replaces current VM state. Snapshot delete is permanent."}

    return ok


def _show_preflight_warning(preflight: Dict, console):
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

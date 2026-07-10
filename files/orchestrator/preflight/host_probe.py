"""
host_probe.py — Host-capability & internet/product probing for pre-flight.

The "reach out to the real world" half of the validation layer: local QEMU
capability queries (machine types / CPU models), CPU-architecture checks,
DuckDuckGo product lookup, host/profile compatibility, stealth product
inference, and the custom-mode flag that disables product verification.

validator.py imports the four entry points its pre-flight gate calls
(set_custom_mode, _validate_profile_for_host, _validate_with_internet,
_stealth_infer_from_product) and re-exports set_custom_mode. This module never
imports from validator — the edge is one-directional (validator -> host_probe).
"""

import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from orchestrator.executor_client import (
    get_ovmf as _get_ovmf,
    get_capabilities as check_system_capabilities,
    get_all_profiles,
)

_CFG = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_THRESHOLDS = _CFG["thresholds"]

# Network / product-lookup state
_NET_CACHE:  Dict[str, Any] = {}
_NET_TIMEOUT = _CFG["net_timeout"]
_NET_ENABLED = True
_CUSTOM_MODE = False   # set True via set_custom_mode() to skip product verification

# QEMU capability caches (expensive — run once per session)
_QEMU_MACHINES_CACHE: Optional[set] = None
_QEMU_CPUS_CACHE:     Optional[set] = None

# CPU architecture classification / product hints
_ARM_CPU_PREFIXES      = tuple(_CFG["arm_cpu_prefixes"])
_X86_CPU_NAMES         = set(_CFG["x86_cpu_names"])
_STEALTH_PRODUCT_HINTS = [tuple(h) for h in _CFG["stealth_product_hints"]]
_MS_WINDOWS_ISO_PAGE   = _CFG["ms_windows_iso_page"]


# Toggles the global flag that disables product verification via DuckDuckGo.
# In: bool → Out: nothing
def set_custom_mode(enabled: bool) -> None:
    global _CUSTOM_MODE
    _CUSTOM_MODE = enabled


# Fetches JSON from a URL with an MD5 session cache and timeout; returns None on failure.
# In: str url, dict? headers → Out: dict | None
def _net_get(url: str, headers: Dict = None) -> Optional[Dict]:
    """Fetch JSON from a URL with session caching and timeout.

    Args:
        url:     URL to fetch; must return JSON.
        headers: Optional extra headers (User-Agent added automatically).

    Returns:
        Parsed JSON dict, or ``None`` if the request failed or networking
        is disabled.

    Example::

        _net_get("https://api.example.com/data")
        # → {"key": "value"} on success, None on failure
    """
    if not _NET_ENABLED:
        return None
    cache_key = hashlib.md5(url.encode()).hexdigest()
    if cache_key in _NET_CACHE:
        return _NET_CACHE[cache_key]
    try:
        req = urllib.request.Request(url, headers=headers or {"User-Agent": _CFG["user_agent"]})
        with urllib.request.urlopen(req, timeout=_NET_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            _NET_CACHE[cache_key] = data
            return data
    except Exception:
        _NET_CACHE[cache_key] = None
        return None


# Checks if a URL exists via HEAD request; returns False on failure.
# In: str url → Out: bool
def _net_head(url: str) -> bool:
    """Check if a URL exists via HEAD request.

    Args:
        url: URL to probe with HTTP HEAD.

    Returns:
        ``True`` if the server returned a 2xx response; ``False`` on any
        error or when networking is disabled.

    Example::

        _net_head("https://example.com/file.iso")
        # → True if the file exists, False if 404 or unreachable
    """
    if not _NET_ENABLED:
        return False
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _CFG["user_agent"]})
        with urllib.request.urlopen(req, timeout=_NET_TIMEOUT):
            return True
    except Exception:
        return False


# Queries the local QEMU binary for all supported machine types (result is cached).
# In: str binary → Out: set
def _get_qemu_machine_types(binary: str = "qemu-system-x86_64") -> set:
    """Ask the local QEMU binary what machine types it supports."""
    global _QEMU_MACHINES_CACHE
    if _QEMU_MACHINES_CACHE is not None:
        return _QEMU_MACHINES_CACHE
    try:
        result = subprocess.run([binary, "-machine", "help"], capture_output=True, text=True, timeout=_CFG["qemu_timeout"])
        machines = set()
        for line in result.stdout.splitlines():
            parts = line.split()
            if parts:
                machines.add(parts[0].lower().rstrip(","))
        _QEMU_MACHINES_CACHE = machines
        return machines
    except Exception:
        return set()


# Queries the local QEMU binary for all supported CPU models (result is cached).
# In: str binary → Out: set
def _get_qemu_cpu_models(binary: str = "qemu-system-x86_64") -> set:
    """Ask the local QEMU binary what CPU models it supports."""
    global _QEMU_CPUS_CACHE
    if _QEMU_CPUS_CACHE is not None:
        return _QEMU_CPUS_CACHE
    try:
        result = subprocess.run([binary, "-cpu", "help"], capture_output=True, text=True, timeout=_CFG["qemu_timeout"])
        cpus = set()
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if parts and not parts[0].startswith("-"):
                cpus.add(parts[0].lower())
        _QEMU_CPUS_CACHE = cpus
        return cpus
    except Exception:
        return set()


# Returns True if the CPU name matches known ARM prefixes.
# In: str cpu_model → Out: bool
def _is_arm_cpu(cpu_model: str) -> bool:
    lower = cpu_model.lower()
    return any(lower.startswith(p) for p in _ARM_CPU_PREFIXES)


# Returns True if the CPU name matches known x86 names.
# In: str cpu_model → Out: bool
def _is_x86_cpu(cpu_model: str) -> bool:
    lower = cpu_model.lower().replace("-", "").replace("_", "")
    return any(x86 in lower for x86 in _X86_CPU_NAMES)


# Queries DuckDuckGo to verify a hardware product is real; returns found flag and summary snippet.
# In: str manufacturer, str product → Out: dict
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


# Cross-checks machine type, CPU, arch, product existence, memory, and ISO arch against local QEMU and DuckDuckGo.
# In: dict args, bool verbose → Out: List[dict] issues
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
        if any(k in prod_lower for k in _CFG["laptop_product_keywords"]) and memory_mb > _THRESHOLDS["max_laptop_memory_mb"]:
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
        if any(k in iso_lower for k in _CFG["arm_iso_keywords"]) and machine_arch == "x86_64":
            issues.append({
                "severity": "error",
                "message":  "ARM64 Windows ISO with x86_64 VM — will not boot",
                "fix":      f"Get x86_64 ISO from: {_MS_WINDOWS_ISO_PAGE}",
                "auto_fix": False, "source": "iso_filename",
            })

    return issues


# Checks a profile against the host for ARM binary, KVM/OVMF/hugepages/RAM/core constraints, and raspi binary mismatch.
# In: str profile_name, dict? profile_data → Out: List[dict] issues
def _validate_profile_for_host(profile_name: str, profile_data: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Validate any profile (built-in or custom) against the current host.
    Returns a list of issue dicts.
    """
    import shutil as _shutil

    issues       = []
    all_profiles = get_all_profiles()
    profile      = profile_data or all_profiles.get(profile_name)
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

    if profile.get("uefi") and not _get_ovmf().get("available") and profile.get("bios","ovmf") in ("ovmf","ovmf_ms"):
        issues.append({"severity":"warning","message":f"Profile '{profile_name}' requires UEFI but OVMF not found","fix":"sudo apt install ovmf","auto_fix":True,"fix_field":"bios","fix_value":"seabios"})

    if profile.get("hugepages"):
        try:
            with open("/proc/sys/vm/nr_hugepages") as f:
                if int(f.read().strip()) == 0:
                    issues.append({"severity":"error","message":f"Profile '{profile_name}' uses hugepages but none are allocated","fix":"sudo sysctl vm.nr_hugepages=2048","auto_fix":True,"fix_field":"hugepages","fix_value":False})
        except Exception:
            pass  # hugepages probe is advisory — skip the warning if /proc can't be read

    profile_mem = int(profile.get("memory_mb", 2048))
    host_mem    = caps.get("host_memory_mb", 0)
    if host_mem > 0 and profile_mem > host_mem:
        issues.append({"severity":"warning","message":f"Profile requests {profile_mem}MB RAM but host only has {host_mem}MB","fix":f"Reduce memory_mb to {host_mem//2} or less","auto_fix":True,"fix_field":"memory_mb","fix_value":min(profile_mem, int(host_mem*_THRESHOLDS["profile_memory_ratio"]))})

    profile_cores = int(profile.get("cpu_cores", 2))
    host_cores    = caps.get("host_cpu_cores", 1)
    if profile_cores > host_cores * _THRESHOLDS["profile_cores_overcommit"]:
        issues.append({"severity":"warning","message":f"Profile requests {profile_cores} cores but host only has {host_cores} — heavy over-commit","fix":f"Reduce cpu_cores to {host_cores} or less","auto_fix":True,"fix_field":"cpu_cores","fix_value":host_cores})

    free_gb = caps.get("home_free_gb", 999)
    if free_gb < _THRESHOLDS["min_disk_free_gb_error"]:
        issues.append({"severity":"error","message":f"Only {free_gb}GB free in home directory","fix":"Free up disk space before creating the VM","auto_fix":False})
    elif free_gb < _THRESHOLDS["min_disk_free_gb_warn"]:
        issues.append({"severity":"warning","message":f"Only {free_gb}GB free — VM disk image may exceed available space","fix":"Use a smaller disk_size_gb or free up space","auto_fix":False})

    mt = profile.get("machine_type", "q35")
    if "raspi" in mt and "aarch64" not in binary:
        issues.append({"severity":"error","message":f"Profile '{profile_name}' uses raspi machine type but qemu_binary is not aarch64","fix":"Set qemu_binary=qemu-system-aarch64 in the profile","auto_fix":True,"fix_field":"qemu_binary","fix_value":"qemu-system-aarch64"})

    notes = profile.get("_notes", "")
    if notes and "slow" in notes.lower():
        issues.append({"severity":"warning","message":f"Profile note: {notes}","fix":"","auto_fix":False})

    if profile.get("_custom"):
        cpu_model = profile.get("cpu_model", "host")
        if any(cpu_model.lower().startswith(p) for p in _ARM_CPU_PREFIXES) and arch == "x86_64":
            issues.append({"severity":"error","message":f"Custom profile '{profile_name}' has ARM cpu_model='{cpu_model}' but machine_arch=x86_64","fix":"Change cpu_model to 'host' or set machine_arch=aarch64","auto_fix":True,"fix_field":"cpu_model","fix_value":"host"})
        missing = [f for f in ("manufacturer","product_name") if not profile.get(f)]
        if missing:
            issues.append({"severity":"warning","message":f"Custom profile '{profile_name}' is missing SMBIOS fields: {missing}","fix":"Add manufacturer and product_name for better hardware spoofing","auto_fix":False})

    return issues


def _stealth_infer_from_product(product_name: str) -> Dict[str, str]:
    """Infer ``{manufacturer, bios_vendor, smbios_type}`` from a product name.

    Args:
        product_name: Product string (e.g. ``"ThinkPad X1"``).

    Returns:
        Dict with inferred SMBIOS fields, or empty dict if no hint matched.

    Example::

        _stealth_infer_from_product("ThinkPad X1 Carbon")
        # → {"manufacturer": "Lenovo", "bios_vendor": "Lenovo",
        #    "smbios_type": "Notebook"}
        _stealth_infer_from_product("unknown box")
        # → {}
    """
    pn = product_name.lower()
    for keyword, mfr, bios_vendor, smbios_type in _STEALTH_PRODUCT_HINTS:
        if keyword in pn:
            result: Dict[str, str] = {"manufacturer": mfr, "bios_vendor": bios_vendor}
            if smbios_type:
                result["smbios_type"] = smbios_type
            return result
    return {}


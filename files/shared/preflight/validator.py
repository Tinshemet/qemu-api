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

try:
    from shared.api.qemu_config import OVMF, check_system_capabilities, get_all_profiles
except ImportError:
    OVMF = {"available": False, "code": "", "vars": ""}
    def check_system_capabilities(): return {}                                # type: ignore[misc]
    def get_all_profiles(): return {}                                         # type: ignore[misc]
from shared.sanitizer.sanitizer import PLACEHOLDER_VM_NAMES, REAL_HOME, VALID_MACHINE_TYPES, _resolve_iso

_CFG = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_THRESHOLDS = _CFG["thresholds"]

# ── Global flags ───────────────────────────────────────────────────────────────

_NET_CACHE:   Dict[str, Any] = {}
_NET_TIMEOUT  = _CFG["net_timeout"]
_NET_ENABLED  = True
_CUSTOM_MODE  = False   # set True via set_custom_mode() to skip product verification

# Cache for QEMU capability queries (expensive — run once per session)
_QEMU_MACHINES_CACHE: Optional[set] = None
_QEMU_CPUS_CACHE:     Optional[set] = None


# Toggles the global flag that disables product verification via DuckDuckGo.
# In: bool → Out: nothing
def set_custom_mode(enabled: bool):
    global _CUSTOM_MODE
    _CUSTOM_MODE = enabled


# ── Network utilities ──────────────────────────────────────────────────────────

# Fetches JSON from a URL with an MD5 session cache and timeout; returns None on failure.
# In: str url, dict? headers → Out: dict | None
def _net_get(url: str, headers: Dict = None) -> Optional[Dict]:
    """Fetch JSON from a URL with session caching and timeout. Returns None on failure."""
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
    """Check if a URL exists via HEAD request. Returns False on failure."""
    if not _NET_ENABLED:
        return False
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": _CFG["user_agent"]})
        with urllib.request.urlopen(req, timeout=_NET_TIMEOUT):
            return True
    except Exception:
        return False


# ── Local QEMU capability queries ──────────────────────────────────────────────

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


# ── CPU architecture classification ───────────────────────────────────────────

_ARM_CPU_PREFIXES = tuple(_CFG["arm_cpu_prefixes"])
_X86_CPU_NAMES    = set(_CFG["x86_cpu_names"])


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


# ── DuckDuckGo product lookup ──────────────────────────────────────────────────

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


_MS_WINDOWS_ISO_PAGE = _CFG["ms_windows_iso_page"]


# ── Internet / local QEMU validation ──────────────────────────────────────────

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


# ── Pre-flight gate ────────────────────────────────────────────────────────────

# Gate run before destructive tools: validates name, ISO, machine type, Windows requirements, disk size, and profile compatibility.
# In: str tool_name, dict args, QemuManager, bool verbose → Out: dict with action (ok/auto_fix/ask_user/abort)
_QEMU_CPU_MODELS = {"qemu64", "qemu32", "kvm64", "kvm32", "max", "base", "host-phys-bits-limit"}

# keyword (lowercase, checked via 'in') → (manufacturer, bios_vendor, smbios_type or None)
# smbios_type=None means we can't infer chassis from the product name alone.
_STEALTH_PRODUCT_HINTS: List[tuple] = [
    # Dell
    ("latitude",    "Dell Inc.",                   "Dell Inc.",                   "Notebook"),
    ("inspiron",    "Dell Inc.",                   "Dell Inc.",                   "Notebook"),
    ("xps",         "Dell Inc.",                   "Dell Inc.",                   "Notebook"),
    ("precision",   "Dell Inc.",                   "Dell Inc.",                   "Notebook"),
    ("vostro",      "Dell Inc.",                   "Dell Inc.",                   "Notebook"),
    ("alienware",   "Dell Inc.",                   "Dell Inc.",                   "Notebook"),
    ("optiplex",    "Dell Inc.",                   "Dell Inc.",                   "Desktop"),
    ("poweredge",   "Dell Inc.",                   "Dell Inc.",                   "Server"),
    # Lenovo
    ("thinkpad",    "Lenovo",                      "Lenovo",                      "Notebook"),
    ("ideapad",     "Lenovo",                      "Lenovo",                      "Notebook"),
    ("yoga",        "Lenovo",                      "Lenovo",                      "Notebook"),
    ("legion",      "Lenovo",                      "Lenovo",                      "Notebook"),
    ("thinkcentre", "Lenovo",                      "Lenovo",                      "Desktop"),
    ("thinkstation","Lenovo",                      "Lenovo",                      "Desktop"),
    # HP
    ("elitebook",   "HP",                          "HP",                          "Notebook"),
    ("probook",     "HP",                          "HP",                          "Notebook"),
    ("pavilion",    "HP",                          "HP",                          "Notebook"),
    ("spectre",     "HP",                          "HP",                          "Notebook"),
    ("envy",        "HP",                          "HP",                          "Notebook"),
    ("zbook",       "HP",                          "HP",                          "Notebook"),
    ("omen",        "HP",                          "HP",                          "Notebook"),
    ("elitedesk",   "HP",                          "HP",                          "Desktop"),
    ("prodesk",     "HP",                          "HP",                          "Desktop"),
    # Apple
    ("macbook",     "Apple Inc.",                  "Apple Inc.",                  "Notebook"),
    ("imac",        "Apple Inc.",                  "Apple Inc.",                  "Desktop"),
    ("mac mini",    "Apple Inc.",                  "Apple Inc.",                  "Desktop"),
    ("mac pro",     "Apple Inc.",                  "Apple Inc.",                  "Tower"),
    # Microsoft
    ("surface",     "Microsoft Corporation",       "Microsoft Corporation",       "Notebook"),
    # ASUS
    ("zephyrus",    "ASUSTeK Computer Inc.",       "American Megatrends Inc.",    "Notebook"),
    ("vivobook",    "ASUSTeK Computer Inc.",       "American Megatrends Inc.",    "Notebook"),
    ("zenbook",     "ASUSTeK Computer Inc.",       "American Megatrends Inc.",    "Notebook"),
    ("rog ",        "ASUSTeK Computer Inc.",       "American Megatrends Inc.",    "Notebook"),
    # Acer
    ("aspire",      "Acer",                        "Acer",                        "Notebook"),
    ("swift",       "Acer",                        "Acer",                        "Notebook"),
    ("nitro",       "Acer",                        "Acer",                        "Notebook"),
    ("predator",    "Acer",                        "Acer",                        "Notebook"),
    # Samsung
    ("galaxy book", "Samsung Electronics Co., Ltd.", "Samsung Electronics Co., Ltd.", "Notebook"),
    # Huawei
    ("matebook",    "HUAWEI",                      "HUAWEI",                      "Notebook"),
    # LG
    ("gram",        "LG Electronics",              "LG Electronics",              "Notebook"),
    # Toshiba
    ("portege",     "TOSHIBA",                     "TOSHIBA",                     "Notebook"),
    ("satellite",   "TOSHIBA",                     "TOSHIBA",                     "Notebook"),
    ("tecra",       "TOSHIBA",                     "TOSHIBA",                     "Notebook"),
    # Fujitsu
    ("lifebook",    "Fujitsu",                     "Fujitsu",                     "Notebook"),
    ("celsius",     "Fujitsu",                     "Fujitsu",                     "Desktop"),
    # Sony
    ("vaio",        "Sony Corporation",            "Sony Corporation",            "Notebook"),
    # Razer
    ("razer blade", "Razer",                       "Razer",                       "Notebook"),
    # MSI
    ("msi ",        "Micro-Star International Co., Ltd.", "American Megatrends Inc.", "Notebook"),
    # Gigabyte
    ("aorus",       "GIGABYTE",                    "American Megatrends Inc.",    "Notebook"),
    # Panasonic
    ("toughbook",   "Panasonic",                   "Panasonic",                   "Notebook"),
]


def _stealth_infer_from_product(product_name: str) -> Dict[str, str]:
    """Return inferred {manufacturer, bios_vendor, smbios_type} from product_name keywords."""
    pn = product_name.lower()
    for keyword, mfr, bios_vendor, smbios_type in _STEALTH_PRODUCT_HINTS:
        if keyword in pn:
            result: Dict[str, str] = {"manufacturer": mfr, "bios_vendor": bios_vendor}
            if smbios_type:
                result["smbios_type"] = smbios_type
            return result
    return {}


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
    _LAPTOP_KW    = ("notebook", "laptop", "portable")
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
        elif any(k in machine_class for k in _LAPTOP_KW):
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
    # vectors. 'gpu' defaults to "virtio" if not set — both cases are errors.
    gpu = str(args.get("gpu", "virtio")).lower()
    if gpu == "virtio":
        msg = (
            "stealth VM GPU is 'virtio' (virtio-vga) — PCI vendor 0x1af4 (Red Hat/QEMU) "
            "is trivially detectable via lspci"
        ) if "gpu" in args else (
            "stealth VM GPU not set — default is 'virtio' (virtio-vga) with detectable "
            "Red Hat PCI vendor 0x1af4"
        )
        issues.append({
            "severity":  "error",
            "message":   msg,
            "fix":       "Set gpu='none' (uses cirrus-vga, PCI vendor 0x1013 Cirrus Logic) or 'qxl' for better 2D",
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


def _preflight_check(
    tool_name:     str,
    args:          Dict[str, Any],
    manager,                       # QemuManager — passed in to avoid circular import
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

    if tool_name not in (
        "create_vm", "create_profile", "launch_vm", "delete_vm", "resize_disk",
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

        if not stateless_only:
            vm_dir = os.path.join(os.path.expanduser("~"), ".qemu_vms", name)
            if os.path.exists(vm_dir):
                return {"action":"ask_user","reason":f"A VM named '{name}' already exists","question":f"A VM called '{name}' already exists. Overwrite it, or use a different name?","fix_field":"name","original_name":name,"options":[f"{name}-2",f"{name}-new","overwrite"],"correction":"Use a different name or delete the existing VM first."}

        if mt and mt not in VALID_MACHINE_TYPES and not mt.startswith("pc-"):
            fixed = dict(args)
            if mt in get_all_profiles():
                fixed["profile"] = mt
            fixed.pop("machine_type", None)
            return {"action":"auto_fix","reason":f"machine_type='{mt}' is a profile name, not a machine type","correction":f"Set profile='{mt}' and removed invalid machine_type","fixed_args":fixed}

        if iso_path and not stateless_only:
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
            stealth_issues = _validate_stealth_args(args)
            auto_fixes = [i for i in stealth_issues if i.get("auto_fix")]
            blockers   = [i for i in stealth_issues if i["severity"] == "error"]
            warnings   = [i for i in stealth_issues if i["severity"] == "warning"]

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

    elif tool_name == "create_profile":
        profile_name  = str(args.get("profile_name", "")).strip()
        profile_data  = {k: v for k, v in args.items() if k not in ("profile_name", "force")}

        _HW_FIELDS = {"cpu_model", "machine_type", "memory_mb", "cpu_cores", "cpu_threads",
                      "machine_arch", "machine_class", "disk_size_gb", "display", "gpu",
                      "audio", "kvm", "uefi", "hugepages", "manufacturer", "product_name"}
        if not any(f in profile_data for f in _HW_FIELDS):
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
            blockers   = [i for i in profile_issues if i["severity"] == "error"]
            auto_fixes = [i for i in profile_issues if i.get("auto_fix") and i["severity"] != "error"]
            warnings   = [i for i in profile_issues if i["severity"] == "warning" and not i.get("auto_fix")]
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
        name   = str(args.get("name", "")).strip()
        vm_dir = os.path.join(os.path.expanduser("~"), ".qemu_vms", name)
        if name and not os.path.exists(vm_dir):
            candidates = []
            if hasattr(manager, "list_vms"):
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

    elif tool_name == "resize_disk" and not stateless_only:
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
        if any(d in cmd for d in ["quit","system_reset","powerdown","eject","device_del","drive_del"]):
            return {"action":"ask_user","reason":f"Potentially destructive monitor command: '{cmd}'","question":f"Run QEMU monitor command '{cmd}'? This may affect the running VM.","fix_field":None,"options":["Yes, run it","No, cancel"]}

    elif tool_name in ("snapshot_restore", "snapshot_delete"):
        name      = str(args.get("name", "")).strip()
        snap_name = str(args.get("snap_name", "")).strip()
        verb      = "restore" if tool_name == "snapshot_restore" else "delete"
        return {"action":"ask_user","reason":f"Snapshot {verb}: '{snap_name}' on VM '{name}'","question":f"Confirm {verb} snapshot '{snap_name}' on '{name}'?","fix_field":None,"options":[f"Yes, {verb} it","No, cancel"],"correction":"Snapshot restore replaces current VM state. Snapshot delete is permanent."}

    return ok


# Renders a yellow warning panel and presents the pre-flight options to the user.
# In: dict preflight, Console → Out: nothing (console output)
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

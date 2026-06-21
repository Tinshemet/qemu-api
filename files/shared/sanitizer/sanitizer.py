"""
sanitizer.py — Input Sanitization and Resolution Layer

Cleans AI tool call arguments before dispatch: fixes hallucinated
paths, coerces types, rejects invalid enum values, and resolves
vague VM name / ISO path references to real values.
"""

import json
import os
import re
import tempfile
from typing import Any, Dict, List, Optional

try:
    from client.api.qemu_config import get_all_profiles
except ImportError:
    def get_all_profiles(): return {}                                         # type: ignore[misc]

_CFG                = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_BOUNDS             = _CFG["bounds"]
_OPTIONAL_REMOVABLE = set(_CFG["optional_removable"])
_ISO_OS_KEYWORDS    = _CFG["iso_os_keywords"]
_ENUM_DEFAULTS      = _CFG["enum_field_defaults"]

REAL_HOME = os.path.expanduser("~")

# ── Validation constants ───────────────────────────────────────────────────────

PLACEHOLDER_VM_NAMES:    set = set(_CFG["placeholder_vm_names"])
PLACEHOLDER_ISO_PATTERNS:set = set(_CFG["placeholder_iso_patterns"])

VALID_MACHINE_TYPES:  set = set(_CFG["valid_machine_types"])
VALID_DISPLAY_MODES:  set = set(_CFG["valid_display_modes"])
VALID_GPU_TYPES:      set = set(_CFG["valid_gpu_types"])
VALID_AUDIO_TYPES:    set = set(_CFG["valid_audio_types"])
VALID_NETWORK_MODES:  set = set(_CFG["valid_network_modes"])
VALID_DISK_FORMATS:   set = set(_CFG["valid_disk_formats"])
VALID_BIOS:           set = set(_CFG["valid_bios"])
VALID_MACHINE_ARCH:   set = set(_CFG["valid_machine_arch"])
VALID_MACHINE_CLASS:  set = set(_CFG["valid_machine_class"])
VALID_OS_TYPES:       set = set(_CFG["valid_os_types"])

OS_TYPE_ALIASES: dict = _CFG["os_type_aliases"]

_QEMU_OUI_PREFIXES:   set   = set(_CFG["qemu_oui_prefixes"])
_BAD_BRIDGE_IFACES:   set   = set(_CFG["bad_bridge_ifaces"])
_BAD_BRIDGE_PREFIXES: tuple = tuple(_CFG["bad_bridge_prefixes"])
_DEFAULT_BRIDGE:      str   = _CFG["default_bridge"]
_ARM_CPU_MODELS:      set   = set(_CFG["arm_cpu_models"])
_ARM_MACHINE_TYPES:   tuple = tuple(_CFG["arm_machine_types"])
_ARM_MACHINE_ARCHS:   tuple = tuple(_CFG["arm_machine_archs"])


# ── Path fixer ─────────────────────────────────────────────────────────────────

# Rejects placeholder text, corrects wrong Linux usernames in paths, converts Windows paths to Linux.
# In: str → Out: str
def _fix_path(p: str) -> str:
    """Fix hallucinated paths — wrong username, relative paths, placeholder text."""
    if not p or not isinstance(p, str):
        return p
    # Reject literal placeholder patterns before any path manipulation
    p_lower = p.lower()
    for pat in PLACEHOLDER_ISO_PATTERNS:
        if pat.lower() in p_lower:
            return ""
    # Fix wrong username: /home/anyname/ or /Users/anyname/ → real home
    p = re.sub(r"^(?:/home/|/Users/)[^/]+/", REAL_HOME + "/", p)
    p = p.replace("~/", REAL_HOME + "/")
    # Fix Windows-style paths
    p = p.replace("\\", "/")
    p = p.replace("C:/", REAL_HOME + "/")
    return p


# ── VM name resolver ───────────────────────────────────────────────────────────

# Resolves a vague VM reference (number, partial name, OS name) to a real VM name.
# In: List[dict] vms, str ref → Out: str | None
def _resolve_vm_name(vms: List[Dict], ref: str) -> Optional[str]:
    if not ref or not ref.strip():
        return None
    if ref == "all":
        return "all"
    for vm in vms:
        if vm["name"] == ref:
            return vm["name"]
    if re.match(r"^\d+$", ref.strip()):
        idx = int(ref.strip()) - 1
        if 0 <= idx < len(vms):
            return vms[idx]["name"]
    lower = ref.lower()
    for vm in vms:
        if lower in vm["name"].lower() or lower in vm.get("os", "").lower():
            return vm["name"]
    return None


# ── ISO resolver ───────────────────────────────────────────────────────────────

# Resolves a hallucinated or vague ISO path to a real file via exact match → username fix → keyword scoring → first ISO fallback.
# In: str iso_hint → Out: str | None
def _resolve_iso(iso_hint: str) -> Optional[str]:
    """
    Resolve an ISO path from a vague hint or hallucinated path.
    Strategy:
      1. Exact path — use directly
      2. Fix wrong username in path
      3. Fuzzy keyword scoring across all search dirs
      4. OS-keyword scan
      5. Last resort — first ISO found in Desktop/Images
    """
    if not iso_hint:
        return None

    # Step 1: exact path
    for candidate in [iso_hint, os.path.expanduser(iso_hint)]:
        if os.path.exists(candidate):
            return candidate

    # Step 2: fix wrong username
    fixed = re.sub(r"^/home/[^/]+/", REAL_HOME + "/", iso_hint)
    if os.path.exists(fixed):
        return fixed

    # Build search dirs
    desktop = os.path.join(REAL_HOME, "Desktop")
    search_dirs: List[str] = []
    _iso_dirs = set(_CFG["iso_search_dirs"])
    if os.path.isdir(desktop):
        for entry in os.listdir(desktop):
            full = os.path.join(desktop, entry)
            if os.path.isdir(full) and entry.lower() in _iso_dirs:
                search_dirs.append(full)
        search_dirs.append(desktop)
    for d in ["Downloads", "iso", "ISOs", "images", "Images"]:
        p = os.path.join(REAL_HOME, d)
        if os.path.isdir(p):
            search_dirs.append(p)
    search_dirs.append(tempfile.gettempdir())

    all_isos: List[str] = []
    for d in search_dirs:
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(".iso"):
                all_isos.append(os.path.join(d, f))

    if not all_isos:
        return iso_hint

    # Step 3: keyword scoring
    stopwords = {
        "iso", "the", "from", "file", "image", "images", "img",
        "folder", "desktop", "home", "user", "and", "my", "v2",
        "v1", "x64", "x86", "arm", "arm64", "amd64", "bit",
    }
    raw_words = re.split(r"[\s/\\-_.]+", iso_hint.lower())
    keywords  = [w for w in raw_words if len(w) > 2 and w not in stopwords]

    hint_lower = iso_hint.lower()
    for key, variants in _ISO_OS_KEYWORDS.items():
        if any(v in hint_lower for v in variants):
            keywords += variants
    keywords = list(dict.fromkeys(keywords))

    best_match, best_score = None, -1
    for full_path in all_isos:
        f_lower = os.path.basename(full_path).lower()
        score   = sum(1 for kw in keywords if kw in f_lower)
        ai_base = os.path.basename(iso_hint).lower().replace(".iso", "").strip()
        if ai_base and len(ai_base) > 3 and ai_base in f_lower:
            score += 10
        if score > best_score:
            best_score, best_match = score, full_path

    if best_match and best_score > 0:
        return best_match

    # Step 4: return first ISO found
    if all_isos:
        return all_isos[0]

    return iso_hint


# ── Main sanitiser ─────────────────────────────────────────────────────────────

# Coerces types, validates enums, caps resource values, cleans paths, rejects bad MACs/bridges, and removes empty optional fields.
# In: str tool_name, dict args → Out: dict
def _sanitise_args(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sanitise all args before dispatch.
    Silently corrects what it can, removes what it can't fix.
    """
    # Type coercion
    int_fields  = {"cpu_cores", "cpu_threads", "memory_mb", "disk_size_gb",
                   "new_size_gb", "disk_index", "cpu_percent", "lines", "vnc_port", "spice_port"}
    bool_fields = {"kvm", "uefi", "battery", "hugepages", "force", "delete_disks", "dry_run", "balloon"}

    for f in int_fields:
        if f in args and args[f] is not None:
            try:
                args[f] = int(str(args[f]).replace("GB","").replace("gb","")
                                          .replace("mb","").replace("MB","").strip())
            except (ValueError, TypeError):
                args.pop(f, None)

    for f in bool_fields:
        if f in args and isinstance(args[f], str):
            args[f] = args[f].lower() in ("true", "yes", "1", "on")

    # VM name: strip special chars, reject placeholders
    if "name" in args and args["name"]:
        args["name"] = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(args["name"]).strip())
        if args["name"].lower() in PLACEHOLDER_VM_NAMES:
            args["name"] = ""

    # machine_type: reject profile names used as machine type
    if "machine_type" in args and args["machine_type"]:
        mt = str(args["machine_type"]).lower().split(",")[0].strip()
        if mt not in VALID_MACHINE_TYPES and not mt.startswith("pc-"):
            if not args.get("profile"):
                all_p = get_all_profiles()
                if mt in all_p:
                    args["profile"] = mt
                else:
                    for pname in all_p:
                        if mt in pname or pname in mt:
                            args["profile"] = pname
                            break
            args.pop("machine_type", None)

    # OS type aliases
    if "os_type" in args and args["os_type"]:
        alias = OS_TYPE_ALIASES.get(str(args["os_type"]).lower().strip())
        if alias:
            args["os_type"] = alias

    # Enum field validation
    # Rescue bus names mis-sent as disk_format before enum validation strips them.
    _DISK_BUS_VALUES = {"sata", "nvme", "scsi", "ide", "virtio"}
    if args.get("disk_format", "").lower() in _DISK_BUS_VALUES:
        args.setdefault("disk_bus", args["disk_format"])
        del args["disk_format"]

    _ENUM_VALID_SETS = {
        "display":       VALID_DISPLAY_MODES,
        "gpu":           VALID_GPU_TYPES,
        "audio":         VALID_AUDIO_TYPES,
        "network_mode":  VALID_NETWORK_MODES,
        "disk_format":   VALID_DISK_FORMATS,
        "bios":          VALID_BIOS,
        "machine_arch":  VALID_MACHINE_ARCH,
        "machine_class": VALID_MACHINE_CLASS,
        "os_type":       VALID_OS_TYPES,
    }
    for field, valid_set in _ENUM_VALID_SETS.items():
        if field in args and args[field] is not None:
            val = str(args[field]).lower().strip()
            args[field] = val if val in valid_set else _ENUM_DEFAULTS[field]

    # Path fields
    for path_field in ("iso_path", "kernel_path", "initrd_path"):
        if path_field in args and args[path_field]:
            args[path_field] = _fix_path(str(args[path_field]))

    # ARM/raspi: force kvm=False
    mt_lower   = str(args.get("machine_type", "")).lower()
    arch_lower = str(args.get("machine_arch", "")).lower()
    bin_lower  = str(args.get("qemu_binary", "")).lower()
    if (any(arm in mt_lower for arm in _ARM_MACHINE_TYPES)
            or arch_lower in _ARM_MACHINE_ARCHS
            or "aarch64" in bin_lower):
        args["kvm"]       = False
        args["hugepages"] = False

    # CPU model: reject ARM CPUs on x86 VMs
    if "cpu_model" in args and args["cpu_model"]:
        cpu = str(args["cpu_model"]).lower().strip()
        if any(arm in cpu for arm in _ARM_CPU_MODELS) and str(args.get("machine_arch","x86_64")).lower() == "x86_64":
            args["cpu_model"] = "host"

    # MAC address validation
    if "mac_address" in args and args["mac_address"]:
        mac = str(args["mac_address"]).strip()
        if not re.match(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$", mac):
            args.pop("mac_address", None)

    # Bridge interface: reject raw ethernet/wifi interfaces
    if "bridge_iface" in args and args["bridge_iface"]:
        br = str(args["bridge_iface"]).strip()
        is_raw = any(br.startswith(p) for p in _BAD_BRIDGE_PREFIXES)
        if br in _BAD_BRIDGE_IFACES or is_raw:
            args["bridge_iface"] = _DEFAULT_BRIDGE

    # Memory: cap at memory_max_ratio of host RAM, minimum memory_min_mb
    if "memory_mb" in args and args["memory_mb"]:
        try:
            import psutil
            host_mb = psutil.virtual_memory().total // (1024 * 1024)
            max_allowed = max(int(host_mb * _BOUNDS["memory_max_ratio"]), 4096)
            args["memory_mb"] = max(_BOUNDS["memory_min_mb"], min(args["memory_mb"], max_allowed))
        except Exception:
            args["memory_mb"] = max(_BOUNDS["memory_min_mb"], args["memory_mb"])

    # CPU cores: cap at host logical core count
    if "cpu_cores" in args and args["cpu_cores"]:
        try:
            import psutil
            args["cpu_cores"] = max(1, min(args["cpu_cores"], psutil.cpu_count(logical=True)))
        except Exception:
            args["cpu_cores"] = max(1, args["cpu_cores"])

    # Disk size: bounded by config
    if "disk_size_gb" in args and args["disk_size_gb"]:
        args["disk_size_gb"] = max(_BOUNDS["disk_min_gb"], min(int(args["disk_size_gb"]), _BOUNDS["disk_max_gb"]))

    # Port numbers: valid range
    for port_field in ("vnc_port", "spice_port"):
        if port_field in args and args[port_field]:
            p = int(args[port_field])
            if not (_BOUNDS["port_min"] <= p <= _BOUNDS["port_max"]):
                args.pop(port_field, None)

    # extra_args: must be list of short strings
    if "extra_args" in args and args["extra_args"]:
        if not isinstance(args["extra_args"], list):
            args["extra_args"] = []
        else:
            args["extra_args"] = [
                str(a) for a in args["extra_args"]
                if isinstance(a, (str, int)) and len(str(a)) < _BOUNDS["extra_arg_max_len"]
            ]

    # Snapshot / network names: alphanumeric only
    for field in ("snap_name", "snapshot_name"):
        if field in args and args[field]:
            args[field] = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(args[field]))
    if "net_name" in args and args["net_name"]:
        args["net_name"] = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(args["net_name"]))
    if "profile_name" in args and args["profile_name"]:
        args["profile_name"] = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(args["profile_name"]).lower())

    # Text fields: strip path-like content
    for text_field in ("os_name", "description", "hostname"):
        if text_field in args and args[text_field]:
            val = str(args[text_field])
            if "/" in val or chr(92) in val or "`" in val or "$(" in val:
                args[text_field] = re.sub(r"[/\\`$()]", "", val)

    # Remove empty optional fields
    for f in _OPTIONAL_REMOVABLE:
        if f in args and (args[f] is None or args[f] == ""):
            args.pop(f, None)

    return args

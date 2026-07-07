"""
qemu_config.py — Machine Configuration Dataclasses & Presets
Part 1 of 4: QEMU/KVM Ollama Wrapper
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
import json
import os
import platform
import sys
import uuid
import subprocess
import shutil

import psutil

_CFG  = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_DIRS = _CFG["dirs"]
_MC   = _CFG["machine_config_defaults"]
_DC   = _CFG["disk_config_defaults"]
_NC   = _CFG["network_config_defaults"]

# ─────────────────────────────────────────────
#  OVMF AUTO-DETECTION
#  Searches all known install locations across distros
# ─────────────────────────────────────────────

_OVMF_SEARCH_PATHS      = _CFG["ovmf_search_paths"]
_OVMF_VARS_SEARCH_PATHS = _CFG["ovmf_vars_search_paths"]
_OVMF_MS_CODE_PATHS     = _CFG["ovmf_ms_code_paths"]
_OVMF_MS_VARS_PATHS     = _CFG["ovmf_ms_vars_paths"]


# Scans a list of paths and returns the first one that exists on disk.
# In: List[str] paths → Out: str | None
def _find_first(paths: List[str]) -> Optional[str]:
    for p in paths:
        if os.path.exists(p):
            return p
    return None


# Searches all known distro locations for OVMF firmware files.
# In: nothing → Out: dict with keys code, vars, ms_code, ms_vars, available
def detect_ovmf() -> Dict[str, Optional[str]]:
    """Auto-detect OVMF firmware paths on this system.

    Returns:
        Dict with keys ``code``, ``vars``, ``ms_code``, ``ms_vars``,
        and ``available`` (True only when both code and vars are found).

    Example::

        detect_ovmf()
        # → {"code": "/usr/share/OVMF/OVMF_CODE.fd",
        #    "vars": "/usr/share/OVMF/OVMF_VARS.fd",
        #    "ms_code": None, "ms_vars": None, "available": True}
    """
    code    = _find_first(_OVMF_SEARCH_PATHS)
    vars_   = _find_first(_OVMF_VARS_SEARCH_PATHS)
    ms_code = _find_first(_OVMF_MS_CODE_PATHS)
    ms_vars = _find_first(_OVMF_MS_VARS_PATHS)
    return {
        "code":      code,
        "vars":      vars_,
        "ms_code":   ms_code,
        "ms_vars":   ms_vars,
        "available": code is not None and vars_ is not None,
    }


# Run detection once at import time
OVMF = detect_ovmf()

# Dynamic BIOS_OPTIONS built from detected paths
BIOS_OPTIONS: Dict[str, Optional[str]] = {
    "seabios": None,
    "ovmf":    OVMF["code"],
    "ovmf_ms": OVMF["ms_code"] or OVMF["code"],  # fallback to plain OVMF if no secboot
}


# Probes the host for KVM, QEMU version, CPU flags, RAM, disk, and arch.
# In: nothing → Out: dict with full capability report
def check_system_capabilities() -> Dict[str, Any]:
    """
    Probe the host system for KVM, OVMF, QEMU version, CPU features, etc.
    Used by the AI to answer compatibility questions.
    """
    caps = {}

    # KVM — Linux-only hardware accelerator
    if sys.platform == "linux":
        caps["kvm_available"] = os.path.exists("/dev/kvm")
        caps["kvm_readable"]  = os.access("/dev/kvm", os.R_OK | os.W_OK)
    else:
        caps["kvm_available"] = False
        caps["kvm_readable"]  = False

    # QEMU
    qemu = shutil.which("qemu-system-x86_64")
    caps["qemu_installed"] = qemu is not None
    if qemu:
        try:
            r = subprocess.run([qemu, "--version"], capture_output=True, text=True)
            caps["qemu_version"] = r.stdout.split("\n")[0]
        except Exception:
            caps["qemu_version"] = "unknown"

    # ARM QEMU (for Pi emulation)
    caps["qemu_arm_installed"]    = shutil.which("qemu-system-aarch64") is not None
    caps["qemu_arm_v7_installed"] = shutil.which("qemu-system-arm") is not None

    # OVMF
    caps["ovmf"] = OVMF

    # CPU info — use platform module; cpu flags only available on Linux via /proc/cpuinfo
    try:
        caps["host_cpu"] = platform.processor() or "unknown"
        cpu_flags: List[str] = []
        if sys.platform == "linux":
            with open("/proc/cpuinfo") as f:
                cpuinfo = f.read()
            cpu_flags = next(
                (l.split(":")[1].strip().split() for l in cpuinfo.splitlines() if l.startswith("flags")), []
            )
        caps["cpu_flags"] = cpu_flags
        caps["vmx"]    = "vmx"    in cpu_flags   # Intel VT-x
        caps["svm"]    = "svm"    in cpu_flags   # AMD-V
        caps["avx2"]   = "avx2"   in cpu_flags
        caps["avx512"] = "avx512f" in cpu_flags
    except Exception:
        caps["host_cpu"] = "unknown"
        caps["vmx"] = caps["svm"] = caps["avx2"] = caps["avx512"] = False

    # Architecture — platform.machine() works on all OSes
    caps["host_arch"] = platform.machine() or "x86_64"

    # Memory — psutil works on Linux, macOS, and Windows
    try:
        caps["host_memory_mb"] = psutil.virtual_memory().total // (1024 * 1024)
    except Exception:
        caps["host_memory_mb"] = 0

    # CPU core count — os.cpu_count() works everywhere
    caps["host_cpu_cores"] = os.cpu_count() or 1

    # Disk space in home — shutil.disk_usage() works on all OSes
    try:
        usage = shutil.disk_usage(os.path.expanduser("~"))
        caps["home_free_gb"] = usage.free // (1024 ** 3)
    except Exception:
        caps["home_free_gb"] = 0

    return caps


# ─────────────────────────────────────────────
#  ENUMS / CONSTANTS
# ─────────────────────────────────────────────

MACHINE_TYPES:   Dict[str, str]           = _CFG["machine_types"]
CPU_PRESETS:     Dict[str, str]           = _CFG["cpu_presets"]
GPU_PRESETS:     Dict[str, Optional[str]] = _CFG["gpu_presets"]
AUDIO_PRESETS:   Dict[str, Optional[str]] = _CFG["audio_presets"]
NETWORK_MODELS:  List[str]                = _CFG["network_models"]


# ─────────────────────────────────────────────
#  DISK CONFIG
# ─────────────────────────────────────────────

@dataclass
class DiskConfig:
    path:       str
    size_gb:    int  = _DC["size_gb"]
    format:     str  = _DC["format"]
    bus:        str  = _DC["bus"]
    cache:      str  = _DC["cache"]
    discard:    bool = _DC["discard"]
    ssd:        bool = _DC["ssd"]
    boot:       bool = _DC["boot"]
    disk_model: str  = ""

    # Coerces size_gb to int — guards against AI sending strings like "60".
    # In: self (post-construction) → Out: nothing (self-mutation)
    def __post_init__(self):
        # Coerce string values from AI (it sometimes sends "60" instead of 60)
        self.size_gb = int(self.size_gb)

    # Converts this disk config into -drive / -device QEMU args for its bus type.
    # In: int index → Out: List[str]
    def to_qemu_args(self, index: int = 0) -> List[str]:
        drive_id = f"drive{index}"
        args = [
            "-drive",
            f"file={self.path},"
            f"format={self.format},"
            f"id={drive_id},"
            f"cache={self.cache},"
            f"if=none"
            + (",discard=unmap" if self.discard else ""),
        ]
        if self.bus == "nvme":
            model_suffix = f",model={self.disk_model}" if self.disk_model else ""
            args += ["-device", f"nvme,drive={drive_id},serial=nvme{index}{model_suffix}"]
        elif self.bus == "virtio":
            ssd_hint = ",rotation_rate=1" if self.ssd else ""
            args += ["-device", f"virtio-blk-pci,drive={drive_id}{ssd_hint}"]
        elif self.bus == "scsi":
            product_suffix = f",product={self.disk_model}" if self.disk_model else ""
            args += ["-device", f"scsi-hd,drive={drive_id}{product_suffix}"]
        elif self.bus == "sata":
            model_suffix = f",model={self.disk_model}" if self.disk_model else ""
            # q35 uses ICH9-AHCI — the controller is added by QemuArgBuilder._disks()
            args += ["-device", f"ide-hd,drive={drive_id},bus=ahci.{index}{model_suffix}"]
        else:
            # ide fallback — only works on non-q35 machines
            model_suffix = f",model={self.disk_model}" if self.disk_model else ""
            args = [
                "-drive",
                f"file={self.path},format={self.format},if=ide,cache={self.cache}"
                + (",discard=unmap" if self.discard else "")
                + model_suffix,
            ]
        return args


# ─────────────────────────────────────────────
#  NETWORK CONFIG
# ─────────────────────────────────────────────

@dataclass
class NetworkConfig:
    mode:              str           = _NC["mode"]
    model:             str           = _NC["model"]
    mac:               Optional[str] = None
    bridge:            str           = _NC["bridge"]
    ip:                Optional[str] = None
    hostname:          Optional[str] = None
    port_forwards:     List[tuple]   = field(default_factory=list)
    manufacturer_hint: Optional[str] = None

    # Generates or validates the MAC address on init.
    # In: self (post-construction) → Out: nothing (self-mutation)
    def __post_init__(self):
        if not self.mac:
            self._generate_mac()
        else:
            # Validate and fix incoming MAC — must be exactly 6 octets
            self.mac = self._fix_mac(self.mac)

    # OUIs keyed by normalized manufacturer keyword — used to pick a vendor-consistent MAC.
    _VENDOR_OUI_MAP = _CFG["vendor_oui_map"]
    _ALL_OUIS       = [oui for ouis in _CFG["vendor_oui_map"].values() for oui in ouis]

    # Generates a MAC using a vendor-matched OUI when possible, otherwise any real OUI.
    # In: nothing → Out: sets self.mac
    def _generate_mac(self):
        import random
        hint = (self.manufacturer_hint or "").lower()
        pool = next(
            (ouis for key, ouis in self._VENDOR_OUI_MAP.items() if key in hint),
            self._ALL_OUIS,
        )
        oui = random.choice(pool)
        device = uuid.uuid4().bytes[:3]
        self.mac = oui + ":" + ":".join(f"{b:02X}" for b in device)

    # Validates or salvages a MAC string; generates a fresh one if unfixable.
    # In: str → Out: str
    @staticmethod
    def _fix_mac(mac: str) -> str:
        """Validate MAC; return it unchanged if valid, else generate a new one.

        Args:
            mac: MAC address string to validate.

        Returns:
            The input MAC if it matches ``XX:XX:XX:XX:XX:XX``, otherwise a
            freshly generated random MAC.

        Example::

            NetworkConfig._fix_mac("AA:BB:CC:DD:EE:FF")
            # → "AA:BB:CC:DD:EE:FF"
            NetworkConfig._fix_mac("not-a-mac")
            # → "52:54:00:xx:xx:xx"  (random)
        """
        import re
        mac = mac.strip()
        if re.match(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$", mac):
            return mac
        # Try to salvage — take first 6 hex pairs
        parts = re.findall(r"[0-9a-fA-F]{2}", mac.replace(":","").replace("-",""))
        if len(parts) >= 6:
            return ":".join(parts[:6])
        # Give up — generate fresh MAC using a real vendor OUI
        import random
        oui = random.choice(NetworkConfig._VENDOR_OUIS)
        device = uuid.uuid4().bytes[:3]
        return oui + ":" + ":".join(f"{b:02X}" for b in device)

    # Returns -netdev/-device args for NAT or bridge networking.
    # In: nothing → Out: List[str]
    def to_qemu_args(self) -> List[str]:
        args = []
        if self.mode == "none":
            args += ["-nic", "none"]
            return args
        if self.mode == "nat":
            fwd = ""
            for hport, gport, proto in self.port_forwards:
                fwd += f",hostfwd={proto}::{hport}-:{gport}"
            args += [
                "-netdev", f"user,id=net0{fwd}",
                "-device", f"{self.model},netdev=net0,mac={self.mac}",
            ]
        elif self.mode == "bridge":
            args += [
                "-netdev", f"bridge,id=net0,br={self.bridge}",
                "-device", f"{self.model},netdev=net0,mac={self.mac}",
            ]
        return args


# ─────────────────────────────────────────────
#  MACHINE CONFIG
# ─────────────────────────────────────────────

@dataclass
class MachineConfig:
    name:            str           = _MC["name"]
    vm_id:           str           = field(default_factory=lambda: str(uuid.uuid4())[:8])
    hostname:        str           = _MC["hostname"]
    description:     str           = ""
    machine_class:   str           = _MC["machine_class"]
    machine_type:    str           = _MC["machine_type"]
    bios:            str           = _MC["bios"]
    uefi:            bool          = _MC["uefi"]
    uefi_vars:       Optional[str] = None
    cpu_model:       str           = _MC["cpu_model"]
    cpu_cores:       int           = _MC["cpu_cores"]
    cpu_threads:     int           = _MC["cpu_threads"]
    cpu_sockets:     int           = _MC["cpu_sockets"]
    cpu_features:    List[str]     = field(default_factory=list)
    kvm:             bool          = _MC["kvm"]
    cpu_pinning:     Optional[List[int]] = None
    memory_mb:       int           = _MC["memory_mb"]
    hugepages:       bool          = _MC["hugepages"]
    balloon:         bool          = _MC["balloon"]
    gpu:             str           = _MC["gpu"]
    display:         str           = _MC["display"]
    vnc_port:        Optional[int] = None
    vnc_bind_local:  bool          = False   # True → bind to 127.0.0.1 + require password (remote mode)
    spice_port:      Optional[int] = None
    opengl:          bool          = _MC["opengl"]
    resolution:      str           = _MC["resolution"]
    audio:           str           = _MC["audio"]
    disks:           List[DiskConfig]    = field(default_factory=list)
    networks:        List[NetworkConfig] = field(default_factory=list)
    os_type:         str           = _MC["os_type"]
    os_name:         str           = ""
    iso_path:        Optional[str] = None
    boot_order:      str           = _MC["boot_order"]
    smbios_type:     str           = _MC["smbios_type"]
    product_name:    str           = _MC["product_name"]
    manufacturer:    str           = _MC["manufacturer"]
    serial_number:   str           = _MC["serial_number"]
    board_product:   str           = _MC["board_product"]
    bios_version:    str           = _MC["bios_version"]
    bios_vendor:     str           = _MC["bios_vendor"]
    kernel_path:     Optional[str] = None
    initrd_path:     Optional[str] = None
    kernel_cmdline:  str           = _MC["kernel_cmdline"]
    battery:         bool          = _MC["battery"]
    tablet:          bool          = _MC["tablet"]
    iommu:           bool          = _MC["iommu"]
    nested_virt:     bool          = _MC["nested_virt"]
    hpet:            bool          = _MC["hpet"]
    rtc_clock:       str           = _MC["rtc_clock"]
    tsc_deadline:    bool          = _MC["tsc_deadline"]
    kvm_pv_features: bool          = _MC["kvm_pv_features"]
    hardened:        bool          = _MC["hardened"]
    stealth:         bool          = _MC.get("stealth", False)
    tpm:             bool          = _MC.get("tpm", False)
    hugepages_path:  str           = _MC["hugepages_path"]
    extra_args:      List[str]     = field(default_factory=list)
    # ARM / non-x86 support
    qemu_binary:     str           = _MC["qemu_binary"]
    machine_arch:    str           = _MC["machine_arch"]
    pid:             Optional[int] = field(default=None, repr=False)
    monitor_socket:  str           = field(default="", repr=False)
    qmp_socket:      str           = field(default="", repr=False)
    # Windows-only: TCP ports for QMP/monitor/serial (0 = use Unix socket on Linux/macOS)
    qmp_tcp_port:     int = field(default=0)
    monitor_tcp_port: int = field(default=0)
    serial_tcp_port:  int = field(default=0)

    # Coerces int fields and auto-falls back to SeaBIOS if OVMF is absent.
    # In: self (post-construction) → Out: nothing (self-mutation)
    def __post_init__(self):
        # Coerce types that AI may send as strings
        self.cpu_cores   = int(self.cpu_cores)
        self.cpu_threads = int(self.cpu_threads)
        self.memory_mb   = int(self.memory_mb)
        if self.stealth:
            self.hardened = True   # stealth implies hardened
        if self.bios in ("ovmf", "ovmf_ms"):
            if OVMF["available"]:
                self.uefi = True   # bios=ovmf always implies uefi=True
            else:
                self.bios = "seabios"
                self.uefi = False

    # Returns the VM's directory path (~/.qemu_vms/<name>).
    # In: nothing → Out: str
    def get_vm_dir(self) -> str:
        return os.path.expanduser(f"~/.qemu_vms/{self.name}")

    # Returns the path to the VM's config.json.
    # In: nothing → Out: str
    def get_config_path(self) -> str:
        return os.path.join(self.get_vm_dir(), "config.json")

    # Returns the QMP socket address — Unix path on Linux/macOS, tcp:host:port on Windows.
    # In: nothing → Out: str
    def get_qmp_socket(self) -> str:
        if sys.platform == "win32" and self.qmp_tcp_port:
            return f"tcp:127.0.0.1:{self.qmp_tcp_port}"
        return os.path.join(self.get_vm_dir(), "qmp.sock")

    # Returns the monitor socket address — Unix path on Linux/macOS, tcp:host:port on Windows.
    # In: nothing → Out: str
    def get_monitor_socket(self) -> str:
        if sys.platform == "win32" and self.monitor_tcp_port:
            return f"tcp:127.0.0.1:{self.monitor_tcp_port}"
        return os.path.join(self.get_vm_dir(), "monitor.sock")

    # Serializes the config to a dict, stripping runtime-only fields (pid, sockets).
    # In: nothing → Out: dict
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("pid", None)
        d.pop("monitor_socket", None)
        d.pop("qmp_socket", None)
        return d

    # Writes the config to ~/.qemu_vms/<name>/config.json.
    # In: nothing → Out: nothing
    def save(self):
        os.makedirs(self.get_vm_dir(), exist_ok=True)
        with open(self.get_config_path(), "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    # Loads and deserializes a VM config from disk by name.
    # In: str name → Out: MachineConfig
    @classmethod
    def load(cls, name: str) -> "MachineConfig":
        path = os.path.expanduser(f"~/.qemu_vms/{name}/config.json")
        if not os.path.exists(path):
            raise FileNotFoundError(f"No VM config found for '{name}'")
        with open(path) as f:
            data = json.load(f)
        data["disks"]    = [DiskConfig(**d) for d in data.get("disks", [])]
        data["networks"] = [NetworkConfig(**n) for n in data.get("networks", [])]
        return cls(**data)


# ─────────────────────────────────────────────
#  CUSTOM PROFILE STORAGE
#  Profiles are saved to ~/.qemu_vms/_profiles/
# ─────────────────────────────────────────────

PROFILES_DIR = os.path.expanduser(_DIRS["profiles"])


# Reads all .json files from ~/.qemu_vms/_profiles/ into a dict.
# In: nothing → Out: dict
def _load_custom_profiles() -> Dict[str, Dict[str, Any]]:
    profiles = {}
    if not os.path.isdir(PROFILES_DIR):
        return profiles
    for fname in os.listdir(PROFILES_DIR):
        if fname.endswith(".json"):
            key = fname[:-5]
            try:
                with open(os.path.join(PROFILES_DIR, fname)) as f:
                    profiles[key] = json.load(f)
            except Exception:
                pass
    return profiles


# Sanitizes the name and writes a custom profile JSON to _profiles/.
# In: str name, dict profile_data → Out: dict with success and path
def save_custom_profile(name: str, profile_data: Dict[str, Any]) -> Dict[str, Any]:
    """Save a custom hardware profile to disk."""
    os.makedirs(PROFILES_DIR, exist_ok=True)
    # Sanitise name
    safe_name = name.lower().replace(" ", "_").replace("-", "_")
    path = os.path.join(PROFILES_DIR, f"{safe_name}.json")
    profile_data["_custom"] = True
    profile_data["_name"]   = safe_name
    with open(path, "w") as f:
        json.dump(profile_data, f, indent=2)
    return {"success": True, "profile_name": safe_name, "path": path}


# Deletes a custom profile JSON file by name.
# In: str name → Out: dict with success
def delete_custom_profile(name: str) -> Dict[str, Any]:
    safe_name = name.lower().replace(" ", "_").replace("-", "_")
    path = os.path.join(PROFILES_DIR, f"{safe_name}.json")
    if not os.path.exists(path):
        return {"success": False, "error": f"Profile '{safe_name}' not found."}
    os.remove(path)
    return {"success": True, "message": f"Profile '{safe_name}' deleted."}


# ─────────────────────────────────────────────
#  BUILT-IN HARDWARE PROFILES
# ─────────────────────────────────────────────

HARDWARE_PROFILES: Dict[str, Dict[str, Any]] = _CFG["hardware_profiles"]


# Merges built-in HARDWARE_PROFILES with any saved custom profiles.
# In: nothing → Out: dict
def get_all_profiles() -> Dict[str, Dict[str, Any]]:
    """Return built-in + custom profiles merged into a single dict.

    Returns:
        Mapping of profile name → profile data dict. Custom profiles from
        disk override built-in profiles with the same name.

    Example::

        profiles = get_all_profiles()
        profiles.keys()
        # → dict_keys(["dell_g15_5520", "lenovo_thinkpad_x1", ..., "my_custom"])
    """
    all_profiles = dict(HARDWARE_PROFILES)
    all_profiles.update(_load_custom_profiles())
    return all_profiles


# Copies all matching profile fields onto a MachineConfig.
# In: MachineConfig, str profile_name → Out: MachineConfig
def apply_profile(config: MachineConfig, profile_name: str) -> MachineConfig:
    all_profiles = get_all_profiles()
    profile = all_profiles.get(profile_name)
    if not profile:
        raise ValueError(
            f"Unknown profile '{profile_name}'. "
            f"Available: {list(all_profiles.keys())}"
        )
    skip_keys = {"_custom", "_name", "_requires", "_notes"}
    for key, value in profile.items():
        if key in skip_keys:
            continue
        if hasattr(config, key):
            setattr(config, key, value)
    return config


# Returns a flat list of all profiles with name, description, arch, and custom flag.
# In: nothing → Out: List[dict]
def list_profiles() -> List[Dict[str, str]]:
    all_profiles = get_all_profiles()
    result = []
    for k, v in all_profiles.items():
        entry = {
            "name":        k,
            "description": v.get("description", ""),
            "arch":        v.get("machine_arch", "x86_64"),
            "custom":      str(v.get("_custom", False)),
        }
        if "_notes" in v:
            entry["notes"] = v["_notes"]
        result.append(entry)
    return result


# Compares a profile's requirements against the host (KVM, RAM, cores, OVMF, arch).
# In: str profile_name → Out: dict with compatible, issues, warnings
def check_profile_compatibility(profile_name: str) -> Dict[str, Any]:
    """
    Check whether a given profile can run on this host system.
    Returns compatibility status, issues found, and alternatives.
    """
    _THRESHOLDS = _CFG["compatibility_thresholds"]

    all_profiles = get_all_profiles()
    profile      = all_profiles.get(profile_name)
    caps         = check_system_capabilities()

    if not profile:
        return {"compatible": False, "error": f"Profile '{profile_name}' not found."}

    issues       = []
    warnings     = []
    alternatives = []

    # KVM check
    if profile.get("kvm", True) and not caps["kvm_available"]:
        issues.append("KVM not available — VM will be very slow (software emulation only). Enable VT-x/AMD-V in BIOS.")

    # Architecture check
    arch = profile.get("machine_arch", "x86_64")
    if arch == "aarch64" and not caps["qemu_arm_installed"]:
        issues.append("qemu-system-aarch64 not installed. Run: sudo apt install qemu-system-arm")
        alternatives.append(
            "raspberry_pi_4 requires qemu-system-aarch64."
            " Install it or use a minimal x86 Linux VM instead."
        )

    if arch == "aarch64" and caps["host_arch"] == "x86_64":
        warnings.append(
            "ARM emulation on x86 host — no KVM acceleration possible. "
            "Expect 10-50x slower than native Pi hardware."
        )

    # OVMF check
    if profile.get("uefi") and not OVMF["available"]:
        if profile.get("bios") != "seabios":
            issues.append(
                f"UEFI requested but OVMF not found. "
                f"Run: sudo apt install ovmf — or the system will fall back to SeaBIOS automatically."
            )

    # Memory check
    requested_mb = int(profile.get("memory_mb", 2048))
    host_mb      = caps.get("host_memory_mb", 0)
    if host_mb > 0 and requested_mb > host_mb * _THRESHOLDS["memory_ratio"]:
        warnings.append(
            f"Profile requests {requested_mb}MB RAM but host only has {host_mb}MB. "
            f"Consider reducing memory_mb to {host_mb // 2}MB."
        )

    # CPU core check
    requested_cores = int(profile.get("cpu_cores", 2))
    host_cores      = caps.get("host_cpu_cores", 1)
    if requested_cores > host_cores:
        warnings.append(
            f"Profile requests {requested_cores} cores but host only has {host_cores}. "
            f"QEMU will over-commit — may cause slowdowns."
        )

    # Disk space check
    free_gb = caps.get("home_free_gb", 0)
    if free_gb < _THRESHOLDS["min_disk_free_gb"]:
        warnings.append(f"Low disk space: only {free_gb}GB free in home directory.")

    compatible = len(issues) == 0
    return {
        "profile":    profile_name,
        "compatible": compatible,
        "issues":     issues,
        "warnings":   warnings,
        "alternatives": alternatives,
        "host_summary": {
            "cpu":       caps.get("host_cpu", "unknown"),
            "cores":     caps.get("host_cpu_cores"),
            "memory_mb": caps.get("host_memory_mb"),
            "kvm":       caps.get("kvm_available"),
            "ovmf":      OVMF["available"],
            "qemu_arm":  caps.get("qemu_arm_installed"),
            "arch":      caps.get("host_arch"),
        },
        "notes": profile.get("_notes", ""),
    }


# Injects OS-specific CPU features: Hyper-V flags for Windows, KVM PV for Linux, vendor tweak for macOS.
# In: MachineConfig → Out: MachineConfig
def apply_os_hints(config: MachineConfig) -> MachineConfig:
    os_type = config.os_type.lower()
    _os_cpu = _CFG.get("os_cpu_features", {})
    if "windows" in os_type or os_type == "windows":
        config.cpu_features += _os_cpu.get("windows", [])
        config.hpet = False
        if config.rtc_clock == _MC["rtc_clock"]:
            config.rtc_clock = "localtime"
        config.tsc_deadline = True
    elif "linux" in os_type:
        config.kvm_pv_features = True
    elif "macos" in os_type:
        config.cpu_features += _os_cpu.get("macos", [])
        config.machine_type = "q35"
    return config

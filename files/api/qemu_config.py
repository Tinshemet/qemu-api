"""
qemu_config.py — Machine Configuration Dataclasses & Presets
Part 1 of 4: QEMU/KVM Ollama Wrapper
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any
import json
import os
import uuid
import subprocess
import shutil

# ─────────────────────────────────────────────
#  OVMF AUTO-DETECTION
#  Searches all known install locations across distros
# ─────────────────────────────────────────────

_OVMF_SEARCH_PATHS = [
    # Debian/Ubuntu/Mint
    "/usr/share/OVMF/OVMF_CODE.fd",
    "/usr/share/OVMF/OVMF_CODE_4M.fd",
    # Fedora/RHEL
    "/usr/share/edk2/ovmf/OVMF_CODE.fd",
    "/usr/share/edk2-ovmf/x64/OVMF_CODE.fd",
    # Arch
    "/usr/share/ovmf/x64/OVMF_CODE.fd",
    "/usr/share/edk2-ovmf/OVMF_CODE.fd",
    # openSUSE
    "/usr/share/qemu/ovmf-x86_64-code.bin",
]

_OVMF_VARS_SEARCH_PATHS = [
    # 4M variants first — these match modern OVMF_CODE_4M.fd installs
    "/usr/share/OVMF/OVMF_VARS_4M.fd",
    "/usr/share/OVMF/OVMF_VARS.fd",
    "/usr/share/edk2/ovmf/OVMF_VARS.fd",
    "/usr/share/edk2-ovmf/x64/OVMF_VARS.fd",
    "/usr/share/ovmf/x64/OVMF_VARS.fd",
    "/usr/share/edk2-ovmf/OVMF_VARS.fd",
    "/usr/share/qemu/ovmf-x86_64-vars.bin",
]

_OVMF_MS_CODE_PATHS = [
    "/usr/share/OVMF/OVMF_CODE.ms.fd",
    "/usr/share/OVMF/OVMF_CODE_4M.ms.fd",
    "/usr/share/edk2-ovmf/x64/OVMF_CODE.secboot.fd",
    "/usr/share/ovmf/x64/OVMF_CODE.secboot.fd",
]

_OVMF_MS_VARS_PATHS = [
    "/usr/share/OVMF/OVMF_VARS.ms.fd",
    "/usr/share/OVMF/OVMF_VARS_4M.ms.fd",
    "/usr/share/edk2-ovmf/x64/OVMF_VARS.secboot.fd",
    "/usr/share/ovmf/x64/OVMF_VARS.secboot.fd",
]


def _find_first(paths: List[str]) -> Optional[str]:
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def detect_ovmf() -> Dict[str, Optional[str]]:
    """
    Auto-detect OVMF firmware paths on this system.
    Returns a dict with keys: code, vars, ms_code, ms_vars, available
    """
    code = _find_first(_OVMF_SEARCH_PATHS)
    vars_ = _find_first(_OVMF_VARS_SEARCH_PATHS)
    ms_code = _find_first(_OVMF_MS_CODE_PATHS)
    ms_vars = _find_first(_OVMF_MS_VARS_PATHS)
    return {
        "code":     code,
        "vars":     vars_,
        "ms_code":  ms_code,
        "ms_vars":  ms_vars,
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


def check_system_capabilities() -> Dict[str, Any]:
    """
    Probe the host system for KVM, OVMF, QEMU version, CPU features, etc.
    Used by the AI to answer compatibility questions.
    """
    caps = {}

    # KVM
    caps["kvm_available"] = os.path.exists("/dev/kvm")
    caps["kvm_readable"] = os.access("/dev/kvm", os.R_OK | os.W_OK)

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
    caps["qemu_arm_installed"] = shutil.which("qemu-system-aarch64") is not None
    caps["qemu_arm_v7_installed"] = shutil.which("qemu-system-arm") is not None

    # OVMF
    caps["ovmf"] = OVMF

    # CPU info
    try:
        with open("/proc/cpuinfo") as f:
            cpuinfo = f.read()
        caps["host_cpu"] = next(
            (l.split(":")[1].strip() for l in cpuinfo.splitlines() if "model name" in l), "unknown"
        )
        caps["cpu_flags"] = next(
            (l.split(":")[1].strip().split() for l in cpuinfo.splitlines() if l.startswith("flags")), []
        )
        caps["vmx"] = "vmx" in caps["cpu_flags"]   # Intel VT-x
        caps["svm"] = "svm" in caps["cpu_flags"]   # AMD-V
        caps["avx2"] = "avx2" in caps["cpu_flags"]
        caps["avx512"] = "avx512f" in caps["cpu_flags"]
    except Exception:
        caps["host_cpu"] = "unknown"
        caps["vmx"] = False
        caps["svm"] = False

    # Architecture
    try:
        r = subprocess.run(["uname", "-m"], capture_output=True, text=True)
        caps["host_arch"] = r.stdout.strip()
    except Exception:
        caps["host_arch"] = "x86_64"

    # Memory
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal"):
                    caps["host_memory_mb"] = int(line.split()[1]) // 1024
                    break
    except Exception:
        caps["host_memory_mb"] = 0

    # CPU core count
    try:
        r = subprocess.run(["nproc"], capture_output=True, text=True)
        caps["host_cpu_cores"] = int(r.stdout.strip())
    except Exception:
        caps["host_cpu_cores"] = 1

    # Disk space in home
    try:
        st = os.statvfs(os.path.expanduser("~"))
        caps["home_free_gb"] = (st.f_bavail * st.f_frsize) // (1024**3)
    except Exception:
        caps["home_free_gb"] = 0

    return caps


# ─────────────────────────────────────────────
#  ENUMS / CONSTANTS
# ─────────────────────────────────────────────

MACHINE_TYPES = {
    "pc":      "i440FX + PIIX (legacy BIOS)",
    "q35":     "Q35 + ICH9 (modern, PCIe, recommended)",
    "microvm": "Minimal microvm (containers/cloud)",
    "virt":    "ARM virt platform (aarch64)",
}

CPU_PRESETS = {
    "host":        "-cpu host",
    "kvm64":       "-cpu kvm64",
    "Haswell":     "-cpu Haswell,+avx2",
    "Broadwell":   "-cpu Broadwell,+avx2",
    "SandyBridge": "-cpu SandyBridge",
    "Skylake":     "-cpu Skylake-Client,+avx512f",
    "IceLake":     "-cpu Icelake-Client,+avx512f",
    "EPYC":        "-cpu EPYC",
    "Opteron_G5":  "-cpu Opteron_G5",
    "cortex-a72":  "-cpu cortex-a72",   # RPi 4
    "cortex-a53":  "-cpu cortex-a53",   # RPi 3
}

GPU_PRESETS = {
    "virtio":  "virtio-vga-gl",
    "qxl":     "qxl-vga",
    "vga":     "VGA",
    "vmware":  "vmware-svga",
    "none":    None,
    "gt1030":  "vfio-pci",
}

AUDIO_PRESETS = {
    "none":  None,
    "ac97":  "ac97",
    "hda":   "intel-hda",
    "ich9":  "ich9-intel-hda",
    "usb":   "usb-audio",
}

NETWORK_MODELS = ["virtio-net-pci", "e1000", "e1000e", "rtl8139", "vmxnet3"]


# ─────────────────────────────────────────────
#  DISK CONFIG
# ─────────────────────────────────────────────

@dataclass
class DiskConfig:
    path: str
    size_gb: int = 60
    format: str = "qcow2"
    bus: str = "virtio"
    cache: str = "writeback"
    discard: bool = True
    ssd: bool = False
    boot: bool = False

    def __post_init__(self):
        # Coerce string values from AI (it sometimes sends "60" instead of 60)
        self.size_gb = int(self.size_gb)

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
            args += ["-device", f"nvme,drive={drive_id},serial=nvme{index}"]
        elif self.bus == "virtio":
            ssd_hint = ",rotation_rate=1" if self.ssd else ""
            args += ["-device", f"virtio-blk-pci,drive={drive_id}{ssd_hint}"]
        elif self.bus == "scsi":
            args += ["-device", f"scsi-hd,drive={drive_id}"]
        else:
            args = [
                "-drive",
                f"file={self.path},format={self.format},if=ide,cache={self.cache}"
                + (",discard=unmap" if self.discard else ""),
            ]
        return args


# ─────────────────────────────────────────────
#  NETWORK CONFIG
# ─────────────────────────────────────────────

@dataclass
class NetworkConfig:
    mode: str = "nat"
    model: str = "virtio-net-pci"
    mac: Optional[str] = None
    bridge: str = "virbr0"
    ip: Optional[str] = None
    hostname: Optional[str] = None
    port_forwards: List[tuple] = field(default_factory=list)

    def __post_init__(self):
        if not self.mac:
            self._generate_mac()
        else:
            # Validate and fix incoming MAC — must be exactly 6 octets
            self.mac = self._fix_mac(self.mac)

    def _generate_mac(self):
        raw = uuid.uuid4().hex[:10]
        self.mac = "52:54:" + ":".join(raw[i:i+2] for i in range(0, 10, 2))

    @staticmethod
    def _fix_mac(mac: str) -> str:
        """Validate MAC — if invalid, generate a fresh one."""
        import re
        mac = mac.strip()
        if re.match(r"^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$", mac):
            return mac
        # Try to salvage — take first 6 hex pairs
        parts = re.findall(r"[0-9a-fA-F]{2}", mac.replace(":","").replace("-",""))
        if len(parts) >= 6:
            return ":".join(parts[:6])
        # Give up — generate fresh locally-administered MAC
        raw = uuid.uuid4().hex[:10]
        return "52:54:" + ":".join(raw[i:i+2] for i in range(0, 10, 2))

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
    name: str = "vm-default"
    vm_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    hostname: str = "localhost"
    description: str = ""
    machine_class: str = "desktop"
    machine_type: str = "q35"
    bios: str = "ovmf"
    uefi: bool = True
    uefi_vars: Optional[str] = None
    cpu_model: str = "host"
    cpu_cores: int = 4
    cpu_threads: int = 2
    cpu_sockets: int = 1
    cpu_features: List[str] = field(default_factory=list)
    kvm: bool = True
    cpu_pinning: Optional[List[int]] = None
    memory_mb: int = 8192
    hugepages: bool = False
    balloon: bool = True
    gpu: str = "virtio"
    display: str = "sdl"
    vnc_port: Optional[int] = None
    spice_port: Optional[int] = None
    opengl: bool = True
    resolution: str = "1920x1080"
    audio: str = "hda"
    disks: List[DiskConfig] = field(default_factory=list)
    networks: List[NetworkConfig] = field(default_factory=list)
    os_type: str = "linux"
    os_name: str = ""
    iso_path: Optional[str] = None
    boot_order: str = "cd"
    smbios_type: str = ""
    product_name: str = ""
    manufacturer: str = ""
    serial_number: str = ""
    bios_version: str = ""
    bios_vendor: str = ""
    kernel_path: Optional[str] = None
    initrd_path: Optional[str] = None
    kernel_cmdline: str = ""
    battery: bool = False
    tablet: bool = True
    iommu: bool = False
    nested_virt: bool = False
    hpet: bool = False
    rtc_clock: str = "host"
    tsc_deadline: bool = True
    kvm_pv_features: bool = True
    hugepages_path: str = "/dev/hugepages"
    extra_args: List[str] = field(default_factory=list)
    # ARM / non-x86 support
    qemu_binary: str = "qemu-system-x86_64"
    machine_arch: str = "x86_64"
    pid: Optional[int] = field(default=None, repr=False)
    monitor_socket: str = field(default="", repr=False)
    qmp_socket: str = field(default="", repr=False)

    def __post_init__(self):
        # Coerce types that AI may send as strings
        self.cpu_cores   = int(self.cpu_cores)
        self.cpu_threads = int(self.cpu_threads)
        self.memory_mb   = int(self.memory_mb)
        if self.uefi and self.bios == "ovmf" and not OVMF["available"]:
            # Auto-fallback to seabios if OVMF not found
            self.bios = "seabios"
            self.uefi = False

    def get_vm_dir(self) -> str:
        return os.path.expanduser(f"~/.qemu_vms/{self.name}")

    def get_config_path(self) -> str:
        return os.path.join(self.get_vm_dir(), "config.json")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("pid", None)
        d.pop("monitor_socket", None)
        d.pop("qmp_socket", None)
        return d

    def save(self):
        os.makedirs(self.get_vm_dir(), exist_ok=True)
        with open(self.get_config_path(), "w") as f:
            json.dump(self.to_dict(), f, indent=2)

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

PROFILES_DIR = os.path.expanduser("~/.qemu_vms/_profiles")


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


def save_custom_profile(name: str, profile_data: Dict[str, Any]) -> Dict[str, Any]:
    """Save a custom hardware profile to disk."""
    os.makedirs(PROFILES_DIR, exist_ok=True)
    # Sanitise name
    safe_name = name.lower().replace(" ", "_").replace("-", "_")
    path = os.path.join(PROFILES_DIR, f"{safe_name}.json")
    profile_data["_custom"] = True
    profile_data["_name"] = safe_name
    with open(path, "w") as f:
        json.dump(profile_data, f, indent=2)
    return {"success": True, "profile_name": safe_name, "path": path}


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

HARDWARE_PROFILES: Dict[str, Dict[str, Any]] = {
    "dell_g15_5520": {
        "machine_class": "laptop", "machine_type": "q35", "bios": "ovmf", "uefi": True,
        "cpu_model": "host", "cpu_cores": 6, "cpu_threads": 12, "memory_mb": 16384,
        "gpu": "virtio", "audio": "hda", "battery": True,
        "manufacturer": "Dell Inc.", "product_name": "G15 5520",
        "bios_vendor": "Dell Inc.", "bios_version": "1.14.0",
        "description": "Dell G15 Gaming Laptop (2022) — i5-12500H profile",
        "kvm_pv_features": True, "opengl": True, "tablet": True,
    },
    "gaming_desktop": {
        "machine_class": "desktop", "machine_type": "q35", "bios": "ovmf", "uefi": True,
        "cpu_model": "host", "cpu_cores": 8, "cpu_threads": 16, "memory_mb": 32768,
        "gpu": "virtio", "audio": "ich9", "battery": False,
        "manufacturer": "Custom Build", "product_name": "Gaming Desktop",
        "description": "High-performance gaming desktop profile", "opengl": True,
    },
    "office_laptop": {
        "machine_class": "laptop", "machine_type": "q35", "bios": "ovmf", "uefi": True,
        "cpu_model": "host", "cpu_cores": 4, "cpu_threads": 8, "memory_mb": 8192,
        "gpu": "qxl", "audio": "hda", "battery": True,
        "manufacturer": "Lenovo", "product_name": "ThinkPad E14",
        "description": "Lenovo ThinkPad-style office laptop",
    },
    "server": {
        "machine_class": "server", "machine_type": "q35", "bios": "ovmf", "uefi": True,
        "cpu_model": "EPYC", "cpu_cores": 16, "cpu_threads": 32, "memory_mb": 65536,
        "gpu": "none", "display": "none", "audio": "none", "battery": False,
        "manufacturer": "Supermicro", "product_name": "AS-1124US-TNRP",
        "description": "EPYC server profile — headless",
        "hugepages": True, "iommu": True,
    },
    "mac_mini": {
        "machine_class": "desktop", "machine_type": "q35", "bios": "ovmf", "uefi": True,
        "cpu_model": "host", "cpu_cores": 4, "cpu_threads": 8, "memory_mb": 8192,
        "os_type": "macos", "manufacturer": "Apple Inc.", "product_name": "Mac mini",
        "bios_vendor": "Apple Inc.", "description": "macOS-style Mac Mini profile",
        "gpu": "vmware", "audio": "hda",
    },
    "minimal": {
        "machine_class": "custom", "machine_type": "q35", "cpu_model": "kvm64",
        "cpu_cores": 2, "cpu_threads": 2, "memory_mb": 2048,
        "gpu": "none", "display": "none", "audio": "none",
        "description": "Minimal headless VM for CI/testing",
    },
    # ── Raspberry Pi profiles (emulated, not native) ──────────────────
    "raspberry_pi_4": {
        "machine_class": "custom",
        "machine_type": "raspi3b",       # QEMU raspi machine type
        "qemu_binary": "qemu-system-aarch64",
        "machine_arch": "aarch64",
        "bios": "seabios",               # No OVMF on ARM
        "uefi": False,
        "cpu_model": "cortex-a72",
        "cpu_cores": 4, "cpu_threads": 1,
        "memory_mb": 4096,
        "gpu": "none", "display": "sdl", "audio": "none",
        "manufacturer": "Raspberry Pi Foundation",
        "product_name": "Raspberry Pi 4 Model B",
        "description": "Emulated Raspberry Pi 4 (aarch64) — requires qemu-system-aarch64",
        "_requires": {"qemu_arm_installed": True},
        "_notes": "Pi 4 emulation is slow. For bare-metal Pi use, flash an SD card instead.",
    },
    "raspberry_pi_3b": {
        "machine_class": "custom",
        "machine_type": "raspi3b",
        "qemu_binary": "qemu-system-aarch64",
        "machine_arch": "aarch64",
        "bios": "seabios",
        "uefi": False,
        "kvm": False,
        "hugepages": False,
        "balloon": False,
        "cpu_model": "cortex-a53",
        "cpu_cores": 4, "cpu_threads": 1,
        "memory_mb": 1024,
        "gpu": "none",
        "display": "none",
        "audio": "none",
        "battery": False,
        "kvm_pv_features": False,
        "manufacturer": "Raspberry Pi Foundation",
        "product_name": "Raspberry Pi 3 Model B Rev 1.2",
        "kernel_cmdline": "console=ttyAMA0 root=/dev/mmcblk0p2 rw rootfstype=ext4 fsck.repair=yes",
        "description": "Emulated Raspberry Pi 3B — serial console only, requires Pi OS image + kernel8.img",
        "_requires": {"qemu_arm_installed": True},
        "_notes": "raspi3b has NO display output in QEMU. Connect via serial console. KVM not available on x86. Needs kernel8.img + bcm2710-rpi-3-b.dtb extracted from Pi OS image.",
    },
}


def get_all_profiles() -> Dict[str, Dict[str, Any]]:
    """Return built-in + custom profiles merged."""
    all_profiles = dict(HARDWARE_PROFILES)
    all_profiles.update(_load_custom_profiles())
    return all_profiles


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


def list_profiles() -> List[Dict[str, str]]:
    all_profiles = get_all_profiles()
    result = []
    for k, v in all_profiles.items():
        entry = {
            "name": k,
            "description": v.get("description", ""),
            "arch": v.get("machine_arch", "x86_64"),
            "custom": str(v.get("_custom", False)),
        }
        if "_notes" in v:
            entry["notes"] = v["_notes"]
        result.append(entry)
    return result


def check_profile_compatibility(profile_name: str) -> Dict[str, Any]:
    """
    Check whether a given profile can run on this host system.
    Returns compatibility status, issues found, and alternatives.
    """
    all_profiles = get_all_profiles()
    profile = all_profiles.get(profile_name)
    caps = check_system_capabilities()

    if not profile:
        return {"compatible": False, "error": f"Profile '{profile_name}' not found."}

    issues = []
    warnings = []
    alternatives = []

    # KVM check
    if profile.get("kvm", True) and not caps["kvm_available"]:
        issues.append("KVM not available — VM will be very slow (software emulation only). Enable VT-x/AMD-V in BIOS.")

    # Architecture check
    arch = profile.get("machine_arch", "x86_64")
    if arch == "aarch64" and not caps["qemu_arm_installed"]:
        issues.append("qemu-system-aarch64 not installed. Run: sudo apt install qemu-system-arm")
        alternatives.append("raspberry_pi_4 requires qemu-system-aarch64. Install it or use a minimal x86 Linux VM instead.")

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
    host_mb = caps.get("host_memory_mb", 0)
    if host_mb > 0 and requested_mb > host_mb * 0.8:
        warnings.append(
            f"Profile requests {requested_mb}MB RAM but host only has {host_mb}MB. "
            f"Consider reducing memory_mb to {host_mb // 2}MB."
        )

    # CPU core check
    requested_cores = int(profile.get("cpu_cores", 2))
    host_cores = caps.get("host_cpu_cores", 1)
    if requested_cores > host_cores:
        warnings.append(
            f"Profile requests {requested_cores} cores but host only has {host_cores}. "
            f"QEMU will over-commit — may cause slowdowns."
        )

    # Disk space check
    free_gb = caps.get("home_free_gb", 0)
    if free_gb < 20:
        warnings.append(f"Low disk space: only {free_gb}GB free in home directory.")

    compatible = len(issues) == 0
    return {
        "profile": profile_name,
        "compatible": compatible,
        "issues": issues,
        "warnings": warnings,
        "alternatives": alternatives,
        "host_summary": {
            "cpu": caps.get("host_cpu", "unknown"),
            "cores": caps.get("host_cpu_cores"),
            "memory_mb": caps.get("host_memory_mb"),
            "kvm": caps.get("kvm_available"),
            "ovmf": OVMF["available"],
            "qemu_arm": caps.get("qemu_arm_installed"),
            "arch": caps.get("host_arch"),
        },
        "notes": profile.get("_notes", ""),
    }


def apply_os_hints(config: MachineConfig) -> MachineConfig:
    os_type = config.os_type.lower()
    if "windows" in os_type or os_type == "windows":
        config.cpu_features += [
            "hv_relaxed", "hv_spinlocks=0x1fff", "hv_vapic",
            "hv_time", "hv_vendor_id=GenuineIntel",
            "hv_synic", "hv_stimer", "hv_vpindex",
        ]
        config.hpet = False
        if config.rtc_clock == "host":
            config.rtc_clock = "localtime"
        config.tsc_deadline = True
    elif "linux" in os_type:
        config.kvm_pv_features = True
    elif "macos" in os_type:
        config.cpu_features += ["-hypervisor", "+invtsc", "vendor=GenuineIntel"]
        config.machine_type = "q35"
    return config

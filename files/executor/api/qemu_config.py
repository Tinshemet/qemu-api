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

from ._vm_constants import VM_BASE_DIR
# OVMF detection + host caps and the device dataclasses were split out; re-exported here so
# existing `from .qemu_config import OVMF / DiskConfig / …` importers keep working, and used
# by MachineConfig below.
from .qemu_host_probe import OVMF, BIOS_OPTIONS, detect_ovmf, check_system_capabilities
from ._qemu_device_config import DiskConfig, NetworkConfig

_CFG  = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_DIRS = _CFG["dirs"]
_MC   = _CFG["machine_config_defaults"]
_DC   = _CFG["disk_config_defaults"]
_NC   = _CFG["network_config_defaults"]



# ─────────────────────────────────────────────
#  ENUMS / CONSTANTS
# ─────────────────────────────────────────────

MACHINE_TYPES:   Dict[str, str]           = _CFG["machine_types"]
CPU_PRESETS:     Dict[str, str]           = _CFG["cpu_presets"]
GPU_PRESETS:     Dict[str, Optional[str]] = _CFG["gpu_presets"]
AUDIO_PRESETS:   Dict[str, Optional[str]] = _CFG["audio_presets"]
NETWORK_MODELS:  List[str]                = _CFG["network_models"]


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
    gpu_passthrough_pci: str       = ""   # host PCI addr (e.g. "01:00.0") for vfio-pci passthrough
    display:         str           = _MC["display"]
    vnc_port:        Optional[int] = None
    vnc_bind_local:  bool          = False   # True → bind to 127.0.0.1 + require password (remote mode); forced True whenever hardened (see __post_init__)
    spice_port:      Optional[int] = None
    opengl:          bool          = _MC["opengl"]
    resolution:      str           = _MC["resolution"]
    audio:           str           = _MC["audio"]
    disks:           List[DiskConfig]    = field(default_factory=list)
    networks:        List[NetworkConfig] = field(default_factory=list)
    os_type:         str           = _MC["os_type"]
    os_name:         str           = ""
    iso_path:        Optional[str] = None
    unattended:      bool          = False   # Windows: attach autounattend.xml CD (opt-in)
    unattended_username: str       = ""
    unattended_password: str       = ""
    unattended_locale:   str       = ""
    unattended_autologon: bool     = True
    unattended_skip_user: bool     = False  # automate everything except account creation
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
    labels:          List[str]     = field(default_factory=list)  # user-defined tags (work_vm, test_vm, …)
    template:        Optional[str] = None  # golden-image name under ~/.gorgon/_templates/ to clone disks from
    randomize_root_password: bool = False  # offline-edit the cloned disk's root password (Linux templates only)
    root_password:   Optional[str] = None  # set when randomize_root_password succeeds — the ONLY record of it
    randomize_user_password: bool = False  # offline-edit the cloned disk's primary user's password too
    user_password:   Optional[str] = None  # set when randomize_user_password succeeds
    randomized_username: Optional[str] = None  # which account user_password applies to (auto-detected)
    new_username:    Optional[str] = None  # rename the cloned disk's primary user to this (Linux templates only)
    randomize_hostname: bool = False  # offline-edit the cloned disk's OS-level hostname/computer name
    new_hostname:    Optional[str] = None  # set when randomize_hostname succeeds (auto-generated if not given)
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
    # Guest agent — in-VM command channel (opt-in; off by default). Non-stealth VMs use
    # the standard qemu-guest-agent over virtio-serial (qga_socket); stealth VMs use a
    # dedicated serial-console port instead (serial_agent_socket) to avoid the virtio tell.
    guest_agent:     bool          = False
    qga_socket:      str           = field(default="", repr=False)
    qga_tcp_port:    int           = field(default=0)
    serial_agent_socket: str       = field(default="", repr=False)
    serial_agent_tcp_port: int     = field(default=0)
    guest_agent_psk: str           = field(default="", repr=False)  # per-VM PSK for the stealth serial channel

    # Coerces int fields and auto-falls back to SeaBIOS if OVMF is absent.
    # In: self (post-construction) → Out: nothing (self-mutation)
    def __post_init__(self) -> None:
        # Coerce types that AI may send as strings
        self.cpu_cores   = int(self.cpu_cores)
        self.cpu_threads = int(self.cpu_threads)
        self.memory_mb   = int(self.memory_mb)
        if self.stealth:
            self.hardened = True   # stealth implies hardened
        if self.hardened:
            # Hardened/stealth VMs never get an open, unauthenticated VNC — force
            # this unconditionally (not just when display=="vnc" right now) since
            # launch_vm's per-call display override happens after __post_init__
            # and would otherwise bypass a display-gated check here. Harmless
            # when display isn't vnc: both consumers (qemu_arg_builder, the
            # post-launch QMP set_password step) already gate on display=="vnc".
            self.vnc_bind_local = True
        if self.bios in ("ovmf", "ovmf_ms"):
            if OVMF["available"]:
                self.uefi = True   # bios=ovmf always implies uefi=True
            else:
                self.bios = "seabios"
                self.uefi = False

    # Returns the VM's directory path (<VM_BASE_DIR>/<name>).
    # In: nothing → Out: str
    def get_vm_dir(self) -> str:
        """Return this VM's state directory under the VM base dir."""
        return os.path.join(VM_BASE_DIR, self.name)

    # Returns the path to the VM's config.json.
    # In: nothing → Out: str
    def get_config_path(self) -> str:
        """Return the path to this VM's config.json."""
        return os.path.join(self.get_vm_dir(), "config.json")

    # Returns the QMP socket address — Unix path on Linux/macOS, tcp:host:port on Windows.
    # In: nothing → Out: str
    def get_qmp_socket(self) -> str:
        """Return the QMP socket path (or TCP address on Windows)."""
        if sys.platform == "win32" and self.qmp_tcp_port:
            return f"tcp:127.0.0.1:{self.qmp_tcp_port}"
        return os.path.join(self.get_vm_dir(), "qmp.sock")

    # Returns the monitor socket address — Unix path on Linux/macOS, tcp:host:port on Windows.
    # In: nothing → Out: str
    def get_monitor_socket(self) -> str:
        """Return the HMP monitor socket path (or TCP address on Windows)."""
        if sys.platform == "win32" and self.monitor_tcp_port:
            return f"tcp:127.0.0.1:{self.monitor_tcp_port}"
        return os.path.join(self.get_vm_dir(), "monitor.sock")

    # Returns the qemu-guest-agent socket — Unix path on Linux/macOS, tcp:host:port on Windows.
    # In: nothing → Out: str
    def get_qga_socket(self) -> str:
        """Return the guest-agent (QGA) socket path (or TCP address on Windows)."""
        if sys.platform == "win32" and self.qga_tcp_port:
            return f"tcp:127.0.0.1:{self.qga_tcp_port}"
        return os.path.join(self.get_vm_dir(), "qga.sock")

    # Returns the stealth serial-agent socket path (Unix; the second COM port's chardev).
    # In: nothing → Out: str
    def get_serial_agent_socket(self) -> str:
        """Return the stealth serial-agent chardev socket path (or TCP address on Windows)."""
        if sys.platform == "win32" and self.serial_agent_tcp_port:
            return f"tcp:127.0.0.1:{self.serial_agent_tcp_port}"
        return os.path.join(self.get_vm_dir(), "serial_agent.sock")

    # Serializes the config to a dict, stripping runtime-only fields (pid, sockets).
    # In: nothing → Out: dict
    def to_dict(self) -> Dict[str, Any]:
        """Return this config as a JSON-serialisable dict."""
        d = asdict(self)
        d.pop("pid", None)
        d.pop("monitor_socket", None)
        d.pop("qmp_socket", None)
        d.pop("qga_socket", None)
        d.pop("serial_agent_socket", None)
        return d

    # Writes the config to ~/.gorgon/<name>/config.json.
    # In: nothing → Out: nothing
    def save(self) -> None:
        """Write this config to the VM's config.json."""
        os.makedirs(self.get_vm_dir(), exist_ok=True)
        with open(self.get_config_path(), "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    # Loads and deserializes a VM config from disk by name.
    # In: str name → Out: MachineConfig
    @classmethod
    def load(cls, name: str) -> "MachineConfig":
        """Load a MachineConfig from a VM's saved config.json."""
        path = os.path.join(VM_BASE_DIR, name, "config.json")
        if not os.path.exists(path):
            raise FileNotFoundError(f"No VM config found for '{name}'")
        with open(path) as f:
            data = json.load(f)
        data["disks"]    = [DiskConfig(**d) for d in data.get("disks", [])]
        data["networks"] = [NetworkConfig(**n) for n in data.get("networks", [])]
        return cls(**data)


# ─────────────────────────────────────────────
#  CUSTOM PROFILE STORAGE
#  Profiles are saved to ~/.gorgon/_profiles/
# ─────────────────────────────────────────────

PROFILES_DIR = os.path.expanduser(_DIRS["profiles"])

from .profiles import (  # profile management (extracted from this file)
    _load_custom_profiles, save_custom_profile, delete_custom_profile,
    get_all_profiles, apply_profile, list_profiles,
    check_profile_compatibility, apply_os_hints,
)

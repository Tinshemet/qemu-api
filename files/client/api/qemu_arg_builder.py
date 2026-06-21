"""
qemu_arg_builder.py — QEMU Argument Building Layer

Translates a MachineConfig into the full QEMU command-line argument
list. Also owns: QEMU version detection, port pool helpers, and the
ISO search-directory scanner.
"""

import glob as _glob
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
from typing import List, Tuple

from .qemu_config import (
    AUDIO_PRESETS, BIOS_OPTIONS, CPU_PRESETS, GPU_PRESETS, MachineConfig, NetworkConfig, OVMF,
)

_CFG      = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_PORTS    = _CFG["ports"]
_TIMEOUTS = _CFG["timeouts"]

# ── QEMU Version Detection ─────────────────────────────────────────────────────

# Runs qemu --version and parses the version as a (major, minor, patch) triple.
# In: str binary → Out: (int, int, int), returns (0, 0, 0) on failure
def _parse_qemu_version(binary: str = "qemu-system-x86_64") -> Tuple[int, int, int]:
    """Return (major, minor, patch). Returns (0, 0, 0) if detection fails."""
    try:
        r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=_TIMEOUTS["qemu_version"])
        m = re.search(r"version (\d+)[.](\d+)[.](\d+)", r.stdout)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
    except Exception:
        pass
    return (0, 0, 0)


QEMU_VERSION: Tuple[int, int, int] = _parse_qemu_version()


# Prints a Rich warning panel for any known issues with the detected QEMU version.
# In: nothing → Out: nothing (console output)
def _qemu_version_warn() -> None:
    """Print a Rich warning panel for known version-specific issues."""
    major, minor, patch = QEMU_VERSION
    ver_str = f"{major}.{minor}.{patch}" if QEMU_VERSION != (0, 0, 0) else "unknown"
    warnings = []

    if QEMU_VERSION == (0, 0, 0):
        warnings.append("QEMU version could not be detected — some features may not work")
    if major >= 7:
        warnings.append(
            f"QEMU {ver_str}: 'vgamem_mb' property removed — "
            "virtio-vga-gl will be used without memory size arg (handled automatically)"
        )
    if major >= 6:
        warnings.append(
            f"QEMU {ver_str}: '-accel kvm' conflicts with '-machine accel=kvm' "
            "— using -enable-kvm only (handled automatically)"
        )
    if major >= 7:
        warnings.append(
            f"QEMU {ver_str}: PulseAudio backend may need pipewire-pulse — "
            "falling back to 'none' if pa fails"
        )

    if warnings:
        from rich.console import Console as _Con
        from rich.panel   import Panel   as _Pan
        _c = _Con()
        body = "\n".join(f"  [yellow]warn[/yellow] {w}" for w in warnings)
        _c.print(_Pan(
            body,
            title=f"[bold yellow]QEMU {ver_str} Compatibility Notes[/bold yellow]",
            border_style="yellow",
        ))


# ── Port Pool (auto-assign VNC / SPICE ports) ─────────────────────────────────

VNC_PORT_START   = _PORTS["vnc_start"]
SPICE_PORT_START = _PORTS["spice_start"]
PORT_RANGE       = _PORTS["port_range"]


# Checks if a TCP port on localhost is currently available.
# In: int port → Out: bool
def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


# Finds the first free port starting from start that is not in the used list.
# In: int start, List[int] used → Out: int
def _next_free_port(start: int, used: List[int]) -> int:
    for p in range(start, start + PORT_RANGE):
        if p not in used and _port_free(p):
            return p
    raise RuntimeError(f"No free port found starting from {start}")


# ── ISO Search Directory Scanner ───────────────────────────────────────────────

# Builds a list of directories to search for ISO files based on common home subdirectories.
# In: nothing → Out: List[str]
_ISO_DESKTOP_SUBDIRS = set(_CFG["iso_desktop_subdirs"])
_ISO_HOME_SUBDIRS    = _CFG["iso_home_subdirs"]


def _build_iso_search_dirs() -> List[str]:
    """Build ISO search dirs dynamically — handles capital/lowercase variants."""
    home = os.path.expanduser("~")
    dirs: List[str] = []

    # Home subdirectories and one level deep inside them for named ISO folders
    for sub in _ISO_HOME_SUBDIRS:
        p = os.path.join(home, sub)
        if not os.path.isdir(p):
            continue
        if p not in dirs:
            dirs.append(p)
        try:
            for entry in os.listdir(p):
                full = os.path.join(p, entry)
                if os.path.isdir(full) and entry.lower() in _ISO_DESKTOP_SUBDIRS and full not in dirs:
                    dirs.append(full)
        except PermissionError:
            pass

    # System-wide mount points: /media/<user>/<device>, /mnt/<device>, /run/media/<user>/<device>
    for mount_root in _CFG.get("iso_mount_roots", []):
        if not os.path.isdir(mount_root):
            continue
        try:
            for top in sorted(os.listdir(mount_root)):
                top_path = os.path.join(mount_root, top)
                if not os.path.isdir(top_path):
                    continue
                # /media/<user>/<device> layout — descend one more level
                try:
                    children = [os.path.join(top_path, c) for c in os.listdir(top_path)
                                if os.path.isdir(os.path.join(top_path, c))]
                except PermissionError:
                    children = []
                targets = children if children else [top_path]
                for t in targets:
                    if t not in dirs:
                        dirs.append(t)
        except PermissionError:
            pass

    dirs.append(tempfile.gettempdir())
    return dirs


ISO_SEARCH_DIRS = _build_iso_search_dirs()


# ── QEMU Argument Builder ──────────────────────────────────────────────────────

class QemuArgBuilder:
    # Stores the config and precomputes ARM/raspi detection flags.
    # In: MachineConfig → Out: nothing
    def __init__(self, config: MachineConfig):
        self.cfg      = config
        self.vm_dir   = config.get_vm_dir()
        self.args:    List[str] = []
        self.qemu_ver = QEMU_VERSION
        self.is_arm   = config.machine_arch in ("aarch64", "arm", "armhf")
        self.is_raspi = "raspi" in config.machine_type.lower()

    # Orchestrates all _* sub-methods and returns the complete QEMU command list.
    # In: nothing → Out: List[str]
    def build(self) -> List[str]:
        self.args = [self.cfg.qemu_binary]
        self._base()
        if self.cfg.hardened and not self.is_arm:
            self._harden()
        self._machine()
        self._cpu()
        self._memory()
        if not self.is_raspi:
            self._firmware()   # raspi has its own ROM, no pflash
        self._smbios()
        self._disks()
        self._network()
        self._display()
        self._audio()
        if not self.is_raspi:
            self._usb()        # raspi machine handles USB itself
        self._battery()
        self._kernel_direct()
        self._qmp()
        self._monitor()
        self._serial()
        if not self.is_arm:
            self._misc()       # virtio-rng not needed on ARM
        if self.cfg.tpm and not self.is_arm:
            self._tpm()
        self.args += self.cfg.extra_args
        return [a for a in self.args if a]

    # Adds -name and -enable-kvm (disabled for ARM).
    # In: nothing → Out: appends to self.args
    def _base(self):
        self.args += ["-name", f"{self.cfg.name},process={self.cfg.name}"]
        # -enable-kvm enables KVM; accel=kvm is set in _machine() — do NOT add -accel kvm here
        if self.cfg.kvm and not self.is_arm:
            self.args += ["-enable-kvm"]
        if self.is_arm:
            self.cfg.kvm = False  # KVM never works for ARM guests on x86 host

    # Hardens the VM against guest escape and fingerprinting. Mutates cfg before
    # other builders run so their output already reflects the hardened state.
    # In: nothing → Out: mutates self.cfg + appends seccomp args
    def _harden(self):
        # Hide hypervisor CPUID bit and KVM paravirt leaves — keeps KVM perf,
        # removes the flag that tells the guest it's inside a hypervisor.
        # -vmx: remove VMX flag so kvm_intel.ko cannot load inside the guest
        # (if the guest sees vmx, it loads kvm_intel, which lsmod exposes to inxi).
        for flag in ("-hypervisor", "kvm=off", "-vmx"):
            if flag not in self.cfg.cpu_features:
                self.cfg.cpu_features.append(flag)
        # cpu=host already inherits all host mitigations (ssbd, ibrs, md-clear,
        # etc.) so don't force-add them — KVM rejects flags the host doesn't
        # actually expose (e.g. spec-ctrl on Enhanced IBRS CPUs).
        # Disable memory balloon — it can leak timing information between tenants.
        self.cfg.balloon = False
        # Disable hugepages in hardened mode — cross-tenant side-channel risk.
        self.cfg.hugepages = False
        # Force NAT for hardened VMs — prevents guest from seeing or attacking
        # the LAN. Exception: stealth VMs use bridge intentionally so they get
        # a real LAN IP and don't expose the 10.0.2.x QEMU NAT subnet.
        if not self.cfg.stealth:
            for net in self.cfg.networks:
                if net.mode == "bridge":
                    net.mode = "nat"
        # QEMU seccomp sandbox — prevents the QEMU process itself from making
        # dangerous syscalls even if the guest achieves code execution in QEMU.
        self.args += [
            "-sandbox",
            "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
        ]

    # Adds -machine with KVM/IOMMU accelerators and -rtc.
    # In: nothing → Out: appends to self.args
    def _machine(self):
        machine_str = self.cfg.machine_type
        extras = []
        if self.cfg.kvm and not self.is_arm:
            extras.append("accel=kvm")
        if self.cfg.iommu and not self.is_arm:
            extras.append("kernel_irqchip=on")
        # Override ACPI OEM ID (defaults to "BOCHS  ") to match the declared
        # manufacturer — inxi and systemd-detect-virt can read ACPI table headers.
        if self.cfg.manufacturer and not self.is_arm:
            oem_id = self.cfg.manufacturer[:6].ljust(6)
            extras.append(f"x-oem-id={oem_id}")
            if self.cfg.product_name:
                oem_table = (self.cfg.product_name.replace(" ", ""))[:8]
                extras.append(f"x-oem-table-id={oem_table}")
        if extras:
            machine_str += "," + ",".join(extras)
        self.args += ["-machine", machine_str]
        if self.cfg.hpet and not self.is_arm:
            self.args += ["-device", "hpet"]
        if not self.is_raspi:
            # 'host' is NOT a valid -rtc base value — use 'utc' instead
            rtc_base = self.cfg.rtc_clock
            if rtc_base not in ("utc", "localtime") and not rtc_base[0].isdigit():
                rtc_base = _CFG["machine_config_defaults"]["rtc_base_fallback"]
            self.args += ["-rtc", f"base={rtc_base},driftfix=slew"]

    # Adds -cpu (with features) and -smp topology.
    # In: nothing → Out: appends to self.args
    def _cpu(self):
        cpu_str  = CPU_PRESETS.get(self.cfg.cpu_model, self.cfg.cpu_model)
        cpu_name = cpu_str.replace("-cpu ", "").split(",")[0]
        features = list(self.cfg.cpu_features)
        if self.cfg.kvm_pv_features and not self.is_arm and "kvm=off" not in features:
            features.append("+kvm_pv_unhalt")
        feature_str = "".join(f",{f}" for f in features)
        self.args += ["-cpu", f"{cpu_name}{feature_str}"]
        self.args += [
            "-smp",
            f"cores={self.cfg.cpu_cores},"
            f"threads={self.cfg.cpu_threads},"
            f"sockets={self.cfg.cpu_sockets},"
            f"maxcpus={self.cfg.cpu_cores * self.cfg.cpu_threads * self.cfg.cpu_sockets}",
        ]

    # Adds -m, hugepages path, and balloon device.
    # In: nothing → Out: appends to self.args
    def _memory(self):
        self.args += ["-m", str(self.cfg.memory_mb)]
        if self.cfg.hugepages and not self.is_arm:
            self.args += ["-mem-path", self.cfg.hugepages_path, "-mem-prealloc"]
        if self.cfg.balloon and not self.is_raspi:
            self.args += ["-device", "virtio-balloon-pci"]

    # Adds OVMF code + vars as pflash drives; skipped on ARM.
    # In: nothing → Out: appends to self.args
    def _firmware(self):
        if self.is_arm:
            return
        bios_path = BIOS_OPTIONS.get(self.cfg.bios) or OVMF.get("code")
        if bios_path and os.path.exists(bios_path):
            self.args += ["-drive", f"if=pflash,format=raw,readonly=on,file={bios_path}"]
            vars_path = self.cfg.uefi_vars
            if not vars_path or not os.path.exists(vars_path):
                for candidate in [
                    os.path.join(self.vm_dir, "OVMF_VARS.fd"),
                    os.path.join(self.vm_dir, "OVMF_VARS_4M.fd"),
                ]:
                    if os.path.exists(candidate):
                        vars_path = candidate
                        break
            if vars_path and os.path.exists(vars_path):
                self.args += ["-drive", f"if=pflash,format=raw,file={vars_path}"]

    # Adds -smbios type=0 (BIOS), type=1 (system), type=3 (chassis); skipped on ARM.
    # In: nothing → Out: appends to self.args
    def _chassis_type_byte(self) -> int:
        mapping = {
            "notebook": 9, "laptop": 9, "portable": 8,
            "desktop": 3, "server": 17, "tower": 7, "tablet": 30,
        }
        guess = mapping.get((self.cfg.smbios_type or "").lower(), 0)
        if not guess:
            guess = mapping.get((self.cfg.machine_class or "").lower(), 3)
        return guess

    def _write_smbios_chassis_bin(self, chassis_type: int) -> str:
        """Write a raw SMBIOS type=3 structure with the given chassis_type byte.

        QEMU appends -smbios file= entries after its built-in structures.
        Linux dmi_scan overwrites dmi_chassis_type for every type=3 hit, so
        our appended entry overrides QEMU's default chassis_type=1 (Other).
        Returns the file path, or '' on failure.
        """
        if self.is_arm or not chassis_type:
            return ''
        mfr = self.cfg.manufacturer or ''
        mfr_idx = 1 if mfr else 0
        # SMBIOS type=3 header: type, length, handle, then 9 field bytes
        header = struct.pack('<BBHBBBBBBBBB',
            3, 0x0D, 0x0301,          # type, length=13, handle (unique from built-in)
            mfr_idx, chassis_type,    # manufacturer string-index, chassis_type byte
            0, 0, 0,                  # version, serial, asset (no strings)
            3, 3, 3, 3,               # boot-up, psu, thermal, security states = Safe
        )
        strings = (mfr.encode('ascii', errors='replace') + b'\x00') if mfr else b''
        strings += b'\x00'  # end-of-strings marker
        blob = header + strings

        try:
            vm_dir = os.path.expanduser(f"~/.qemu_vms/{self.cfg.name}")
            os.makedirs(vm_dir, exist_ok=True)
            path = os.path.join(vm_dir, 'smbios_chassis.bin')
            with open(path, 'wb') as f:
                f.write(blob)
            return path
        except OSError:
            return ''

    def _smbios(self):
        if self.is_arm:
            return
        if self.cfg.manufacturer or self.cfg.product_name:
            parts = ["type=1"]
            if self.cfg.manufacturer:  parts.append(f"manufacturer={self.cfg.manufacturer}")
            if self.cfg.product_name:  parts.append(f"product={self.cfg.product_name}")
            if self.cfg.serial_number: parts.append(f"serial={self.cfg.serial_number}")
            if self.cfg.hostname:      parts.append(f"family={self.cfg.hostname}")
            self.args += ["-smbios", ",".join(parts)]
        if self.cfg.bios_vendor or self.cfg.bios_version:
            parts = ["type=0"]
            if self.cfg.bios_vendor:  parts.append(f"vendor={self.cfg.bios_vendor}")
            if self.cfg.bios_version: parts.append(f"version={self.cfg.bios_version}")
            self.args += ["-smbios", ",".join(parts)]
        # type=2 (baseboard): override board_vendor/board_name which default to
        # "QEMU" and "Standard PC (Q35+ICH9)" — inxi reads these via DMI and
        # uses them to identify KVM even when CPUID is hidden.
        board_vendor  = self.cfg.manufacturer
        board_product = self.cfg.board_product or self.cfg.product_name
        if board_vendor or board_product:
            parts = ["type=2"]
            if board_vendor:  parts.append(f"manufacturer={board_vendor}")
            if board_product: parts.append(f"product={board_product}")
            self.args += ["-smbios", ",".join(parts)]
        # type=3 (chassis): override chassis_vendor which defaults to "QEMU".
        # chassis_type byte is NOT settable via -smbios CLI in QEMU 8.x, so we
        # inject a raw SMBIOS type=3 binary. QEMU appends -smbios file= entries
        # AFTER its built-in structures; the Linux DMI scanner overwrites
        # dmi_chassis_type on each type=3 hit, so the last entry (ours) wins.
        chassis_type = self._chassis_type_byte()
        chassis_bin  = self._write_smbios_chassis_bin(chassis_type)
        if chassis_bin:
            # Binary already includes manufacturer; QEMU rejects both file= and
            # type=3 CLI for the same structure type simultaneously.
            self.args += ["-smbios", f"file={chassis_bin}"]
        elif self.cfg.manufacturer:
            self.args += ["-smbios", f"type=3,manufacturer={self.cfg.manufacturer}"]

    # Adds disk drives (SD card for raspi, virtio/NVMe/SCSI/IDE for x86) and ISO cdrom.
    # In: nothing → Out: appends to self.args
    def _disks(self):
        if self.is_raspi:
            # raspi3b ONLY accepts SD card interface
            for disk in self.cfg.disks:
                disk_path = os.path.expanduser(disk.path)
                self.args += ["-drive", f"file={disk_path},format={disk.format},if=sd,index=0"]
            return

        has_scsi = any(d.bus == "scsi" for d in self.cfg.disks)
        has_sata = any(d.bus == "sata" for d in self.cfg.disks)
        if has_scsi:
            self.args += ["-device", "virtio-scsi-pci,id=scsi0"]
        if has_sata:
            self.args += ["-device", "ich9-ahci,id=ahci"]
        for i, disk in enumerate(self.cfg.disks):
            self.args += disk.to_qemu_args(i)
        if self.cfg.iso_path:
            self.args += [
                "-drive",  f"file={self.cfg.iso_path},if=none,id=cdrom0,readonly=on,media=cdrom",
                "-device", "ide-cd,drive=cdrom0,bootindex=1,model=HL-DT-ST DVDRAM GU90N",
            ]
            if not self.cfg.uefi:
                # Legacy BIOS only — UEFI uses bootindex and ignores -boot order
                self.args += ["-boot", f"order={self.cfg.boot_order},menu=on"]
        else:
            if not self.cfg.uefi:
                self.args += ["-boot", "order=c,menu=on"]

    # Adds network args from each NetworkConfig, or a default user NAT if none defined.
    # In: nothing → Out: appends to self.args
    def _network(self):
        if not self.cfg.networks:
            net = NetworkConfig(manufacturer_hint=self.cfg.manufacturer)
            self.args += net.to_qemu_args()
            return
        for net in self.cfg.networks:
            self.args += net.to_qemu_args()

    # Probes QEMU with gl=on to check if virgl/OpenGL is actually usable on this host.
    # In: nothing → Out: bool
    def _gl_available(self) -> bool:
        """Check if virgl/OpenGL is actually usable before passing gl=on."""
        try:
            r = subprocess.run(
                [self.cfg.qemu_binary, "-display", "sdl,gl=on",
                 "-machine", "none", "-no-user-config"],
                capture_output=True, text=True, timeout=_TIMEOUTS["gl_check"],
            )
            err = (r.stderr or "").lower()
            return "gl" not in err and "opengl" not in err
        except Exception:
            return False

    # Adds display args for SDL/GTK/SPICE/VNC or -nographic; downgrades GPU if GL unavailable.
    # In: nothing → Out: appends to self.args
    def _display(self):
        if self.is_raspi:
            self.args += ["-nographic"]  # raspi3b has NO video output in QEMU
            return
        gpu_device = GPU_PRESETS.get(self.cfg.gpu)
        if self.cfg.display == "none":
            self.args += ["-nographic"]
            return
        if self.cfg.gpu == "none":
            # Linux stealth: vmware-svga loads vmwgfx (no "qemu" in module name).
            # Windows stealth: std VGA — no VMware driver needed, avoids "VMware SVGA"
            #   showing up in Device Manager before any driver install.
            # Non-stealth: cirrus-vga (loads cirrus_qemu, reveals hypervisor via lsmod).
            # Bochs VGA (QEMU default) uses PCI ID 1234:1111 which inxi flags as QEMU.
            if self.cfg.stealth:
                device = "VGA" if self.cfg.os_type == "windows" else "vmware-svga"
            else:
                device = "cirrus-vga"
            self.args += ["-device", device]

        gl_wanted = self.cfg.opengl and not self.is_arm
        gl_ok     = gl_wanted and self._gl_available()
        gl_flag   = "gl=on" if gl_ok else "gl=off"

        # If gl=on was wanted but unavailable, also downgrade virtio-vga-gl → virtio-vga
        if gl_wanted and not gl_ok and self.cfg.gpu == "virtio":
            gpu_device = "virtio-vga"

        if self.cfg.display == "sdl":
            self.args += ["-display", f"sdl,{gl_flag}"]
        elif self.cfg.display == "gtk":
            self.args += ["-display", f"gtk,{gl_flag}"]
        elif self.cfg.display == "spice":
            port = self.cfg.spice_port or SPICE_PORT_START
            self.args += [
                "-spice",   f"port={port},disable-ticketing=on",
                "-device",  "virtio-serial",
                "-chardev", "spicevmc,id=vdagent,debug=0,name=vdagent",
                "-device",  "virtserialport,chardev=vdagent,name=com.redhat.spice.0",
                "-display", "spice-app",
            ]
        elif self.cfg.display == "vnc":
            port        = self.cfg.vnc_port or VNC_PORT_START
            display_num = port - 5900
            if self.cfg.vnc_bind_local:
                # Remote mode: bind to localhost only + require password (set via QMP after boot).
                self.args += ["-vnc", f"127.0.0.1:{display_num},password=on"]
            else:
                self.args += ["-vnc", f":{display_num}"]

        if gpu_device and not self.is_raspi:
            if gpu_device == "virtio-vga-gl":
                self.args += ["-device", "virtio-vga-gl,xres=1920,yres=1080"]
            elif gpu_device == "vfio-pci":
                pci = getattr(self.cfg, "_vfio_pci", "0000:01:00.0")
                self.args += ["-device", f"vfio-pci,host={pci}"]
            else:
                # vgamem_mb removed in QEMU 7+ — don't pass it
                self.args += ["-device", gpu_device]

    # Detects the platform audio server and adds the appropriate -audiodev + -device.
    # Linux: PulseAudio or PipeWire. macOS: CoreAudio. Windows: DirectSound.
    # In: nothing → Out: appends to self.args
    def _audio(self):
        if self.is_raspi:
            return
        audio_dev = AUDIO_PRESETS.get(self.cfg.audio)
        if not audio_dev:
            return

        if sys.platform == "linux":
            _tmp = tempfile.gettempdir()
            pa_running = bool(
                _glob.glob("/run/user/*/pulse/native") or
                _glob.glob(os.path.join(_tmp, "pulse-*", "native"))
            )
            pw_running = bool(_glob.glob("/run/user/*/pipewire-0"))
            if pa_running:
                audiodev = "pa,id=audio0"
            elif pw_running:
                audiodev = "pipewire,id=audio0"
            else:
                return  # no audio server — skip to avoid crash
        elif sys.platform == "darwin":
            audiodev = "coreaudio,id=audio0"
        elif sys.platform == "win32":
            audiodev = "dsound,id=audio0"
        else:
            return

        self.args += ["-audiodev", audiodev, "-device", audio_dev]
        if self.cfg.audio in ("hda", "ich9"):
            self.args += ["-device", "hda-duplex,audiodev=audio0"]

    # Adds xHCI controller, keyboard, and tablet/mouse device.
    # In: nothing → Out: appends to self.args
    def _usb(self):
        # nec-usb-xhci: NEC uPD720200 USB 3.0 (PCI 1033:0194) — real chip PCI IDs.
        # qemu-xhci uses 1b36 (Red Hat/QEMU) which inxi detects as virtual.
        self.args += ["-device", "nec-usb-xhci,id=usb", "-device", "usb-kbd"]
        self.args += ["-device", "usb-tablet" if self.cfg.tablet else "usb-mouse"]

    def _battery(self):
        pass  # acpi-battery is not a valid QEMU device; battery via ACPI tables is not yet implemented

    # Adds -kernel, -initrd, -append for direct kernel boot if paths are set.
    # In: nothing → Out: appends to self.args
    def _kernel_direct(self):
        if self.cfg.kernel_path:    self.args += ["-kernel", self.cfg.kernel_path]
        if self.cfg.initrd_path:    self.args += ["-initrd", self.cfg.initrd_path]
        if self.cfg.kernel_cmdline: self.args += ["-append", self.cfg.kernel_cmdline]

    # Creates the QMP socket and adds its -chardev/-mon args.
    # Uses Unix domain sockets on Linux/macOS; TCP on Windows.
    # In: nothing → Out: appends to self.args
    def _qmp(self):
        if sys.platform == "win32":
            port = self.cfg.qmp_tcp_port or _next_free_port(
                _CFG["ports"].get("qmp_port_start", 9000), []
            )
            self.cfg.qmp_tcp_port = port
            self.cfg.qmp_socket   = f"tcp:127.0.0.1:{port}"
            self.args += [
                "-chardev", f"socket,id=qmp,host=127.0.0.1,port={port},server=on,wait=off",
                "-mon",     "chardev=qmp,mode=control,pretty=off",
            ]
        else:
            sock = os.path.join(self.vm_dir, "qmp.sock")
            self.cfg.qmp_socket = sock
            self.args += [
                "-chardev", f"socket,id=qmp,path={sock},server=on,wait=off",
                "-mon",     "chardev=qmp,mode=control,pretty=off",
            ]

    # Creates the human monitor socket and adds its -chardev/-mon args.
    # Uses Unix domain sockets on Linux/macOS; TCP on Windows.
    # In: nothing → Out: appends to self.args
    def _monitor(self):
        if sys.platform == "win32":
            port = self.cfg.monitor_tcp_port or _next_free_port(
                _CFG["ports"].get("monitor_port_start", 9100), []
            )
            self.cfg.monitor_tcp_port = port
            self.cfg.monitor_socket   = f"tcp:127.0.0.1:{port}"
            self.args += [
                "-chardev", f"socket,id=mon,host=127.0.0.1,port={port},server=on,wait=off",
                "-mon",     "chardev=mon,mode=readline",
            ]
        else:
            sock = os.path.join(self.vm_dir, "monitor.sock")
            self.cfg.monitor_socket = sock
            self.args += [
                "-chardev", f"socket,id=mon,path={sock},server=on,wait=off",
                "-mon",     "chardev=mon,mode=readline",
            ]

    # Adds a serial console socket — Unix socket on Linux/macOS, TCP telnet on Windows.
    # In: nothing → Out: appends to self.args
    def _serial(self):
        if sys.platform == "win32":
            port = self.cfg.serial_tcp_port or _next_free_port(
                _CFG["ports"].get("serial_port_start", 9200), []
            )
            self.cfg.serial_tcp_port = port
            self.args += ["-serial", f"telnet:127.0.0.1:{port},server,nowait"]
        else:
            sock = os.path.join(self.vm_dir, "serial.sock")
            self.args += ["-serial", f"unix:{sock},server,nowait"]

    # Adds entropy device and -no-user-config (x86 only).
    # virtio-rng-pci uses PCI vendor 1af4 (Red Hat/QEMU) — detectable by inxi.
    # Hardened VMs skip it; non-hardened get it for performance.
    # In: nothing → Out: appends to self.args
    def _tpm(self):
        tpm_sock = os.path.join(self.cfg.get_vm_dir(), "tpm.sock")
        self.args += [
            "-chardev", f"socket,id=chrtpm,path={tpm_sock}",
            "-tpmdev",  "emulator,id=tpm0,chardev=chrtpm",
            "-device",  "tpm-tis,tpmdev=tpm0",
        ]

    def _misc(self):
        if not self.cfg.hardened:
            self.args += ["-device", "virtio-rng-pci"]
        if self.cfg.iso_path:
            self.args += ["-no-reboot"]
        self.args += ["-no-user-config"]

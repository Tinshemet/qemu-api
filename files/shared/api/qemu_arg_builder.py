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

with open(os.path.join(os.path.dirname(__file__), "config.json")) as _f:
    _CFG = json.load(_f)
_PORTS    = _CFG["ports"]
_TIMEOUTS = _CFG["timeouts"]

# ── QEMU Version Detection ─────────────────────────────────────────────────────

def _parse_qemu_version(binary: str = "qemu-system-x86_64") -> Tuple[int, int, int]:
    """Return the QEMU version as ``(major, minor, patch)``.

    Args:
        binary: QEMU binary to query (default ``qemu-system-x86_64``).

    Returns:
        Version tuple, or ``(0, 0, 0)`` if detection fails.

    Example::

        _parse_qemu_version()         # → (8, 2, 1)  on a typical Ubuntu 24.04
        _parse_qemu_version("qemu-system-aarch64")  # → (8, 2, 1)
        _parse_qemu_version("missing-binary")       # → (0, 0, 0)
    """
    try:
        r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=_TIMEOUTS["qemu_version"])
        m = re.search(r"version (\d+)[.](\d+)[.](\d+)", r.stdout)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
    except Exception:
        pass
    return (0, 0, 0)


QEMU_VERSION: Tuple[int, int, int] = _parse_qemu_version()


def qemu_version_warn() -> None:
    """Print a Rich warning panel for known version-specific issues."""
    major, minor, patch = QEMU_VERSION
    ver_str = f"{major}.{minor}.{patch}" if QEMU_VERSION != (0, 0, 0) else "unknown"
    warnings = []

    if QEMU_VERSION == (0, 0, 0):
        warnings.append("QEMU version could not be detected — some features may not work")
    if major >= 7:
        warnings.append(
            f"QEMU {ver_str}: 'vgamem_mb' property removed — "
            "virtio-vga 'vgamem_mb' property removed — resolution set via xres/yres (handled automatically)"
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


def _port_free(port: int) -> bool:
    """Check whether a localhost TCP port is available.

    Args:
        port: Port number to probe.

    Returns:
        ``True`` if nothing is listening on 127.0.0.1:port; ``False`` if
        the port is bound.

    Example::

        _port_free(5900)  # → True if no VNC server is running
        _port_free(22)    # → False on a typical Linux machine (sshd)
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def next_free_port(start: int, used: List[int]) -> int:
    """Return the first port ≥ start that is not in *used* and is not actively bound.

    Args:
        start: Lowest port number to try.
        used:  Ports already assigned to existing VMs.

    Returns:
        A free port number.

    Raises:
        RuntimeError: If no free port is found in the search range.

    Example::
        >>> next_free_port(5900, [5900, 5901])
        5902
    """
    for p in range(start, start + PORT_RANGE):
        if p not in used and _port_free(p):
            return p
    raise RuntimeError(f"No free port found starting from {start}")


# ── ISO Search Directory Scanner ───────────────────────────────────────────────

# Builds a list of directories to search for ISO files based on common home subdirectories.
# In: nothing → Out: List[str]
_ISO_DESKTOP_SUBDIRS = set(_CFG["iso_desktop_subdirs"])
_ISO_HOME_SUBDIRS    = _CFG["iso_home_subdirs"]


def build_iso_search_dirs() -> List[str]:
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


# ── QEMU Argument Builder ──────────────────────────────────────────────────────

class QemuArgBuilder:
    def __init__(self, config: MachineConfig) -> None:
        """Store the config and precompute ARM/raspi detection flags."""
        self.cfg      = config
        self.vm_dir   = config.get_vm_dir()
        self.args:    List[str] = []
        self.qemu_ver = QEMU_VERSION
        self.is_arm   = config.machine_arch in ("aarch64", "arm", "armhf")
        self.is_raspi = "raspi" in config.machine_type.lower()

    def build(self) -> List[str]:
        """Build and return the full QEMU command list for this config.

        Calls every ``_*`` sub-method in order, applies hardening and extra-arg
        filtering, and returns a clean list with empty strings stripped.

        Returns:
            The complete QEMU argv starting with the binary name.

        Example::
            >>> QemuArgBuilder(cfg).build()[0]
            'qemu-system-x86_64'
        """
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
        # Drop any extra_arg that disables the seccomp sandbox when hardened.
        # The sanitizer filters AI-supplied args, but the arg_builder is the
        # last line of defense before the QEMU command is assembled.
        extra = self.cfg.extra_args
        if self.cfg.hardened and not self.is_arm:
            extra = [a for a in extra if "-sandbox" not in a and "obsolete=allow" not in a]
        self.args += extra
        return [a for a in self.args if a]

    def _base(self) -> None:
        """Append -name and (when enabled) -enable-kvm; force kvm=False for ARM."""
        self.args += ["-name", f"{self.cfg.name},process={self.cfg.name}"]
        # -enable-kvm enables KVM; accel=kvm is set in _machine() — do NOT add -accel kvm here
        if self.cfg.kvm and not self.is_arm:
            self.args += ["-enable-kvm"]
        if self.is_arm:
            self.cfg.kvm = False  # KVM never works for ARM guests on x86 host

    def _harden(self) -> None:
        """Apply CPU masking, sandbox, and network hardening; mutates self.cfg in place."""
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

    def _machine(self) -> None:
        """Append -machine (with KVM/IOMMU/OEM overrides) and -rtc."""
        machine_str = self.cfg.machine_type
        extras = []
        if self.cfg.kvm and not self.is_arm:
            extras.append("accel=kvm")
        if self.cfg.iommu and not self.is_arm:
            extras.append("kernel_irqchip=on")
        # Override ACPI OEM ID (defaults to "BOCHS  ") to match the declared
        # manufacturer — inxi and systemd-detect-virt can read ACPI table headers.
        if self.cfg.manufacturer and not self.is_arm:
            # Commas in -machine option values inject extra directives.
            oem_id = self.cfg.manufacturer.replace(",", "")[:6].ljust(6)
            extras.append(f"x-oem-id={oem_id}")
            if self.cfg.product_name:
                oem_table = (self.cfg.product_name.replace(",", "").replace(" ", ""))[:8]
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

    def _cpu(self) -> None:
        """Append -cpu (with feature flags) and -smp topology."""
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

    def _memory(self) -> None:
        """Append -m, optional hugepages path, and the virtio balloon device."""
        self.args += ["-m", str(self.cfg.memory_mb)]
        if self.cfg.hugepages and not self.is_arm:
            self.args += ["-mem-path", self.cfg.hugepages_path, "-mem-prealloc"]
        if self.cfg.balloon and not self.is_raspi:
            self.args += ["-device", "virtio-balloon-pci"]

    def _firmware(self) -> None:
        """Append OVMF code + vars pflash drives (x86 only; no-op on ARM)."""
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
        mapping = _CFG["smbios_chassis_type_map"]
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
            os.makedirs(self.vm_dir, exist_ok=True)
            path = os.path.join(self.vm_dir, 'smbios_chassis.bin')
            with open(path, 'wb') as f:
                f.write(blob)
            return path
        except OSError:
            return ''

    @staticmethod
    def _smbios_escape(value: str) -> str:
        """Remove commas from a string value used in a -smbios option.

        In: "Dell, Inc." → Out: "Dell Inc."
        A comma in a -smbios value terminates the current field and starts a
        new key=value pair, allowing injection of arbitrary QEMU SMBIOS directives.
        """
        return value.replace(",", "")

    def _smbios(self) -> None:
        if self.is_arm:
            return
        if self.cfg.manufacturer or self.cfg.product_name:
            parts = ["type=1"]
            if self.cfg.manufacturer:  parts.append(f"manufacturer={self._smbios_escape(self.cfg.manufacturer)}")
            if self.cfg.product_name:  parts.append(f"product={self._smbios_escape(self.cfg.product_name)}")
            if self.cfg.serial_number: parts.append(f"serial={self._smbios_escape(self.cfg.serial_number)}")
            if self.cfg.hostname:      parts.append(f"family={self._smbios_escape(self.cfg.hostname)}")
            self.args += ["-smbios", ",".join(parts)]
        if self.cfg.bios_vendor or self.cfg.bios_version:
            parts = ["type=0"]
            if self.cfg.bios_vendor:  parts.append(f"vendor={self._smbios_escape(self.cfg.bios_vendor)}")
            if self.cfg.bios_version: parts.append(f"version={self._smbios_escape(self.cfg.bios_version)}")
            self.args += ["-smbios", ",".join(parts)]
        # type=2 (baseboard): override board_vendor/board_name which default to
        # "QEMU" and "Standard PC (Q35+ICH9)" — inxi reads these via DMI and
        # uses them to identify KVM even when CPUID is hidden.
        board_vendor  = self.cfg.manufacturer
        board_product = self.cfg.board_product or self.cfg.product_name
        if board_vendor or board_product:
            parts = ["type=2"]
            if board_vendor:  parts.append(f"manufacturer={self._smbios_escape(board_vendor)}")
            if board_product: parts.append(f"product={self._smbios_escape(board_product)}")
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

    def _disks(self) -> None:
        """Append disk drives (SD for raspi; virtio/NVMe/SCSI/IDE for x86) and the ISO cdrom."""
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
                "-device", f"ide-cd,drive=cdrom0,bootindex=1,model={_CFG['cdrom_model']}",
            ]
            if not self.cfg.uefi:
                # Legacy BIOS only — UEFI uses bootindex and ignores -boot order
                self.args += ["-boot", f"order={self.cfg.boot_order},menu=on"]
        else:
            if not self.cfg.uefi:
                self.args += ["-boot", "order=c,menu=on"]

    def _network(self) -> None:
        """Append network args from each NetworkConfig, falling back to default user-NAT."""
        if not self.cfg.networks:
            net = NetworkConfig(manufacturer_hint=self.cfg.manufacturer)
            self.args += net.to_qemu_args()
            return
        for net in self.cfg.networks:
            self.args += net.to_qemu_args()

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

    def _display(self) -> None:
        """Append display args (SDL/GTK/SPICE/VNC/-nographic); downgrade GPU if GL is unavailable."""
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

        # virtio-vga-gl requires GL; downgrade to virtio-vga when GL is off or unavailable
        if self.cfg.gpu == "virtio" and not gl_ok:
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

    def _audio(self) -> None:
        """Detect the platform audio server and append the matching -audiodev + -device."""
        if self.is_raspi:
            return
        audio_dev = AUDIO_PRESETS.get(self.cfg.audio)
        if not audio_dev:
            return

        if sys.platform == "linux":
            _tmp = tempfile.gettempdir()
            _ag = _CFG.get("audio_socket_globs", {})
            pa_running = bool(
                _glob.glob(_ag.get("pulse_unix", "/run/user/*/pulse/native")) or
                _glob.glob(os.path.join(_tmp, "pulse-*", "native"))
            )
            pw_running = bool(_glob.glob(_ag.get("pipewire", "/run/user/*/pipewire-0")))
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

    def _usb(self) -> None:
        """Append the NEC xHCI controller, USB keyboard, and tablet/mouse device."""
        # nec-usb-xhci: NEC uPD720200 USB 3.0 (PCI 1033:0194) — real chip PCI IDs.
        # qemu-xhci uses 1b36 (Red Hat/QEMU) which inxi detects as virtual.
        self.args += ["-device", "nec-usb-xhci,id=usb", "-device", "usb-kbd"]
        self.args += ["-device", "usb-tablet" if self.cfg.tablet else "usb-mouse"]

    def _battery(self) -> None:
        """No-op placeholder — ACPI battery tables are not yet implemented for QEMU."""
        pass

    def _kernel_direct(self) -> None:
        """Append -kernel/-initrd/-append when direct kernel boot paths are configured."""
        if self.cfg.kernel_path:    self.args += ["-kernel", self.cfg.kernel_path]
        if self.cfg.initrd_path:    self.args += ["-initrd", self.cfg.initrd_path]
        if self.cfg.kernel_cmdline: self.args += ["-append", self.cfg.kernel_cmdline]

    def _qmp(self) -> None:
        """Append QMP chardev/mon args; Unix socket on Linux/macOS, TCP on Windows."""
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

    def _monitor(self) -> None:
        """Append human-monitor chardev/mon args; Unix socket on Linux/macOS, TCP on Windows."""
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

    def _serial(self) -> None:
        """Append a serial console (Unix socket on Linux/macOS, TCP telnet on Windows)."""
        if sys.platform == "win32":
            port = self.cfg.serial_tcp_port or _next_free_port(
                _CFG["ports"].get("serial_port_start", 9200), []
            )
            self.cfg.serial_tcp_port = port
            self.args += ["-serial", f"telnet:127.0.0.1:{port},server,nowait"]
        else:
            sock = os.path.join(self.vm_dir, "serial.sock")
            self.args += ["-serial", f"unix:{sock},server,nowait"]

    def _tpm(self) -> None:
        """Append TPM chardev, tpmdev emulator, and tpm-tis device."""
        tpm_sock = os.path.join(self.cfg.get_vm_dir(), "tpm.sock")
        self.args += [
            "-chardev", f"socket,id=chrtpm,path={tpm_sock}",
            "-tpmdev",  "emulator,id=tpm0,chardev=chrtpm",
            "-device",  "tpm-tis,tpmdev=tpm0",
        ]

    def _misc(self) -> None:
        """Append virtio-rng (non-hardened), -no-reboot (ISO boot), and -no-user-config."""
        if not self.cfg.hardened:
            self.args += ["-device", "virtio-rng-pci"]
        if self.cfg.iso_path:
            self.args += ["-no-reboot"]
        self.args += ["-no-user-config"]

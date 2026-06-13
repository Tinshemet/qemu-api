"""
qemu_arg_builder.py — QEMU Argument Building Layer

Translates a MachineConfig into the full QEMU command-line argument
list. Also owns: QEMU version detection, port pool helpers, and the
ISO search-directory scanner.
"""

import glob as _glob
import os
import re
import socket
import subprocess
from typing import List, Tuple

from qemu_config import (
    AUDIO_PRESETS, BIOS_OPTIONS, CPU_PRESETS, GPU_PRESETS, MachineConfig, OVMF,
)

# ── QEMU Version Detection ─────────────────────────────────────────────────────

def _parse_qemu_version(binary: str = "qemu-system-x86_64") -> Tuple[int, int, int]:
    """Return (major, minor, patch). Returns (0, 0, 0) if detection fails."""
    try:
        r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5)
        m = re.search(r"version (\d+)[.](\d+)[.](\d+)", r.stdout)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
    except Exception:
        pass
    return (0, 0, 0)


QEMU_VERSION: Tuple[int, int, int] = _parse_qemu_version()


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

VNC_PORT_START   = 5900
SPICE_PORT_START = 5930
PORT_RANGE       = 50  # supports up to 50 simultaneous VMs


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _next_free_port(start: int, used: List[int]) -> int:
    for p in range(start, start + PORT_RANGE):
        if p not in used and _port_free(p):
            return p
    raise RuntimeError(f"No free port found starting from {start}")


# ── ISO Search Directory Scanner ───────────────────────────────────────────────

def _build_iso_search_dirs() -> List[str]:
    """Build ISO search dirs dynamically — handles capital/lowercase variants."""
    home    = os.path.expanduser("~")
    desktop = os.path.join(home, "Desktop")
    dirs: List[str] = []
    if os.path.isdir(desktop):
        for entry in os.listdir(desktop):
            full = os.path.join(desktop, entry)
            if os.path.isdir(full) and entry.lower() in ("images", "iso", "isos", "vms", "vm"):
                dirs.append(full)
        dirs.append(desktop)
    for sub in ["Downloads", "downloads", "iso", "ISO", "ISOs", "isos",
                "images", "Images", "vm", "VMs"]:
        p = os.path.join(home, sub)
        if os.path.isdir(p) and p not in dirs:
            dirs.append(p)
    dirs.append("/tmp")
    return dirs


ISO_SEARCH_DIRS = _build_iso_search_dirs()


# ── QEMU Argument Builder ──────────────────────────────────────────────────────

class QemuArgBuilder:
    def __init__(self, config: MachineConfig):
        self.cfg      = config
        self.vm_dir   = config.get_vm_dir()
        self.args:    List[str] = []
        self.qemu_ver = QEMU_VERSION
        self.is_arm   = config.machine_arch in ("aarch64", "arm", "armhf")
        self.is_raspi = "raspi" in config.machine_type.lower()

    def build(self) -> List[str]:
        self.args = [self.cfg.qemu_binary]
        self._base()
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
        self.args += self.cfg.extra_args
        return [a for a in self.args if a]

    def _base(self):
        self.args += ["-name", f"{self.cfg.name},process={self.cfg.name}"]
        # -enable-kvm enables KVM; accel=kvm is set in _machine() — do NOT add -accel kvm here
        if self.cfg.kvm and not self.is_arm:
            self.args += ["-enable-kvm"]
        if self.is_arm:
            self.cfg.kvm = False  # KVM never works for ARM guests on x86 host

    def _machine(self):
        machine_str = self.cfg.machine_type
        extras = []
        if self.cfg.kvm and not self.is_arm:
            extras.append("accel=kvm")
        if self.cfg.iommu and not self.is_arm:
            extras.append("kernel_irqchip=on")
        if extras:
            machine_str += "," + ",".join(extras)
        self.args += ["-machine", machine_str]
        if self.cfg.hpet and not self.is_arm:
            self.args += ["-device", "hpet"]
        if not self.is_raspi:
            # 'host' is NOT a valid -rtc base value — use 'utc' instead
            rtc_base = self.cfg.rtc_clock
            if rtc_base not in ("utc", "localtime") and not rtc_base[0].isdigit():
                rtc_base = "utc"
            self.args += ["-rtc", f"base={rtc_base},driftfix=slew"]

    def _cpu(self):
        cpu_str  = CPU_PRESETS.get(self.cfg.cpu_model, self.cfg.cpu_model)
        cpu_name = cpu_str.replace("-cpu ", "").split(",")[0]
        features = list(self.cfg.cpu_features)
        if self.cfg.kvm_pv_features and not self.is_arm:
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

    def _memory(self):
        self.args += ["-m", str(self.cfg.memory_mb)]
        if self.cfg.hugepages and not self.is_arm:
            self.args += ["-mem-path", self.cfg.hugepages_path, "-mem-prealloc"]
        if self.cfg.balloon and not self.is_raspi:
            self.args += ["-device", "virtio-balloon-pci"]

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

    def _disks(self):
        if self.is_raspi:
            # raspi3b ONLY accepts SD card interface
            for disk in self.cfg.disks:
                disk_path = os.path.expanduser(disk.path)
                self.args += ["-drive", f"file={disk_path},format={disk.format},if=sd,index=0"]
            return

        has_scsi = any(d.bus == "scsi" for d in self.cfg.disks)
        if has_scsi:
            self.args += ["-device", "virtio-scsi-pci,id=scsi0"]
        for i, disk in enumerate(self.cfg.disks):
            self.args += disk.to_qemu_args(i)
        if self.cfg.iso_path:
            self.args += [
                "-drive", f"file={self.cfg.iso_path},if=none,id=cdrom0,readonly=on,media=cdrom",
                "-device", "ide-cd,drive=cdrom0,bootindex=1",
                "-boot",   f"order={self.cfg.boot_order},menu=on",
            ]
        else:
            self.args += ["-boot", "order=c,menu=on"]

    def _network(self):
        if not self.cfg.networks:
            self.args += ["-nic", "user,model=virtio-net-pci"]
            return
        for net in self.cfg.networks:
            self.args += net.to_qemu_args()

    @staticmethod
    def _gl_available() -> bool:
        """Check if virgl/OpenGL is actually usable before passing gl=on."""
        try:
            r = subprocess.run(
                ["qemu-system-x86_64", "-display", "sdl,gl=on",
                 "-machine", "none", "-no-user-config"],
                capture_output=True, text=True, timeout=3,
            )
            err = (r.stderr or "").lower()
            return "gl" not in err and "opengl" not in err
        except Exception:
            return False

    def _display(self):
        if self.is_raspi:
            self.args += ["-nographic"]  # raspi3b has NO video output in QEMU
            return
        gpu_device = GPU_PRESETS.get(self.cfg.gpu)
        if self.cfg.display == "none" or self.cfg.gpu == "none":
            self.args += ["-nographic"]
            return

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
            port = self.cfg.spice_port or 5930
            self.args += [
                "-spice",   f"port={port},disable-ticketing=on",
                "-device",  "virtio-serial",
                "-chardev", "spicevmc,id=vdagent,debug=0,name=vdagent",
                "-device",  "virtserialport,chardev=vdagent,name=com.redhat.spice.0",
                "-display", "spice-app",
            ]
        elif self.cfg.display == "vnc":
            port = self.cfg.vnc_port or 5900
            self.args += ["-vnc", f":{port - 5900}"]

        if gpu_device and not self.is_raspi:
            if gpu_device == "virtio-vga-gl":
                self.args += ["-device", "virtio-vga-gl,xres=1920,yres=1080"]
            elif gpu_device == "vfio-pci":
                pci = getattr(self.cfg, "_vfio_pci", "0000:01:00.0")
                self.args += ["-device", f"vfio-pci,host={pci}"]
            else:
                # vgamem_mb removed in QEMU 7+ — don't pass it
                self.args += ["-device", gpu_device]

    def _audio(self):
        if self.is_raspi:
            return
        audio_dev = AUDIO_PRESETS.get(self.cfg.audio)
        if not audio_dev:
            return
        pa_running = bool(
            _glob.glob("/run/user/*/pulse/native") or
            _glob.glob("/tmp/pulse-*/native")
        )
        pw_running = bool(_glob.glob("/run/user/*/pipewire-0"))
        if pa_running:
            audiodev = "pa,id=audio0"
        elif pw_running:
            audiodev = "pipewire,id=audio0"
        else:
            return  # no audio server found — skip to avoid crash
        self.args += ["-audiodev", audiodev, "-device", audio_dev]
        if self.cfg.audio in ("hda", "ich9"):
            self.args += ["-device", "hda-duplex,audiodev=audio0"]

    def _usb(self):
        self.args += ["-device", "qemu-xhci,id=usb", "-device", "usb-kbd"]
        self.args += ["-device", "usb-tablet" if self.cfg.tablet else "usb-mouse"]

    def _battery(self):
        if self.cfg.battery and not self.is_arm:
            self.args += ["-device", "acpi-battery"]

    def _kernel_direct(self):
        if self.cfg.kernel_path:    self.args += ["-kernel", self.cfg.kernel_path]
        if self.cfg.initrd_path:    self.args += ["-initrd", self.cfg.initrd_path]
        if self.cfg.kernel_cmdline: self.args += ["-append", self.cfg.kernel_cmdline]

    def _qmp(self):
        sock = os.path.join(self.vm_dir, "qmp.sock")
        self.cfg.qmp_socket = sock
        self.args += [
            "-chardev", f"socket,id=qmp,path={sock},server=on,wait=off",
            "-mon",     "chardev=qmp,mode=control,pretty=off",
        ]

    def _monitor(self):
        sock = os.path.join(self.vm_dir, "monitor.sock")
        self.cfg.monitor_socket = sock
        self.args += [
            "-chardev", f"socket,id=mon,path={sock},server=on,wait=off",
            "-mon",     "chardev=mon,mode=readline",
        ]

    def _serial(self):
        sock = os.path.join(self.vm_dir, "serial.sock")
        self.args += ["-serial", f"unix:{sock},server,nowait"]

    def _misc(self):
        self.args += ["-device", "virtio-rng-pci", "-no-user-config"]

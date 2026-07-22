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
from ._qemu_smbios import _QemuSmbiosMixin
from .qemu_host_utils import (  # host/qemu utils (extracted from this file)
    _parse_qemu_version, qemu_version_warn, _port_free,
    next_free_port, build_iso_search_dirs,
)

with open(os.path.join(os.path.dirname(__file__), "config.json")) as _f:
    _CFG = json.load(_f)
_PORTS    = _CFG["ports"]
_TIMEOUTS = _CFG["timeouts"]

# ── QEMU Version Detection ─────────────────────────────────────────────────────


QEMU_VERSION: Tuple[int, int, int] = _parse_qemu_version()


# ── Port Pool (auto-assign VNC / SPICE ports) ─────────────────────────────────

VNC_PORT_START   = _PORTS["vnc_start"]
SPICE_PORT_START = _PORTS["spice_start"]
PORT_RANGE       = _PORTS["port_range"]


# ── ISO Search Directory Scanner ───────────────────────────────────────────────

# Builds a list of directories to search for ISO files based on common home subdirectories.
# In: nothing → Out: List[str]
_ISO_DESKTOP_SUBDIRS = set(_CFG["iso_desktop_subdirs"])
_ISO_HOME_SUBDIRS    = _CFG["iso_home_subdirs"]


# ── QEMU Argument Builder ──────────────────────────────────────────────────────

class QemuArgBuilder(_QemuSmbiosMixin):
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
        self._qga()
        self._serial_agent()
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
        # +invtsc: advertise an invariant TSC (CPUID 80000007H:EDX[8]) like real
        # silicon. KVM masks it by default (it blocks live migration, which we
        # don't do), and its ABSENCE is a common timing-based VM tell. This does
        # NOT defeat VMEXIT-latency red-pills — that overhead is inherent to
        # hardware virtualisation — but it closes the naive "no invariant TSC" check.
        for flag in ("-hypervisor", "kvm=off", "-vmx", "+invtsc"):
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
        # Unattended Windows install: attach the generated answer-file CD so
        # Windows Setup runs fully hands-off. Opt-in; the ISO is built at
        # create_vm time into the VM dir. Inert if the ISO isn't present.
        if self.cfg.unattended:
            unattend_iso = os.path.join(self.vm_dir, "autounattend.iso")
            if os.path.exists(unattend_iso):
                # The install ISO already holds the single default-IDE unit, so put
                # the answer CD on the AHCI controller (present for Windows' SATA
                # disk) at the port after the disks. Falls back to plain ide-cd only
                # if there's no AHCI controller (non-Windows edge case).
                n_sata = sum(1 for d in self.cfg.disks if d.bus == "sata")
                dev = f"ide-cd,drive=cdrom_ua,bus=ahci.{n_sata}" if has_sata else "ide-cd,drive=cdrom_ua"
                self.args += [
                    "-drive",  f"file={unattend_iso},if=none,id=cdrom_ua,readonly=on,media=cdrom",
                    "-device", dev,
                ]
        # Unattended Linux (casper family — Ubuntu/Mint) install: attach the
        # generated cidata volume so cloud-init's autoinstall runs hands-off up
        # to account creation. Opt-in; built at create_vm time into the VM dir.
        # Inert if absent — debian-installer family (Kali) injects its preseed
        # into the initrd instead and never creates this file.
        if self.cfg.unattended:
            cidata_iso = os.path.join(self.vm_dir, "cidata.iso")
            if os.path.exists(cidata_iso):
                # q35's default ide.0 (where cdrom0 lands) is a single-unit bus —
                # a second bare ide-cd collides on it ("bus supports only 1
                # units"). ide.1 is q35's other independent single-unit legacy
                # IDE channel, confirmed free.
                self.args += [
                    "-drive",  f"file={cidata_iso},if=none,id=cdrom_cidata,readonly=on,media=cdrom",
                    "-device", "ide-cd,drive=cdrom_cidata,bus=ide.1",
                ]

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
        # GPU passthrough: hand the guest a REAL GPU via vfio-pci so its /sys PCI
        # vendor/device IDs are genuine hardware — the one way to defeat the
        # "display adapter = VMware 15ad" tell that no emulated GPU can hide.
        # Requires host prep: IOMMU on, the GPU bound to vfio-pci and isolated.
        # The passed GPU drives the guest's own output, so QEMU runs headless.
        if self.cfg.gpu_passthrough_pci:
            # Comma-separated host PCI addresses (BDF). The first is the primary GPU
            # function and gets x-vga=on; the rest (e.g. the .1 HDMI-audio function,
            # or other devices in the IOMMU group) are passed as plain vfio-pci.
            addrs = [a.strip() for a in self.cfg.gpu_passthrough_pci.split(",") if a.strip()]
            for i, addr in enumerate(addrs):
                dev = f"vfio-pci,host={addr}" + (",x-vga=on" if i == 0 else "")
                self.args += ["-device", dev]
            self.args += ["-display", "none"]
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
        """Append the NEC xHCI controller, USB keyboard, and pointing device.

        Stealth forces a relative ``usb-mouse`` instead of ``usb-tablet``: the
        absolute-positioning tablet is a hypervisor-console convention (bare-metal
        machines don't have one) that virt-detection reads as a VM tell. The
        tradeoff is relative pointer motion over VNC/SDL for stealth VMs.
        """
        # nec-usb-xhci: NEC uPD720200 USB 3.0 (PCI 1033:0194) — real chip PCI IDs.
        # qemu-xhci uses 1b36 (Red Hat/QEMU) which inxi detects as virtual.
        self.args += ["-device", "nec-usb-xhci,id=usb", "-device", "usb-kbd"]
        use_tablet = self.cfg.tablet and not self.cfg.stealth
        self.args += ["-device", "usb-tablet" if use_tablet else "usb-mouse"]

        # Unattended Windows: attach the FAT answer medium as a removable USB stick.
        # OVMF mounts FAT (unlike the plain answer ISO), so the UEFI shell auto-runs
        # its startup.nsh to launch the installer — the install boots hands-off.
        # Windows Setup also reads autounattend.xml off it. Attached here (after the
        # xHCI controller) so bus=usb.0 resolves. Inert if the image isn't present.
        if self.cfg.unattended:
            unattend_img = os.path.join(self.vm_dir, "autounattend.img")
            if os.path.exists(unattend_img):
                self.args += [
                    "-drive",  f"file={unattend_img},if=none,id=ua_fat,format=raw",
                    "-device", "usb-storage,drive=ua_fat,removable=on,bus=usb.0",
                ]

    def _battery(self) -> None:
        """Inject a synthetic ACPI battery + AC adapter for laptop personas.

        QEMU has no battery device, so a laptop persona otherwise exposes no
        /sys/class/power_supply/BAT0 — a clean "laptop with no battery"
        inconsistency (upower/acpi/GNOME reveal it). When cfg.battery is set
        (laptop machine_class) and the SSDT has been compiled, add it via
        -acpitable. Inert until acpi/battery.aml exists, so a missing/uncompiled
        table never risks the guest's ACPI boot.
        """
        if self.is_arm or not self.cfg.battery:
            return
        aml = os.path.join(os.path.dirname(__file__), "acpi", "battery.aml")
        if os.path.exists(aml):
            self.args += ["-acpitable", f"file={aml}"]

    def _kernel_direct(self) -> None:
        """Append -kernel/-initrd/-append when direct kernel boot paths are configured."""
        if self.cfg.kernel_path:    self.args += ["-kernel", self.cfg.kernel_path]
        if self.cfg.initrd_path:    self.args += ["-initrd", self.cfg.initrd_path]
        if self.cfg.kernel_cmdline: self.args += ["-append", self.cfg.kernel_cmdline]

    def _qmp(self) -> None:
        """Append QMP chardev/mon args; Unix socket on Linux/macOS, TCP on Windows."""
        if sys.platform == "win32":
            port = self.cfg.qmp_tcp_port or next_free_port(
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

    def _qga(self) -> None:
        """Append the qemu-guest-agent virtio-serial channel — non-stealth VMs only.

        Opt-in (``cfg.guest_agent``) and OFF for stealth VMs (the virtio-serial
        device is a hypervisor tell; stealth VMs get a serial-console channel via
        ``_serial_agent()`` instead). Gives the agent its OWN virtio-serial
        controller (not shared with the SPICE vdagent bus) exposing a port named
        ``org.qemu.guest_agent.0`` — the fixed name the ``qemu-ga`` daemon expects.
        Mirrors ``_qmp()``'s Unix-socket / Windows-TCP split.
        """
        if not self.cfg.guest_agent or self.cfg.stealth or self.is_arm:
            return
        if sys.platform == "win32":
            port = self.cfg.qga_tcp_port or next_free_port(
                _CFG["ports"].get("qga_port_start", 9300), []
            )
            self.cfg.qga_tcp_port = port
            self.cfg.qga_socket   = f"tcp:127.0.0.1:{port}"
            chardev = f"socket,id=qga0,host=127.0.0.1,port={port},server=on,wait=off"
        else:
            sock = os.path.join(self.vm_dir, "qga.sock")
            self.cfg.qga_socket = sock
            chardev = f"socket,id=qga0,path={sock},server=on,wait=off"
        self.args += [
            "-device",  "virtio-serial-pci,id=qga",
            "-chardev", chardev,
            "-device",  "virtserialport,bus=qga.0,chardev=qga0,name=org.qemu.guest_agent.0",
        ]

    def _serial_agent(self) -> None:
        """Append a second plain UART (COM2) as the stealth guest-agent channel.

        Mutually exclusive with ``_qga()`` by construction — stealth VMs skip
        virtio-serial entirely (a hypervisor tell) and instead get a second
        ``-serial`` flag, the exact same device class as ``_serial()``'s COM1
        console, so nothing about this port's hardware signature differs from
        a real second UART. The wire protocol spoken over it (PSK-authenticated
        JSON lines) lives in ``serial_agent_client.py`` / the guest-side daemon
        from ``_vm_guest.py``, not here — this method only wires the transport.
        """
        if not self.cfg.guest_agent or not self.cfg.stealth or self.is_arm:
            return
        if sys.platform == "win32":
            port = self.cfg.serial_agent_tcp_port or next_free_port(
                _CFG["ports"].get("serial_agent_port_start", 9400), []
            )
            self.cfg.serial_agent_tcp_port = port
            self.cfg.serial_agent_socket   = f"tcp:127.0.0.1:{port}"
            self.args += ["-serial", f"telnet:127.0.0.1:{port},server,nowait"]
        else:
            sock = self.cfg.get_serial_agent_socket()
            self.cfg.serial_agent_socket = sock
            self.args += ["-serial", f"unix:{sock},server,nowait"]

    def _monitor(self) -> None:
        """Append human-monitor chardev/mon args; Unix socket on Linux/macOS, TCP on Windows."""
        if sys.platform == "win32":
            port = self.cfg.monitor_tcp_port or next_free_port(
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
            port = self.cfg.serial_tcp_port or next_free_port(
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

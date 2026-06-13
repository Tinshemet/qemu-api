"""
qemu_manager.py — QEMU/KVM Machine Manager
Part 2 of 4: QEMU/KVM Ollama Wrapper

New in v3:
  - State persistence  (survives terminal/reboot)
  - VM cloning
  - Auto port assignment (VNC/SPICE no collisions)
  - Full snapshot management (list, restore, delete)
  - Resource limits via cgroups/cpulimit
  - Network isolation (private inter-VM networks)
  - --dry-run support
  - ISO directory scanner
"""

import os, subprocess, json, socket, time, shutil, signal
import threading, psutil, glob, re
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime

from qemu_config import (
    MachineConfig, DiskConfig, NetworkConfig,
    CPU_PRESETS, GPU_PRESETS, AUDIO_PRESETS, BIOS_OPTIONS, OVMF,
    apply_os_hints,
)

VM_BASE_DIR      = os.path.expanduser("~/.qemu_vms")
STATE_FILE       = os.path.join(VM_BASE_DIR, ".state.json")
ISOLATED_NET_DIR = os.path.join(VM_BASE_DIR, "_networks")

def _build_iso_search_dirs() -> List[str]:
    """Build ISO search dirs dynamically — handles capital/lowercase variants."""
    home    = os.path.expanduser("~")
    desktop = os.path.join(home, "Desktop")
    dirs    = []
    # Scan Desktop subdirectories for any image/iso folders (case-insensitive)
    if os.path.isdir(desktop):
        for entry in os.listdir(desktop):
            full = os.path.join(desktop, entry)
            if os.path.isdir(full) and entry.lower() in ("images", "iso", "isos", "vms", "vm"):
                dirs.append(full)
        dirs.append(desktop)
    # Common home subdirs — add both capitalised and lowercase variants
    for sub in ["Downloads", "downloads", "iso", "ISO", "ISOs", "isos",
                "images", "Images", "vm", "VMs"]:
        p = os.path.join(home, sub)
        if os.path.isdir(p) and p not in dirs:
            dirs.append(p)
    dirs.append("/tmp")
    return dirs

ISO_SEARCH_DIRS = _build_iso_search_dirs()

# ─────────────────────────────────────────────
#  PORT POOL  (auto-assign VNC / SPICE ports)
# ─────────────────────────────────────────────

VNC_PORT_START   = 5900
SPICE_PORT_START = 5930
PORT_RANGE       = 50   # support up to 50 simultaneous VMs


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) != 0


def _next_free_port(start: int, used: List[int]) -> int:
    for p in range(start, start + PORT_RANGE):
        if p not in used and _port_free(p):
            return p
    raise RuntimeError(f"No free port found starting from {start}")


# ─────────────────────────────────────────────
#  ARG BUILDER
# ─────────────────────────────────────────────

# ── QEMU Version Detection ────────────────────────────────────────────────────
# Parsed once at import time, used by QemuArgBuilder to conditionally
# include/exclude args that changed between QEMU versions.

def _parse_qemu_version(binary: str = "qemu-system-x86_64") -> Tuple[int, int, int]:
    """Return (major, minor, patch) tuple. Returns (0,0,0) if detection fails."""
    try:
        r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=5)
        # "QEMU emulator version 8.2.2 (Debian ...)"
        m = re.search(r"version (\d+)[.](\d+)[.](\d+)", r.stdout)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
    except Exception:
        pass
    return (0, 0, 0)


QEMU_VERSION: Tuple[int, int, int] = _parse_qemu_version()


def _qemu_version_warn() -> None:
    """Print a warning panel with known version-specific issues."""
    major, minor, patch = QEMU_VERSION
    ver_str = f"{major}.{minor}.{patch}" if QEMU_VERSION != (0,0,0) else "unknown"
    warnings = []

    if QEMU_VERSION == (0, 0, 0):
        warnings.append("QEMU version could not be detected — some features may not work")

    # vgamem_mb was removed in QEMU 7.x+
    if major >= 7:
        warnings.append(
            f"QEMU {ver_str}: 'vgamem_mb' property removed — "
            "virtio-vga-gl will be used without memory size arg (handled automatically)"
        )

    # -accel kvm conflicts with machine accel= since QEMU 6.x
    if major >= 6:
        warnings.append(
            f"QEMU {ver_str}: '-accel kvm' conflicts with '-machine accel=kvm' "
            "— using -enable-kvm only (handled automatically)"
        )

    # Audio backend changes in 7.x
    if major >= 7:
        warnings.append(
            f"QEMU {ver_str}: PulseAudio backend may need pipewire-pulse — "
            "falling back to 'none' if pa fails"
        )

    if warnings:
        from rich.console import Console as _Con
        from rich.panel   import Panel as _Pan
        _c = _Con()
        body = "\n".join(f"  [yellow]warn[/yellow] {w}" for w in warnings)
        _c.print(_Pan(
            body,
            title=f"[bold yellow]QEMU {ver_str} Compatibility Notes[/bold yellow]",
            border_style="yellow",
        ))


class QemuArgBuilder:
    def __init__(self, config: MachineConfig):
        self.cfg      = config
        self.vm_dir   = config.get_vm_dir()
        self.args:    List[str] = []
        self.qemu_ver = QEMU_VERSION
        # ARM / non-x86 flag — disables x86-specific features
        self.is_arm   = config.machine_arch in ("aarch64", "arm", "armhf")
        # raspi machines are extra restricted
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
        # KVM only works when host and guest arch match
        # -enable-kvm enables the KVM module; accel=kvm is set in _machine()
        # Do NOT add -accel kvm here — it conflicts with -machine accel=kvm
        if self.cfg.kvm and not self.is_arm:
            self.args += ["-enable-kvm"]
        # ARM on x86: pure emulation, no KVM
        if self.is_arm:
            self.cfg.kvm = False

    def _machine(self):
        machine_str = self.cfg.machine_type
        extras = []
        # Only add accel=kvm for x86
        if self.cfg.kvm and not self.is_arm:
            extras.append("accel=kvm")
        if self.cfg.iommu and not self.is_arm:
            extras.append("kernel_irqchip=on")
        if extras:
            machine_str += "," + ",".join(extras)
        self.args += ["-machine", machine_str]
        if self.cfg.hpet and not self.is_arm:
            self.args += ["-device", "hpet"]
        # raspi doesn't support -rtc
        if not self.is_raspi:
            # Valid base values: utc, localtime, or an ISO 8601 datetime
            # 'host' is NOT a valid -rtc base value — use 'utc' instead
            rtc_base = self.cfg.rtc_clock
            if rtc_base not in ("utc", "localtime") and not rtc_base[0].isdigit():
                rtc_base = "utc"
            self.args += ["-rtc", f"base={rtc_base},driftfix=slew"]

    def _cpu(self):
        cpu_str  = CPU_PRESETS.get(self.cfg.cpu_model, self.cfg.cpu_model)
        cpu_name = cpu_str.replace("-cpu ", "").split(",")[0]
        features = list(self.cfg.cpu_features)
        # kvm_pv_unhalt is x86-only
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
        # Hugepages are x86-only
        if self.cfg.hugepages and not self.is_arm:
            self.args += ["-mem-path", self.cfg.hugepages_path, "-mem-prealloc"]
        # balloon device only on x86 virtio
        if self.cfg.balloon and not self.is_raspi:
            self.args += ["-device", "virtio-balloon-pci"]

    def _firmware(self):
        # x86 only — raspi and ARM virt have their own ROM
        if self.is_arm:
            return
        bios_path = BIOS_OPTIONS.get(self.cfg.bios) or OVMF.get("code")
        if bios_path and os.path.exists(bios_path):
            self.args += ["-drive", f"if=pflash,format=raw,readonly=on,file={bios_path}"]
            # Find VARS: use config path, then check vm_dir for any .fd file
            vars_path = self.cfg.uefi_vars
            if not vars_path or not os.path.exists(vars_path):
                # Scan vm_dir for any VARS-style .fd file
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
        # SMBIOS is x86-only
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
            # Use the primary disk as an SD card image
            for disk in self.cfg.disks:
                disk_path = os.path.expanduser(disk.path)
                self.args += [
                    "-drive",
                    f"file={disk_path},format={disk.format},if=sd,index=0",
                ]
            # raspi boots from SD — no separate boot flag needed
            return

        # Standard x86 disk handling
        has_scsi = any(d.bus == "scsi" for d in self.cfg.disks)
        if has_scsi:
            self.args += ["-device", "virtio-scsi-pci,id=scsi0"]
        for i, disk in enumerate(self.cfg.disks):
            self.args += disk.to_qemu_args(i)
        if self.cfg.iso_path:
            self.args += [
                "-drive", f"file={self.cfg.iso_path},if=none,id=cdrom0,readonly=on,media=cdrom",
                "-device", "ide-cd,drive=cdrom0,bootindex=1",
                "-boot", f"order={self.cfg.boot_order},menu=on",
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
            import subprocess
            r = subprocess.run(
                ["qemu-system-x86_64", "-display", "sdl,gl=on",
                 "-machine", "none", "-no-user-config"],
                capture_output=True, text=True, timeout=3
            )
            # If gl=on is unsupported the error contains "gl=on" or "opengl"
            err = (r.stderr or "").lower()
            return "gl" not in err and "opengl" not in err
        except Exception:
            return False

    def _display(self):
        # raspi3b has NO video output in QEMU - serial console only
        if self.is_raspi:
            self.args += ["-nographic"]
            return
        gpu_device = GPU_PRESETS.get(self.cfg.gpu)
        if self.cfg.display == "none" or self.cfg.gpu == "none":
            self.args += ["-nographic"]
            return

        # Probe GL availability — fall back to gl=off if not supported
        # This prevents "sdl,gl=on" crashing silently with no log file
        gl_wanted = self.cfg.opengl and not self.is_arm
        gl_ok     = gl_wanted and self._gl_available()
        gl_flag   = "gl=on" if gl_ok else "gl=off"

        # If gl=on was wanted but unavailable, also downgrade virtio-vga-gl → virtio-vga
        if gl_wanted and not gl_ok and self.cfg.gpu == "virtio":
            gpu_device = "virtio-vga"  # non-GL variant

        if self.cfg.display == "sdl":
            self.args += ["-display", f"sdl,{gl_flag}"]
        elif self.cfg.display == "gtk":
            self.args += ["-display", f"gtk,{gl_flag}"]
        elif self.cfg.display == "spice":
            port = self.cfg.spice_port or 5930
            self.args += [
                "-spice", f"port={port},disable-ticketing=on",
                "-device", "virtio-serial",
                "-chardev", "spicevmc,id=vdagent,debug=0,name=vdagent",
                "-device", "virtserialport,chardev=vdagent,name=com.redhat.spice.0",
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
        # QEMU 7+: detect whether PulseAudio or PipeWire is running
        # pa,id=audio0 fails silently on PipeWire-only systems
        import glob as _glob
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
            # No audio server found — skip audio entirely to avoid crash
            return
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
        if self.cfg.kernel_path:   self.args += ["-kernel", self.cfg.kernel_path]
        if self.cfg.initrd_path:   self.args += ["-initrd", self.cfg.initrd_path]
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
        # Always expose a serial console socket for shell access (Part 2)
        sock = os.path.join(self.vm_dir, "serial.sock")
        self.args += ["-serial", f"unix:{sock},server,nowait"]

    def _misc(self):
        self.args += ["-device", "virtio-rng-pci", "-no-user-config"]


# ─────────────────────────────────────────────
#  QMP CLIENT
# ─────────────────────────────────────────────

class QMPClient:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.sock = None

    def connect(self, timeout=5):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(self.socket_path)
        self._recv()
        self._send({"execute": "qmp_capabilities"})
        self._recv()

    def _send(self, data: dict):
        self.sock.sendall((json.dumps(data) + "\n").encode())

    def _recv(self) -> dict:
        buf = b""
        while True:
            buf += self.sock.recv(4096)
            try:
                return json.loads(buf.decode())
            except json.JSONDecodeError:
                continue

    def execute(self, cmd: str, args: dict = None) -> dict:
        payload = {"execute": cmd}
        if args:
            payload["arguments"] = args
        self._send(payload)
        return self._recv()

    def close(self):
        if self.sock:
            self.sock.close()


# ─────────────────────────────────────────────
#  STATE PERSISTENCE
# ─────────────────────────────────────────────

class VMState:
    """
    Persists running VM PIDs to ~/.qemu_vms/.state.json
    so the manager can reconnect after a terminal restart.
    """
    def __init__(self):
        os.makedirs(VM_BASE_DIR, exist_ok=True)
        self._data: Dict[str, Dict] = self._load()

    def _load(self) -> Dict:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save(self):
        with open(STATE_FILE, "w") as f:
            json.dump(self._data, f, indent=2)

    def set_running(self, name: str, pid: int):
        self._data[name] = {"pid": pid, "started": datetime.now().isoformat()}
        self._save()

    def set_stopped(self, name: str):
        self._data.pop(name, None)
        self._save()

    def get_pid(self, name: str) -> Optional[int]:
        entry = self._data.get(name)
        if not entry:
            return None
        pid = entry.get("pid")
        # Verify the process is actually still alive
        try:
            p = psutil.Process(pid)
            if p.is_running() and p.name().startswith("qemu"):
                return pid
        except (psutil.NoSuchProcess, TypeError):
            self.set_stopped(name)
        return None

    def all_running(self) -> Dict[str, int]:
        """Return {name: pid} for all VMs that are actually still running."""
        live = {}
        for name in list(self._data.keys()):
            pid = self.get_pid(name)
            if pid:
                live[name] = pid
        return live


# ─────────────────────────────────────────────
#  ISOLATED NETWORK MANAGER
# ─────────────────────────────────────────────

class IsolatedNetManager:
    """
    Creates private virtual networks between VMs using QEMU's socket networking.
    VMs on the same isolated net can talk to each other but NOT to the internet.
    """
    NET_FILE = os.path.join(ISOLATED_NET_DIR, "networks.json")

    def __init__(self):
        os.makedirs(ISOLATED_NET_DIR, exist_ok=True)
        self._nets: Dict[str, Dict] = self._load()

    def _load(self) -> Dict:
        if os.path.exists(self.NET_FILE):
            try:
                with open(self.NET_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save(self):
        with open(self.NET_FILE, "w") as f:
            json.dump(self._nets, f, indent=2)

    def create_network(self, net_name: str) -> Dict[str, Any]:
        if net_name in self._nets:
            return {"success": False, "error": f"Network '{net_name}' already exists."}
        # Find a free mcast port (QEMU socket multicast)
        used_ports = [n["mcast_port"] for n in self._nets.values()]
        port = 1234
        while port in used_ports:
            port += 1
        self._nets[net_name] = {
            "name": net_name,
            "mcast_port": port,
            "mcast_addr": "230.0.0.1",
            "members": [],
            "created": datetime.now().isoformat(),
        }
        self._save()
        return {"success": True, "network": self._nets[net_name]}

    def delete_network(self, net_name: str) -> Dict[str, Any]:
        if net_name not in self._nets:
            return {"success": False, "error": f"Network '{net_name}' not found."}
        del self._nets[net_name]
        self._save()
        return {"success": True, "message": f"Network '{net_name}' deleted."}

    def list_networks(self) -> List[Dict]:
        return list(self._nets.values())

    def get_netdev_args(self, net_name: str, vm_name: str) -> Optional[List[str]]:
        """Return QEMU -netdev args to attach a VM to an isolated network."""
        net = self._nets.get(net_name)
        if not net:
            return None
        if vm_name not in net["members"]:
            net["members"].append(vm_name)
            self._save()
        addr  = net["mcast_addr"]
        port  = net["mcast_port"]
        netid = f"iso_{net_name}"
        return [
            "-netdev", f"socket,id={netid},mcast={addr}:{port}",
            "-device", f"virtio-net-pci,netdev={netid}",
        ]

    def add_vm_to_network(self, net_name: str, vm_name: str) -> Dict[str, Any]:
        """Update a stopped VM's config to include an isolated network interface."""
        net = self._nets.get(net_name)
        if not net:
            return {"success": False, "error": f"Network '{net_name}' not found."}
        try:
            cfg = MachineConfig.load(vm_name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}
        addr  = net["mcast_addr"]
        port  = net["mcast_port"]
        netid = f"iso_{net_name}"
        iso_args = [
            "-netdev", f"socket,id={netid},mcast={addr}:{port}",
            "-device", f"virtio-net-pci,netdev={netid}",
        ]
        for arg in iso_args:
            if arg not in cfg.extra_args:
                cfg.extra_args.append(arg)
        if vm_name not in net["members"]:
            net["members"].append(vm_name)
            self._save()
        cfg.save()
        return {"success": True, "message": f"VM '{vm_name}' added to isolated network '{net_name}'."}


# ─────────────────────────────────────────────
#  MAIN MANAGER
# ─────────────────────────────────────────────

class QemuManager:
    def __init__(self):
        os.makedirs(VM_BASE_DIR, exist_ok=True)
        self._state    = VMState()
        self._procs:   Dict[str, subprocess.Popen] = {}
        self.iso_nets  = IsolatedNetManager()
        # Reconnect any VMs that were running before this session
        self._reconnect_running()

    # ── RECONNECT ────────────────────────────────────────────

    def _reconnect_running(self):
        """Reconnect to VMs that survived a terminal restart."""
        for name, pid in self._state.all_running().items():
            try:
                p = psutil.Process(pid)
                # Wrap in a fake Popen-compatible object
                self._procs[name] = _PsutilProcWrapper(p)
            except psutil.NoSuchProcess:
                self._state.set_stopped(name)

    # ── DISCOVERY ────────────────────────────────────────────

    def list_vms(self) -> List[Dict[str, Any]]:
        vms = []
        if not os.path.isdir(VM_BASE_DIR):
            return vms
        for name in sorted(os.listdir(VM_BASE_DIR)):
            if name.startswith("_"):
                continue
            vm_dir   = os.path.join(VM_BASE_DIR, name)
            cfg_path = os.path.join(vm_dir, "config.json")
            if not os.path.isfile(cfg_path):
                continue
            try:
                cfg = MachineConfig.load(name)
            except Exception as e:
                vms.append({"name": name, "error": str(e)})
                continue
            status = self.vm_status(name)
            vms.append({
                "name":        name,
                "id":          cfg.vm_id,
                "description": cfg.description,
                "os":          cfg.os_name or cfg.os_type,
                "cpu_cores":   cfg.cpu_cores,
                "memory_mb":   cfg.memory_mb,
                "disks":       len(cfg.disks),
                "status":      status["state"],
            })
        return vms

    def scan_isos(self) -> List[Dict[str, str]]:
        """Scan common directories for ISO files — rebuilds dir list fresh each call."""
        found = []
        seen  = set()
        for d in _build_iso_search_dirs():
            if not os.path.isdir(d):
                continue
            for f in sorted(os.listdir(d)):
                if not f.lower().endswith(".iso"):
                    continue
                full = os.path.join(d, f)
                if full in seen:
                    continue
                seen.add(full)
                try:
                    size_gb = round(os.path.getsize(full) / 1024**3, 1)
                except OSError:
                    size_gb = 0
                found.append({"name": f, "path": full, "size_gb": size_gb})
        return found

    # ── CREATE ────────────────────────────────────────────────

    def create_vm(self, config: MachineConfig, force: bool = False) -> Dict[str, Any]:
        vm_dir = config.get_vm_dir()
        if os.path.exists(vm_dir) and not force:
            return {"success": False, "error": f"VM '{config.name}' already exists. Use force=True to overwrite."}

        os.makedirs(vm_dir, exist_ok=True)
        config = apply_os_hints(config)

        # UEFI VARS — search all known paths, match size to CODE file
        if config.uefi and config.bios in ("ovmf", "ovmf_ms"):
            vars_dst = os.path.join(vm_dir, "OVMF_VARS.fd")
            if not os.path.exists(vars_dst):
                # Build prioritised search list — match 4M/standard to CODE file
                code_path = OVMF.get("code", "")
                prefer_4m = "4M" in (code_path or "")

                if config.bios == "ovmf_ms":
                    search = [
                        OVMF.get("ms_vars"),
                        "/usr/share/OVMF/OVMF_VARS_4M.ms.fd",
                        "/usr/share/OVMF/OVMF_VARS_4M.snakeoil.fd",
                        "/usr/share/OVMF/OVMF_VARS.ms.fd",
                        "/usr/share/edk2-ovmf/x64/OVMF_VARS.secboot.fd",
                    ]
                elif prefer_4m:
                    search = [
                        "/usr/share/OVMF/OVMF_VARS_4M.fd",
                        OVMF.get("vars"),
                        "/usr/share/OVMF/OVMF_VARS.fd",
                        "/usr/share/edk2/ovmf/OVMF_VARS.fd",
                        "/usr/share/edk2-ovmf/x64/OVMF_VARS.fd",
                        "/usr/share/ovmf/x64/OVMF_VARS.fd",
                        "/usr/share/qemu/ovmf-x86_64-vars.bin",
                    ]
                else:
                    search = [
                        OVMF.get("vars"),
                        "/usr/share/OVMF/OVMF_VARS.fd",
                        "/usr/share/OVMF/OVMF_VARS_4M.fd",
                        "/usr/share/edk2/ovmf/OVMF_VARS.fd",
                        "/usr/share/edk2-ovmf/x64/OVMF_VARS.fd",
                        "/usr/share/ovmf/x64/OVMF_VARS.fd",
                        "/usr/share/qemu/ovmf-x86_64-vars.bin",
                    ]

                vars_template = next((p for p in search if p and os.path.exists(p)), None)
                if vars_template:
                    shutil.copy2(vars_template, vars_dst)
                    # Log what we used so debugging is easy
                    print(f"  [OVMF] Copied VARS from: {vars_template}")
                else:
                    # No VARS found anywhere — fall back to SeaBIOS
                    print(f"  [OVMF] WARNING: No VARS file found — falling back to SeaBIOS")
                    config.bios = "seabios"
                    config.uefi = False
                    vars_dst    = None

            if vars_dst and os.path.exists(vars_dst):
                config.uefi_vars = vars_dst

        # Auto port assignment
        used_vnc   = self._used_ports("vnc")
        used_spice = self._used_ports("spice")
        if config.display == "vnc" and not config.vnc_port:
            config.vnc_port = _next_free_port(VNC_PORT_START, used_vnc)
        if config.display == "spice" and not config.spice_port:
            config.spice_port = _next_free_port(SPICE_PORT_START, used_spice)

        # Create disk images
        for disk in config.disks:
            disk_path = os.path.expanduser(disk.path)
            if not os.path.exists(disk_path):
                os.makedirs(os.path.dirname(disk_path), exist_ok=True)
                result = subprocess.run(
                    ["qemu-img", "create", "-f", disk.format, disk_path, f"{disk.size_gb}G"],
                    capture_output=True, text=True
                )
                if result.returncode != 0:
                    return {"success": False, "error": f"qemu-img failed: {result.stderr}"}

        config.save()
        return {
            "success": True,
            "name":    config.name,
            "vm_dir":  vm_dir,
            "bios":    config.bios,
            "uefi":    config.uefi,
            "message": f"VM '{config.name}' created successfully.",
        }

    # ── CLONE ─────────────────────────────────────────────────

    def clone_vm(self, source_name: str, new_name: str) -> Dict[str, Any]:
        """Clone an existing VM — copies config and disk images."""
        if self._is_running(source_name):
            return {"success": False, "error": "Stop the source VM before cloning."}
        try:
            src_cfg = MachineConfig.load(source_name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

        new_vm_dir = os.path.join(VM_BASE_DIR, new_name)
        if os.path.exists(new_vm_dir):
            return {"success": False, "error": f"VM '{new_name}' already exists."}
        os.makedirs(new_vm_dir, exist_ok=True)

        # Clone disks using qemu-img (copy-on-write backing)
        new_disks = []
        for i, disk in enumerate(src_cfg.disks):
            src_path = os.path.expanduser(disk.path)
            new_path = os.path.join(new_vm_dir, f"disk{i}.{disk.format}")
            if os.path.exists(src_path):
                result = subprocess.run(
                    ["qemu-img", "create", "-f", "qcow2",
                     "-b", src_path, "-F", disk.format, new_path],
                    capture_output=True, text=True
                )
                if result.returncode != 0:
                    shutil.rmtree(new_vm_dir)
                    return {"success": False, "error": f"Disk clone failed: {result.stderr}"}
            new_disk = DiskConfig(
                path=new_path, size_gb=disk.size_gb,
                format="qcow2", bus=disk.bus,
            )
            new_disks.append(new_disk)

        # Copy UEFI VARS
        src_vars = os.path.join(src_cfg.get_vm_dir(), "OVMF_VARS.fd")
        if os.path.exists(src_vars):
            shutil.copy2(src_vars, os.path.join(new_vm_dir, "OVMF_VARS.fd"))

        # Build new config
        import uuid as _uuid
        src_cfg.name    = new_name
        src_cfg.vm_id   = str(_uuid.uuid4())[:8]
        src_cfg.disks   = new_disks
        src_cfg.uefi_vars = os.path.join(new_vm_dir, "OVMF_VARS.fd")

        # Give each NIC a fresh MAC
        for net in src_cfg.networks:
            net.mac = None
            net.__post_init__()

        src_cfg.save()
        return {
            "success": True,
            "message": f"VM '{source_name}' cloned to '{new_name}'.",
            "new_vm":  new_name,
        }

    # ── LAUNCH ────────────────────────────────────────────────

    def launch_vm(self, name: str, display: Optional[str] = None,
                  dry_run: bool = False) -> Dict[str, Any]:
        _qemu_version_warn()
        try:
            config = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

        if self._is_running(name):
            return {"success": False, "error": f"VM '{name}' is already running."}

        if display:
            config.display = display

        builder = QemuArgBuilder(config)
        cmd     = builder.build()
        cmd_str = " ".join(cmd)

        if dry_run:
            return {
                "success":   True,
                "dry_run":   True,
                "command":   cmd_str,
                "message":   "Dry run — command not executed.",
            }

        log_path = os.path.join(config.get_vm_dir(), "launch.log")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=open(log_path, "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError:
            return {"success": False, "error": f"{config.qemu_binary} not found. Check QEMU installation."}
        except Exception as e:
            return {"success": False, "error": str(e)}

        self._procs[name] = proc
        self._state.set_running(name, proc.pid)

        if config.cpu_pinning:
            time.sleep(1)
            self._apply_cpu_pinning(proc.pid, config.cpu_pinning)

        return {
            "success": True,
            "name":    name,
            "pid":     proc.pid,
            "display": config.display,
            "message": f"VM '{name}' launched (PID {proc.pid}).",
        }

    # ── STOP ──────────────────────────────────────────────────

    def stop_vm(self, name: str, force: bool = False) -> Dict[str, Any]:
        if not self._is_running(name):
            return {"success": False, "error": f"VM '{name}' is not running."}

        if not force:
            try:
                cfg = MachineConfig.load(name)
                qmp = QMPClient(cfg.qmp_socket)
                qmp.connect()
                qmp.execute("system_powerdown")
                qmp.close()
                for _ in range(30):
                    if not self._is_running(name):
                        break
                    time.sleep(1)
            except Exception:
                pass

        proc = self._procs.get(name)
        if proc:
            try:
                if hasattr(proc, "terminate"):
                    proc.terminate()
                    time.sleep(2)
                    if hasattr(proc, "poll") and proc.poll() is None:
                        proc.kill()
                else:
                    # psutil wrapper
                    proc.terminate()
            except Exception:
                pass

        self._procs.pop(name, None)
        self._state.set_stopped(name)
        return {"success": True, "name": name, "message": f"VM '{name}' stopped."}

    def stop_all(self) -> Dict[str, Any]:
        results = {}
        for name in list(self._procs.keys()):
            results[name] = self.stop_vm(name)
        return results

    # ── STATUS ────────────────────────────────────────────────

    def vm_status(self, name: str) -> Dict[str, Any]:
        running = self._is_running(name)
        pid     = self._state.get_pid(name) if running else None

        status = {"name": name, "state": "running" if running else "stopped", "pid": pid}

        if running and pid:
            try:
                p = psutil.Process(pid)
                status["cpu_percent"] = p.cpu_percent(interval=0.5)
                mem = p.memory_info()
                status["rss_mb"]   = round(mem.rss / 1024**2, 1)
                status["uptime_s"] = int(time.time() - p.create_time())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            try:
                cfg = MachineConfig.load(name)
                qmp = QMPClient(cfg.qmp_socket)
                qmp.connect(timeout=2)
                info = qmp.execute("query-status")
                status["qemu_status"] = info.get("return", {}).get("status", "unknown")
                qmp.close()
            except Exception:
                pass

        return status

    # ── MONITORING ────────────────────────────────────────────

    def monitor_vm(self, name: str) -> Dict[str, Any]:
        status = self.vm_status(name)
        if status["state"] != "running":
            return status

        pid    = status.get("pid")
        report = dict(status)
        report["timestamp"] = datetime.now().isoformat()

        try:
            p = psutil.Process(pid)
            report["cpu_times"]    = p.cpu_times()._asdict()
            report["cpu_affinity"] = p.cpu_affinity()
            try:
                io = p.io_counters()
                report["disk_io"] = {
                    "read_mb":    round(io.read_bytes / 1024**2, 2),
                    "write_mb":   round(io.write_bytes / 1024**2, 2),
                    "read_count":  io.read_count,
                    "write_count": io.write_count,
                }
            except psutil.AccessDenied:
                pass
            try:
                report["open_files"] = len(p.open_files())
            except psutil.AccessDenied:
                pass
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            report["error"] = str(e)

        try:
            cfg = MachineConfig.load(name)
            qmp = QMPClient(cfg.qmp_socket)
            qmp.connect(timeout=2)
            bs = qmp.execute("query-blockstats")
            if "return" in bs:
                report["block_stats"] = [
                    {
                        "device":   b.get("device", "?"),
                        "rd_bytes": b.get("stats", {}).get("rd_bytes", 0),
                        "wr_bytes": b.get("stats", {}).get("wr_bytes", 0),
                    }
                    for b in bs["return"]
                ]
            qmp.close()
        except Exception:
            pass

        return report

    def monitor_all(self) -> Dict[str, Any]:
        results = {}
        for name in list(self._procs.keys()):
            results[name] = self.monitor_vm(name)
        for vm in self.list_vms():
            if vm["name"] not in results and vm.get("status") == "running":
                results[vm["name"]] = self.monitor_vm(vm["name"])
        return results

    # ── DISPLAY / SHELL ───────────────────────────────────────

    def open_display(self, name: str) -> Dict[str, Any]:
        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}
        if not self._is_running(name):
            return {"success": False, "error": f"VM '{name}' is not running."}

        if cfg.display == "spice":
            port = cfg.spice_port or 5930
            for viewer in ["remote-viewer", "spicy"]:
                if shutil.which(viewer):
                    subprocess.Popen([viewer, f"spice://localhost:{port}"])
                    return {"success": True, "message": f"Opened SPICE display on port {port}."}
            return {"success": False, "error": "Install virt-viewer: sudo apt install virt-viewer"}
        elif cfg.display == "vnc":
            port = cfg.vnc_port or 5900
            for viewer in ["vncviewer", "tigervnc", "xtigervncviewer"]:
                if shutil.which(viewer):
                    subprocess.Popen([viewer, f"localhost:{port}"])
                    return {"success": True, "message": f"Opened VNC display on port {port}."}
            return {"success": False, "error": "Install VNC viewer: sudo apt install tigervnc-viewer"}
        else:
            return {"success": True, "message": f"VM uses {cfg.display} — window should already be open."}

    def open_shell(self, name: str) -> Dict[str, Any]:
        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}
        if not self._is_running(name):
            return {"success": False, "error": f"VM '{name}' is not running."}

        serial_sock = os.path.join(cfg.get_vm_dir(), "serial.sock")
        if not os.path.exists(serial_sock):
            return {"success": False, "error": f"Serial socket not found: {serial_sock}"}

        for term in ["gnome-terminal", "xterm", "konsole", "lxterminal", "xfce4-terminal"]:
            if shutil.which(term):
                cmd = [term, "--", "socat", "-", f"UNIX-CONNECT:{serial_sock}"] \
                      if term == "gnome-terminal" else \
                      [term, "-e", f"socat - UNIX-CONNECT:{serial_sock}"]
                subprocess.Popen(cmd)
                return {"success": True, "message": f"Opened serial console in {term}."}
        return {"success": False, "error": "No terminal emulator found."}

    # ── DISK ──────────────────────────────────────────────────

    def resize_disk(self, name: str, disk_index: int, new_size_gb: int) -> Dict[str, Any]:
        if self._is_running(name):
            return {"success": False, "error": "Stop the VM before resizing."}
        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}
        if disk_index >= len(cfg.disks):
            return {"success": False, "error": f"Disk index {disk_index} out of range."}

        disk_path = os.path.expanduser(cfg.disks[disk_index].path)
        result    = subprocess.run(
            ["qemu-img", "resize", disk_path, f"{new_size_gb}G"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return {"success": False, "error": result.stderr}

        cfg.disks[disk_index].size_gb = new_size_gb
        cfg.save()
        return {"success": True, "message": f"Disk {disk_index} resized to {new_size_gb}GB. Remember to expand the partition inside the guest."}

    # ── SNAPSHOTS ─────────────────────────────────────────────

    def snapshot_create(self, name: str, snap_name: str) -> Dict[str, Any]:
        if not self._is_running(name):
            return {"success": False, "error": f"VM '{name}' must be running for a live snapshot."}
        try:
            cfg = MachineConfig.load(name)
            qmp = QMPClient(cfg.qmp_socket)
            qmp.connect()
            qmp.execute("savevm", {"tag": snap_name})
            qmp.close()
            return {"success": True, "message": f"Snapshot '{snap_name}' created."}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def snapshot_list(self, name: str) -> Dict[str, Any]:
        try:
            cfg = MachineConfig.load(name)
            if not cfg.disks:
                return {"success": False, "error": "No disks."}
            disk_path = os.path.expanduser(cfg.disks[0].path)
            result    = subprocess.run(
                ["qemu-img", "snapshot", "-l", disk_path],
                capture_output=True, text=True
            )
            # Parse into structured list
            snaps = []
            for line in result.stdout.splitlines()[2:]:  # skip header
                parts = line.split()
                if len(parts) >= 4:
                    snaps.append({"id": parts[0], "tag": parts[1], "vm_size": parts[2], "date": parts[3]})
            return {"success": True, "snapshots": snaps, "raw": result.stdout}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def snapshot_restore(self, name: str, snap_name: str) -> Dict[str, Any]:
        """Restore a snapshot (VM must be stopped for offline restore)."""
        if self._is_running(name):
            # Live restore via QMP
            try:
                cfg = MachineConfig.load(name)
                qmp = QMPClient(cfg.qmp_socket)
                qmp.connect()
                qmp.execute("loadvm", {"tag": snap_name})
                qmp.close()
                return {"success": True, "message": f"Snapshot '{snap_name}' restored (live)."}
            except Exception as e:
                return {"success": False, "error": str(e)}
        else:
            # Offline restore via qemu-img
            try:
                cfg       = MachineConfig.load(name)
                disk_path = os.path.expanduser(cfg.disks[0].path)
                result    = subprocess.run(
                    ["qemu-img", "snapshot", "-a", snap_name, disk_path],
                    capture_output=True, text=True
                )
                if result.returncode != 0:
                    return {"success": False, "error": result.stderr}
                return {"success": True, "message": f"Snapshot '{snap_name}' restored (offline)."}
            except Exception as e:
                return {"success": False, "error": str(e)}

    def snapshot_delete(self, name: str, snap_name: str) -> Dict[str, Any]:
        try:
            cfg       = MachineConfig.load(name)
            disk_path = os.path.expanduser(cfg.disks[0].path)
            result    = subprocess.run(
                ["qemu-img", "snapshot", "-d", snap_name, disk_path],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                return {"success": False, "error": result.stderr}
            return {"success": True, "message": f"Snapshot '{snap_name}' deleted."}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── RESOURCE LIMITS ───────────────────────────────────────

    def set_resource_limits(self, name: str,
                             cpu_percent: Optional[int] = None,
                             memory_mb: Optional[int] = None) -> Dict[str, Any]:
        """
        Apply resource limits to a running VM.
        cpu_percent: 0-100 per core (uses cpulimit if available, else cgroups)
        memory_mb:   balloon the guest memory down via QMP
        """
        if not self._is_running(name):
            return {"success": False, "error": f"VM '{name}' is not running."}

        results = {}
        pid = self._state.get_pid(name)

        # CPU limit
        if cpu_percent is not None:
            if shutil.which("cpulimit"):
                subprocess.Popen(
                    ["cpulimit", "-p", str(pid), "-l", str(cpu_percent), "-b"],
                    start_new_session=True,
                )
                results["cpu_limit"] = f"cpulimit set to {cpu_percent}% (PID {pid})"
            else:
                # Try cgroups v2
                cgroup_path = f"/sys/fs/cgroup/qemu-api-{name}"
                try:
                    os.makedirs(cgroup_path, exist_ok=True)
                    quota  = int(cpu_percent * 1000)
                    period = 100000
                    with open(f"{cgroup_path}/cpu.max", "w") as f:
                        f.write(f"{quota} {period}\n")
                    with open(f"{cgroup_path}/cgroup.procs", "w") as f:
                        f.write(str(pid))
                    results["cpu_limit"] = f"cgroup cpu.max set to {cpu_percent}%"
                except PermissionError:
                    results["cpu_limit_error"] = "Need sudo for cgroups. Install cpulimit instead: sudo apt install cpulimit"

        # Memory balloon
        if memory_mb is not None:
            try:
                cfg = MachineConfig.load(name)
                qmp = QMPClient(cfg.qmp_socket)
                qmp.connect()
                qmp.execute("balloon", {"value": memory_mb * 1024 * 1024})
                qmp.close()
                results["memory_balloon"] = f"Ballooned to {memory_mb}MB"
            except Exception as e:
                results["memory_balloon_error"] = str(e)

        return {"success": True, "name": name, "results": results}

    # ── CONFIG ────────────────────────────────────────────────

    def show_config(self, name: str) -> Dict[str, Any]:
        try:
            cfg = MachineConfig.load(name)
            return {"success": True, "config": cfg.to_dict()}
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

    def update_config(self, name: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        if self._is_running(name):
            return {"success": False, "error": "Stop the VM before updating config."}
        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

        changed = []
        for key, value in updates.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
                changed.append(key)
            else:
                return {"success": False, "error": f"Unknown config field: '{key}'"}
        cfg.save()
        return {"success": True, "message": f"Updated {changed} for '{name}'."}

    def delete_vm(self, name: str, delete_disks: bool = False) -> Dict[str, Any]:
        if self._is_running(name):
            return {"success": False, "error": "Stop the VM before deleting."}
        vm_dir = os.path.join(VM_BASE_DIR, name)
        if not os.path.exists(vm_dir):
            return {"success": False, "error": f"VM '{name}' not found."}
        if delete_disks:
            try:
                cfg = MachineConfig.load(name)
                for disk in cfg.disks:
                    p = os.path.expanduser(disk.path)
                    if os.path.exists(p):
                        os.remove(p)
            except Exception:
                pass
        shutil.rmtree(vm_dir)
        self._state.set_stopped(name)
        return {"success": True, "message": f"VM '{name}' deleted."}

    def get_vm_logs(self, name: str, lines: int = 50) -> Dict[str, Any]:
        """
        Read the VM launch log, parse QEMU error messages, and
        return a structured failure report the AI can explain clearly.
        """
        vm_dir   = os.path.join(VM_BASE_DIR, name)
        log_path = os.path.join(vm_dir, "launch.log")
        result   = {
            "name":       name,
            "log_path":   log_path,
            "log_exists": os.path.exists(log_path),
            "raw_tail":   "",
            "errors":     [],
            "warnings":   [],
            "last_line":  "",
            "diagnosis":  "",
            "suggestions": [],
        }

        # ── Read log ─────────────────────────────────────────
        if os.path.exists(log_path):
            with open(log_path, "r", errors="replace") as f:
                all_lines = f.readlines()
            tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
            result["raw_tail"]  = "".join(tail)
            result["last_line"] = tail[-1].strip() if tail else ""
            result["total_log_lines"] = len(all_lines)

            # Parse known QEMU error patterns
            error_patterns = [
                ("qemu: could not load",          "Boot image or kernel file not found"),
                ("no bootable device",             "No bootable disk — check ISO/disk image path"),
                ("failed to open",                 "Could not open a file — check paths in config"),
                ("permission denied",              "Permission denied — check file permissions"),
                ("address already in use",         "Port conflict — VNC/SPICE port already in use"),
                ("kvm: permission denied",         "KVM permission denied — run: sudo usermod -aG kvm $USER"),
                ("kvm: no such file",              "KVM not available — check BIOS VT-x/AMD-V setting"),
                ("could not initialize kvm",       "KVM init failed — may conflict with another hypervisor"),
                ("unsupported machine type",       "Invalid machine type for this QEMU binary"),
                ("invalid accelerator",            "Accelerator not supported — KVM on wrong arch"),
                ("accel=kvm not supported",        "KVM not supported for this guest architecture"),
                ("cannot set up guest memory",     "Not enough RAM — reduce memory_mb or free host RAM"),
                ("hugepages",                      "Hugepages not allocated — run: sudo sysctl vm.nr_hugepages=2048"),
                ("no such file or directory",      "A required file is missing — check disk/ISO/firmware paths"),
                ("invalid parameter",              "Invalid QEMU argument — check machine type and CPU flags"),
                ("unknown cpu model",              "CPU model not supported by this QEMU version"),
                ("pflash",                         "OVMF/UEFI firmware file not found or wrong format"),
                ("virtio",                         "Virtio device error — may be incompatible with machine type"),
                ("sdl_init failed",                "SDL display init failed — check DISPLAY env var"),
                ("gtk_init_check failed",          "GTK display init failed — check DISPLAY env var"),
                ("socket",                         "Socket error — another instance may be running"),
                ("address family not supported",   "Network config error — check bridge/network settings"),
                ("tap: failed to connect",         "TAP network error — bridge may not exist"),
                ("if=sd",                          "SD card interface error — check disk image format"),
                ("raspi",                          "Raspberry Pi machine error — needs kernel8.img + dtb file"),
                ("arm",                            "ARM machine error — check qemu-system-aarch64 is installed"),
                ("out of memory",                  "Host is out of memory — reduce VM memory_mb"),
                ("segfault",                       "QEMU crashed (segfault) — try updating qemu-system"),
                ("aborted",                        "QEMU aborted — likely an incompatible argument combination"),
            ]

            for line in tail:
                line_lower = line.lower()
                for pattern, meaning in error_patterns:
                    if pattern in line_lower:
                        entry = {"line": line.strip(), "meaning": meaning}
                        if entry not in result["errors"]:
                            result["errors"].append(entry)

        else:
            # Log doesn't exist — QEMU crashed before writing anything
            result["diagnosis"] = (
                "No log file found — QEMU crashed immediately before writing output. "
                "This usually means the QEMU binary is wrong, a required argument is "
                "completely invalid, or the binary couldn't be executed at all."
            )

        # ── Check config for common misconfigs ───────────────
        try:
            cfg = MachineConfig.load(name)

            # Wrong binary for arch
            if cfg.machine_arch in ("aarch64", "arm") and "x86_64" in cfg.qemu_binary:
                result["errors"].append({
                    "line": f"qemu_binary = {cfg.qemu_binary}",
                    "meaning": "Wrong QEMU binary — ARM machine needs qemu-system-aarch64"
                })

            # KVM on wrong arch
            if cfg.kvm and cfg.machine_arch in ("aarch64", "arm"):
                result["errors"].append({
                    "line": "kvm=True on ARM guest",
                    "meaning": "KVM cannot be used for ARM guests on an x86 host"
                })

            # Hugepages without allocation
            if cfg.hugepages:
                try:
                    with open("/proc/sys/vm/nr_hugepages") as f:
                        nr = int(f.read().strip())
                    if nr == 0:
                        result["errors"].append({
                            "line": "hugepages=True but nr_hugepages=0",
                            "meaning": "Hugepages requested but not allocated on host — run: sudo sysctl vm.nr_hugepages=2048"
                        })
                except Exception:
                    pass

            # Missing ISO
            if cfg.iso_path and not os.path.exists(cfg.iso_path):
                result["errors"].append({
                    "line": f"iso_path = {cfg.iso_path}",
                    "meaning": f"ISO file not found: {cfg.iso_path}"
                })

            # Missing disk
            for i, disk in enumerate(cfg.disks):
                dp = os.path.expanduser(disk.path)
                if not os.path.exists(dp):
                    result["errors"].append({
                        "line": f"disk[{i}].path = {disk.path}",
                        "meaning": f"Disk image not found: {dp}"
                    })

            # OVMF missing but requested
            if cfg.bios in ("ovmf", "ovmf_ms") and cfg.uefi:
                from qemu_config import OVMF
                if not OVMF["available"]:
                    result["errors"].append({
                        "line": "bios=ovmf but OVMF not installed",
                        "meaning": "UEFI firmware not found — run: sudo apt install ovmf"
                    })

            # Invalid machine type — profile name used instead of q35/pc
            valid_machine_types = {
                "q35", "pc", "pc-i440fx", "microvm", "virt",
                "raspi3b", "raspi2b", "raspi0",
            }
            mt = cfg.machine_type.lower().split(",")[0].strip()
            if mt not in valid_machine_types and not mt.startswith("pc-"):
                result["errors"].append({
                    "line": f"machine_type = {cfg.machine_type}",
                    "meaning": (
                        f"'{cfg.machine_type}' is not a valid QEMU machine type — "
                        "it looks like a profile name was used by mistake. "
                        "Should be 'q35' for modern x86 or 'pc' for legacy."
                    )
                })

            # ISO architecture mismatch
            if cfg.iso_path:
                iso_lower = os.path.basename(cfg.iso_path).lower()
                is_iso_arm = any(k in iso_lower for k in ("arm64", "aarch64", "arm_", "_arm"))
                is_iso_x86 = any(k in iso_lower for k in ("amd64", "x86_64", "x64", "i386", "i686"))
                is_vm_arm  = cfg.machine_arch in ("aarch64", "arm")
                is_vm_x86  = cfg.machine_arch == "x86_64"

                if is_iso_arm and is_vm_x86:
                    result["errors"].append({
                        "line": f"iso={os.path.basename(cfg.iso_path)}, arch={cfg.machine_arch}",
                        "meaning": (
                            "Architecture mismatch — ARM64 ISO cannot boot on an x86_64 VM. "
                            "Use an x86_64 ISO (e.g. Win11 x64 edition) or change the VM "
                            "to machine_arch=aarch64 with qemu-system-aarch64."
                        )
                    })
                elif is_iso_x86 and is_vm_arm:
                    result["errors"].append({
                        "line": f"iso={os.path.basename(cfg.iso_path)}, arch={cfg.machine_arch}",
                        "meaning": (
                            "Architecture mismatch — x86_64 ISO cannot boot on an ARM VM. "
                            "Use an ARM64 ISO instead."
                        )
                    })

            result["config_summary"] = {
                "qemu_binary":    cfg.qemu_binary,
                "machine_type":   cfg.machine_type,
                "machine_arch":   cfg.machine_arch,
                "kvm":            cfg.kvm,
                "bios":           cfg.bios,
                "hugepages":      cfg.hugepages,
                "iso_path":       cfg.iso_path,
                "display":        cfg.display,
                "memory_mb":      cfg.memory_mb,
                "disk_paths":     [d.path for d in cfg.disks],
            }
        except FileNotFoundError:
            result["config_error"] = f"No config found for VM '{name}'"
        except Exception as e:
            result["config_error"] = str(e)

        # ── Build diagnosis summary ───────────────────────────
        if result["errors"] and not result["diagnosis"]:
            top = result["errors"][0]
            result["diagnosis"] = top["meaning"]

        # ── Build suggestions ─────────────────────────────────
        suggestions = []
        for err in result["errors"]:
            m = err["meaning"].lower()
            if "hugepages" in m:
                suggestions.append("sudo sysctl vm.nr_hugepages=2048")
            if "kvm permission" in m:
                suggestions.append("sudo usermod -aG kvm $USER  (then log out and back in)")
            if "arm" in m and "binary" in m:
                suggestions.append("sudo apt install qemu-system-arm")
            if "ovmf" in m or "uefi" in m:
                suggestions.append("sudo apt install ovmf")
            if "no bootable" in m:
                suggestions.append("Check iso_path in VM config — run: qemu-api config " + name)
            if "port" in m or "address already" in m:
                suggestions.append("Change vnc_port or spice_port in VM config to a free port")
            if "display" in m:
                suggestions.append("Check DISPLAY env var: echo $DISPLAY  (should be :0 or :1)")
            if "not a valid qemu machine type" in m or "profile name" in m:
                suggestions.append(
                    f"Fix machine_type: run: qemu-api cmd {name} '' — or delete and recreate the VM with machine_type=q35"
                )
            if "architecture mismatch" in m and "arm64" in m:
                suggestions.append(
                    "Download x86_64 Windows 11 ISO from Microsoft: "
                    "https://www.microsoft.com/software-download/windows11"
                )
            if "architecture mismatch" in m:
                suggestions.append(
                    f"Fix: delete the VM and recreate it — the ISO arch and VM arch must match"
                )
        result["suggestions"] = list(dict.fromkeys(suggestions))  # deduplicate

        return result

    def send_monitor_cmd(self, name: str, cmd: str) -> Dict[str, Any]:
        try:
            cfg      = MachineConfig.load(name)
            sock_path = cfg.monitor_socket
            if not os.path.exists(sock_path):
                return {"success": False, "error": "Monitor socket not found."}
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(sock_path)
            time.sleep(0.3)
            s.recv(4096)
            s.sendall((cmd + "\n").encode())
            time.sleep(0.3)
            response = s.recv(8192).decode()
            s.close()
            return {"success": True, "output": response}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def print_command(self, name: str, dry_run: bool = False) -> Dict[str, Any]:
        try:
            cfg     = MachineConfig.load(name)
            builder = QemuArgBuilder(cfg)
            cmd     = builder.build()
            return {"success": True, "command": " ".join(cmd)}
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

    # ── ISOLATED NETWORKS (pass-through) ─────────────────────

    def create_network(self, net_name: str) -> Dict[str, Any]:
        return self.iso_nets.create_network(net_name)

    def delete_network(self, net_name: str) -> Dict[str, Any]:
        return self.iso_nets.delete_network(net_name)

    def list_networks(self) -> List[Dict]:
        return self.iso_nets.list_networks()

    def add_vm_to_network(self, net_name: str, vm_name: str) -> Dict[str, Any]:
        return self.iso_nets.add_vm_to_network(net_name, vm_name)

    # ── HELPERS ───────────────────────────────────────────────

    def _is_running(self, name: str) -> bool:
        proc = self._procs.get(name)
        if proc:
            if hasattr(proc, "poll"):
                if proc.poll() is None:
                    return True
            else:
                try:
                    if proc.is_running():
                        return True
                except Exception:
                    pass
        # Fall back to state file
        pid = self._state.get_pid(name)
        if pid:
            try:
                p = psutil.Process(pid)
                if p.is_running():
                    self._procs[name] = _PsutilProcWrapper(p)
                    return True
            except psutil.NoSuchProcess:
                pass
        self._procs.pop(name, None)
        self._state.set_stopped(name)
        return False

    def _used_ports(self, kind: str) -> List[int]:
        ports = []
        for name in os.listdir(VM_BASE_DIR):
            if name.startswith("_"):
                continue
            cfg_path = os.path.join(VM_BASE_DIR, name, "config.json")
            if os.path.exists(cfg_path):
                try:
                    with open(cfg_path) as f:
                        data = json.load(f)
                    if kind == "vnc" and data.get("vnc_port"):
                        ports.append(data["vnc_port"])
                    if kind == "spice" and data.get("spice_port"):
                        ports.append(data["spice_port"])
                except Exception:
                    pass
        return ports

    def _apply_cpu_pinning(self, pid: int, cpus: List[int]):
        cpu_list = ",".join(map(str, cpus))
        subprocess.run(["taskset", "-cp", cpu_list, str(pid)], capture_output=True)


# ─────────────────────────────────────────────
#  PSUTIL PROC WRAPPER
#  Makes a psutil.Process behave like subprocess.Popen
# ─────────────────────────────────────────────

class _PsutilProcWrapper:
    def __init__(self, proc: psutil.Process):
        self._proc = proc
        self.pid   = proc.pid

    def poll(self):
        try:
            return None if self._proc.is_running() else 0
        except psutil.NoSuchProcess:
            return 1

    def terminate(self):
        try:
            self._proc.terminate()
        except psutil.NoSuchProcess:
            pass

    def kill(self):
        try:
            self._proc.kill()
        except psutil.NoSuchProcess:
            pass

    def is_running(self):
        try:
            return self._proc.is_running()
        except psutil.NoSuchProcess:
            return False

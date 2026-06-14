"""
qemu_manager.py — VM Orchestration Layer

QemuManager is the single public façade for all VM lifecycle operations:
create, clone, launch, stop, status, monitor, snapshots, disk resize,
resource limits, network, display, shell, config, and log analysis.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

_CFG      = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_TIMEOUTS = _CFG["timeouts"]
_BUFFERS  = _CFG["buffers"]

import psutil

from .qemu_config import (
    DiskConfig, MachineConfig, NetworkConfig, OVMF, apply_os_hints,
)
from .qemu_arg_builder import (
    QemuArgBuilder, _build_iso_search_dirs, _next_free_port, _qemu_version_warn,
    SPICE_PORT_START, VNC_PORT_START,
)
from .qmp_client      import QMPClient
from .network_manager import IsolatedNetManager
from .vm_state        import VMState, _PsutilProcWrapper

VM_BASE_DIR = os.path.expanduser(_CFG["dirs"]["vm_base"])


class QemuManager:
    # Creates the VM base dir, initializes state and net managers, reconnects to surviving VMs.
    # In: nothing → Out: nothing
    def __init__(self):
        os.makedirs(VM_BASE_DIR, exist_ok=True)
        self._state   = VMState()
        self._procs:  Dict[str, subprocess.Popen] = {}
        self.iso_nets = IsolatedNetManager()
        self._reconnect_running()

    # ── Reconnect ──────────────────────────────────────────────────────────────

    # For each PID in state, attaches a _PsutilProcWrapper or cleans up dead entries.
    # In: nothing → Out: nothing
    def _reconnect_running(self):
        """Reconnect to VMs that survived a terminal restart."""
        for name, pid in self._state.all_running().items():
            try:
                p = psutil.Process(pid)
                self._procs[name] = _PsutilProcWrapper(p)
            except psutil.NoSuchProcess:
                self._state.set_stopped(name)

    # ── Discovery ──────────────────────────────────────────────────────────────

    # Scans ~/.qemu_vms/ and returns status info for every VM directory.
    # In: nothing → Out: List[dict]
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

    # Walks common directories to find .iso files and returns their names, paths, and sizes.
    # In: nothing → Out: List[dict]
    def scan_isos(self) -> List[Dict[str, str]]:
        """Scan common directories for ISO files."""
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

    # ── Create ─────────────────────────────────────────────────────────────────

    # Creates VM directory, copies OVMF VARS, assigns ports, runs qemu-img create for each disk.
    # In: MachineConfig, bool force → Out: dict with success
    def create_vm(self, config: MachineConfig, force: bool = False) -> Dict[str, Any]:
        vm_dir = config.get_vm_dir()
        if os.path.exists(vm_dir) and not force:
            return {"success": False, "error": f"VM '{config.name}' already exists. Use force=True to overwrite."}

        os.makedirs(vm_dir, exist_ok=True)
        config = apply_os_hints(config)

        # UEFI VARS — find, copy, and bind
        if config.uefi and config.bios in ("ovmf", "ovmf_ms"):
            vars_dst = os.path.join(vm_dir, "OVMF_VARS.fd")
            if not os.path.exists(vars_dst):
                code_path = OVMF.get("code", "")
                prefer_4m = "4M" in (code_path or "")

                _MACOS_OVMF = [
                    "/opt/homebrew/share/qemu/edk2-x86_64-vars.fd",
                    "/usr/local/share/qemu/edk2-x86_64-vars.fd",
                ]
                _WIN_OVMF = [
                    "C:/Program Files/qemu/share/edk2-x86_64-vars.fd",
                    "C:/Program Files (x86)/QEMU/share/edk2-x86_64-vars.fd",
                ]
                if config.bios == "ovmf_ms":
                    search = [
                        OVMF.get("ms_vars"),
                        "/usr/share/OVMF/OVMF_VARS_4M.ms.fd",
                        "/usr/share/OVMF/OVMF_VARS_4M.snakeoil.fd",
                        "/usr/share/OVMF/OVMF_VARS.ms.fd",
                        "/usr/share/edk2-ovmf/x64/OVMF_VARS.secboot.fd",
                        "/opt/homebrew/share/qemu/edk2-x86_64-secure-vars.fd",
                        "/usr/local/share/qemu/edk2-x86_64-secure-vars.fd",
                        "C:/Program Files/qemu/share/edk2-x86_64-secure-vars.fd",
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
                        *_MACOS_OVMF, *_WIN_OVMF,
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
                        *_MACOS_OVMF, *_WIN_OVMF,
                    ]

                vars_template = next((p for p in search if p and os.path.exists(p)), None)
                if vars_template:
                    shutil.copy2(vars_template, vars_dst)
                    print(f"  [OVMF] Copied VARS from: {vars_template}")
                else:
                    print("  [OVMF] WARNING: No VARS file found — falling back to SeaBIOS")
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
                    capture_output=True, text=True,
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

    # ── Clone ──────────────────────────────────────────────────────────────────

    # Creates a CoW qcow2 clone of each disk and copies the config under a new name/UUID.
    # In: str source_name, str new_name → Out: dict with success
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

        new_disks = []
        for i, disk in enumerate(src_cfg.disks):
            src_path = os.path.expanduser(disk.path)
            new_path = os.path.join(new_vm_dir, f"disk{i}.{disk.format}")
            if os.path.exists(src_path):
                result = subprocess.run(
                    ["qemu-img", "create", "-f", "qcow2", "-b", src_path, "-F", disk.format, new_path],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    shutil.rmtree(new_vm_dir)
                    return {"success": False, "error": f"Disk clone failed: {result.stderr}"}
            new_disks.append(DiskConfig(path=new_path, size_gb=disk.size_gb, format="qcow2", bus=disk.bus))

        src_vars = os.path.join(src_cfg.get_vm_dir(), "OVMF_VARS.fd")
        if os.path.exists(src_vars):
            shutil.copy2(src_vars, os.path.join(new_vm_dir, "OVMF_VARS.fd"))

        import uuid as _uuid
        src_cfg.name      = new_name
        src_cfg.vm_id     = str(_uuid.uuid4())[:8]
        src_cfg.disks     = new_disks
        src_cfg.uefi_vars = os.path.join(new_vm_dir, "OVMF_VARS.fd")
        for net in src_cfg.networks:
            net.mac = None
            net.__post_init__()

        src_cfg.save()
        return {"success": True, "message": f"VM '{source_name}' cloned to '{new_name}'.", "new_vm": new_name}

    # ── Launch ─────────────────────────────────────────────────────────────────

    # Builds the QEMU command, starts the process via Popen, records PID, applies CPU pinning.
    # In: str name, str? display, bool dry_run → Out: dict with success and pid
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

        cmd     = QemuArgBuilder(config).build()
        cmd_str = " ".join(cmd)

        if dry_run:
            return {"success": True, "dry_run": True, "command": cmd_str, "message": "Dry run — command not executed."}

        log_path = os.path.join(config.get_vm_dir(), "launch.log")
        try:
            _popen_kwargs: Dict[str, Any] = {
                "stdout": open(log_path, "a"),
                "stderr": subprocess.STDOUT,
            }
            if sys.platform == "win32":
                _popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                _popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(cmd, **_popen_kwargs)
        except FileNotFoundError:
            return {"success": False, "error": f"{config.qemu_binary} not found. Check QEMU installation."}
        except Exception as e:
            return {"success": False, "error": str(e)}

        self._procs[name] = proc
        self._state.set_running(name, proc.pid)

        if config.cpu_pinning:
            time.sleep(_TIMEOUTS["cpu_pinning_delay"])
            self._apply_cpu_pinning(proc.pid, config.cpu_pinning)

        return {"success": True, "name": name, "pid": proc.pid, "display": config.display,
                "message": f"VM '{name}' launched (PID {proc.pid})."}

    # ── Stop ───────────────────────────────────────────────────────────────────

    # Tries graceful QMP system_powerdown, then terminates/kills the process.
    # In: str name, bool force → Out: dict with success
    def stop_vm(self, name: str, force: bool = False) -> Dict[str, Any]:
        if not self._is_running(name):
            return {"success": False, "error": f"VM '{name}' is not running."}

        if not force:
            try:
                cfg = MachineConfig.load(name)
                qmp = QMPClient(cfg.get_qmp_socket())
                qmp.connect()
                qmp.execute("system_powerdown")
                qmp.close()
                for _ in range(_TIMEOUTS["stop_graceful"]):
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
                    proc.terminate()
            except Exception:
                pass

        self._procs.pop(name, None)
        self._state.set_stopped(name)
        return {"success": True, "name": name, "message": f"VM '{name}' stopped."}

    # Calls stop_vm on every tracked running VM.
    # In: nothing → Out: dict of results keyed by name
    def stop_all(self) -> Dict[str, Any]:
        return {name: self.stop_vm(name) for name in list(self._procs.keys())}

    # ── Status ─────────────────────────────────────────────────────────────────

    # Returns state, PID, CPU%, RSS, uptime, and QMP internal status for a VM.
    # In: str name → Out: dict
    def vm_status(self, name: str) -> Dict[str, Any]:
        running = self._is_running(name)
        pid     = self._state.get_pid(name) if running else None
        status  = {"name": name, "state": "running" if running else "stopped", "pid": pid}

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
                qmp = QMPClient(cfg.get_qmp_socket())
                qmp.connect(timeout=2)
                info = qmp.execute("query-status")
                status["qemu_status"] = info.get("return", {}).get("status", "unknown")
                qmp.close()
            except Exception:
                pass

        return status

    # ── Monitoring ─────────────────────────────────────────────────────────────

    # Deep resource report: CPU times, IO counters, open files, and QMP block stats.
    # In: str name → Out: dict
    def monitor_vm(self, name: str) -> Dict[str, Any]:
        status = self.vm_status(name)
        if status["state"] != "running":
            return status

        pid    = status.get("pid")
        report = dict(status)
        report["timestamp"] = __import__("datetime").datetime.now().isoformat()

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
            qmp = QMPClient(cfg.get_qmp_socket())
            qmp.connect(timeout=2)
            bs = qmp.execute("query-blockstats")
            if "return" in bs:
                report["block_stats"] = [
                    {"device": b.get("device","?"),
                     "rd_bytes": b.get("stats",{}).get("rd_bytes",0),
                     "wr_bytes": b.get("stats",{}).get("wr_bytes",0)}
                    for b in bs["return"]
                ]
            qmp.close()
        except Exception:
            pass

        return report

    # Returns monitor_vm results for all running VMs.
    # In: nothing → Out: dict keyed by VM name
    def monitor_all(self) -> Dict[str, Any]:
        results = {name: self.monitor_vm(name) for name in list(self._procs.keys())}
        for vm in self.list_vms():
            if vm["name"] not in results and vm.get("status") == "running":
                results[vm["name"]] = self.monitor_vm(vm["name"])
        return results

    # ── Display / Shell ────────────────────────────────────────────────────────

    # Launches remote-viewer (SPICE) or vncviewer for the VM's display.
    # In: str name → Out: dict with success
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
            if sys.platform == "darwin":
                subprocess.Popen(["open", f"spice://localhost:{port}"])
                return {"success": True, "message": f"SPICE on port {port}. Install virt-viewer for full support: brew install virt-viewer"}
            return {"success": False, "error": "Install virt-viewer: sudo apt install virt-viewer"}
        elif cfg.display == "vnc":
            port = cfg.vnc_port or 5900
            for viewer in ["vncviewer", "tigervnc", "xtigervncviewer"]:
                if shutil.which(viewer):
                    subprocess.Popen([viewer, f"localhost:{port}"])
                    return {"success": True, "message": f"Opened VNC display on port {port}."}
            if sys.platform == "darwin":
                subprocess.Popen(["open", f"vnc://localhost:{port}"])
                return {"success": True, "message": f"Opening VNC in Screen Sharing on port {port}."}
            if sys.platform == "win32":
                for viewer in ["tvnviewer", "vncviewer"]:
                    if shutil.which(viewer):
                        subprocess.Popen([viewer, f"localhost:{port}"])
                        return {"success": True, "message": f"Opened VNC display on port {port}."}
            return {"success": False, "error": "Install VNC viewer: sudo apt install tigervnc-viewer"}
        else:
            return {"success": True, "message": f"VM uses {cfg.display} — window should already be open."}

    # Opens a socat serial console in the first available terminal emulator.
    # In: str name → Out: dict with success
    def open_shell(self, name: str) -> Dict[str, Any]:
        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}
        if not self._is_running(name):
            return {"success": False, "error": f"VM '{name}' is not running."}

        if sys.platform == "win32":
            port = cfg.serial_tcp_port
            if not port:
                return {"success": False, "error": "Serial TCP port not configured — launch the VM first."}
            subprocess.Popen(["cmd", "/c", "start", "telnet", "127.0.0.1", str(port)])
            return {"success": True, "message": f"Opened serial console via telnet on port {port}."}

        serial_sock = os.path.join(cfg.get_vm_dir(), "serial.sock")
        if not os.path.exists(serial_sock):
            return {"success": False, "error": f"Serial socket not found: {serial_sock}"}

        if sys.platform == "darwin":
            script = f'tell app "Terminal" to do script "socat - UNIX-CONNECT:{serial_sock}"'
            subprocess.Popen(["osascript", "-e", script])
            return {"success": True, "message": "Opened serial console in Terminal.app."}

        for term in ["gnome-terminal", "xterm", "konsole", "lxterminal", "xfce4-terminal"]:
            if shutil.which(term):
                cmd = ([term, "--", "socat", "-", f"UNIX-CONNECT:{serial_sock}"]
                       if term == "gnome-terminal"
                       else [term, "-e", f"socat - UNIX-CONNECT:{serial_sock}"])
                subprocess.Popen(cmd)
                return {"success": True, "message": f"Opened serial console in {term}."}
        return {"success": False, "error": "No terminal emulator found. Install: sudo apt install xterm"}

    # ── Disk ───────────────────────────────────────────────────────────────────

    # Runs qemu-img resize on a stopped VM's disk and saves the updated config.
    # In: str name, int disk_index, int new_size_gb → Out: dict with success
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
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return {"success": False, "error": result.stderr}

        cfg.disks[disk_index].size_gb = new_size_gb
        cfg.save()
        return {"success": True, "message": f"Disk {disk_index} resized to {new_size_gb}GB. Remember to expand the partition inside the guest."}

    # ── Snapshots ──────────────────────────────────────────────────────────────

    # Sends savevm to a running VM via QMP.
    # In: str name, str snap_name → Out: dict with success
    def snapshot_create(self, name: str, snap_name: str) -> Dict[str, Any]:
        if not self._is_running(name):
            return {"success": False, "error": f"VM '{name}' must be running for a live snapshot."}
        try:
            cfg = MachineConfig.load(name)
            qmp = QMPClient(cfg.get_qmp_socket())
            qmp.connect()
            qmp.execute("savevm", {"tag": snap_name})
            qmp.close()
            return {"success": True, "message": f"Snapshot '{snap_name}' created."}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Runs qemu-img snapshot -l and parses the output table.
    # In: str name → Out: dict with snapshots list
    def snapshot_list(self, name: str) -> Dict[str, Any]:
        try:
            cfg = MachineConfig.load(name)
            if not cfg.disks:
                return {"success": False, "error": "No disks."}
            disk_path = os.path.expanduser(cfg.disks[0].path)
            result    = subprocess.run(
                ["qemu-img", "snapshot", "-l", disk_path],
                capture_output=True, text=True,
            )
            snaps = []
            for line in result.stdout.splitlines()[2:]:  # skip header
                parts = line.split()
                if len(parts) >= 4:
                    snaps.append({"id": parts[0], "tag": parts[1], "vm_size": parts[2], "date": parts[3]})
            return {"success": True, "snapshots": snaps, "raw": result.stdout}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Restores live via QMP loadvm or offline via qemu-img snapshot -a.
    # In: str name, str snap_name → Out: dict with success
    def snapshot_restore(self, name: str, snap_name: str) -> Dict[str, Any]:
        if self._is_running(name):
            try:
                cfg = MachineConfig.load(name)
                qmp = QMPClient(cfg.get_qmp_socket())
                qmp.connect()
                qmp.execute("loadvm", {"tag": snap_name})
                qmp.close()
                return {"success": True, "message": f"Snapshot '{snap_name}' restored (live)."}
            except Exception as e:
                return {"success": False, "error": str(e)}
        else:
            try:
                cfg       = MachineConfig.load(name)
                disk_path = os.path.expanduser(cfg.disks[0].path)
                result    = subprocess.run(
                    ["qemu-img", "snapshot", "-a", snap_name, disk_path],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    return {"success": False, "error": result.stderr}
                return {"success": True, "message": f"Snapshot '{snap_name}' restored (offline)."}
            except Exception as e:
                return {"success": False, "error": str(e)}

    # Runs qemu-img snapshot -d to delete a snapshot from disk.
    # In: str name, str snap_name → Out: dict with success
    def snapshot_delete(self, name: str, snap_name: str) -> Dict[str, Any]:
        try:
            cfg       = MachineConfig.load(name)
            disk_path = os.path.expanduser(cfg.disks[0].path)
            result    = subprocess.run(
                ["qemu-img", "snapshot", "-d", snap_name, disk_path],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                return {"success": False, "error": result.stderr}
            return {"success": True, "message": f"Snapshot '{snap_name}' deleted."}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Resource limits ────────────────────────────────────────────────────────

    # Caps CPU via cpulimit or cgroups; adjusts balloon memory via QMP.
    # In: str name, int? cpu_percent, int? memory_mb → Out: dict with results
    def set_resource_limits(self, name: str,
                             cpu_percent: Optional[int] = None,
                             memory_mb:   Optional[int] = None) -> Dict[str, Any]:
        if not self._is_running(name):
            return {"success": False, "error": f"VM '{name}' is not running."}

        results = {}
        pid = self._state.get_pid(name)

        if cpu_percent is not None:
            if sys.platform != "linux":
                results["cpu_limit_error"] = f"CPU limiting via cpulimit/cgroups is Linux-only (current platform: {sys.platform})."
            elif shutil.which("cpulimit"):
                subprocess.Popen(
                    ["cpulimit", "-p", str(pid), "-l", str(cpu_percent), "-b"],
                    start_new_session=True,
                )
                results["cpu_limit"] = f"cpulimit set to {cpu_percent}% (PID {pid})"
            else:
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

        if memory_mb is not None:
            try:
                cfg = MachineConfig.load(name)
                qmp = QMPClient(cfg.get_qmp_socket())
                qmp.connect()
                qmp.execute("balloon", {"value": memory_mb * 1024 * 1024})
                qmp.close()
                results["memory_balloon"] = f"Ballooned to {memory_mb}MB"
            except Exception as e:
                results["memory_balloon_error"] = str(e)

        return {"success": True, "name": name, "results": results}

    # ── Config ─────────────────────────────────────────────────────────────────

    # Loads and returns the VM's config dict.
    # In: str name → Out: dict with config
    def show_config(self, name: str) -> Dict[str, Any]:
        try:
            cfg = MachineConfig.load(name)
            return {"success": True, "config": cfg.to_dict()}
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

    # Applies a dict of field updates to a stopped VM's config and saves.
    # In: str name, dict updates → Out: dict with success
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

    # Removes the VM directory; optionally deletes disk image files too.
    # In: str name, bool delete_disks → Out: dict with success
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

    # Reads the launch log, pattern-matches 30+ known error strings, returns diagnosis and fix suggestions.
    # In: str name, int lines → Out: dict with errors, diagnosis, suggestions
    def get_vm_logs(self, name: str, lines: int = _CFG["log_default_lines"]) -> Dict[str, Any]:
        """Read the VM launch log and return a structured failure report."""
        vm_dir   = os.path.join(VM_BASE_DIR, name)
        log_path = os.path.join(vm_dir, "launch.log")
        result   = {
            "name": name, "log_path": log_path,
            "log_exists": os.path.exists(log_path),
            "raw_tail": "", "errors": [], "warnings": [],
            "last_line": "", "diagnosis": "", "suggestions": [],
        }

        if os.path.exists(log_path):
            with open(log_path, "r", errors="replace") as f:
                all_lines = f.readlines()
            tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
            result["raw_tail"]        = "".join(tail)
            result["last_line"]       = tail[-1].strip() if tail else ""
            result["total_log_lines"] = len(all_lines)

            error_patterns = [
                ("qemu: could not load",        "Boot image or kernel file not found"),
                ("no bootable device",           "No bootable disk — check ISO/disk image path"),
                ("failed to open",               "Could not open a file — check paths in config"),
                ("permission denied",            "Permission denied — check file permissions"),
                ("address already in use",       "Port conflict — VNC/SPICE port already in use"),
                ("kvm: permission denied",       "KVM permission denied — run: sudo usermod -aG kvm $USER"),
                ("kvm: no such file",            "KVM not available — check BIOS VT-x/AMD-V setting"),
                ("could not initialize kvm",     "KVM init failed — may conflict with another hypervisor"),
                ("unsupported machine type",     "Invalid machine type for this QEMU binary"),
                ("invalid accelerator",          "Accelerator not supported — KVM on wrong arch"),
                ("accel=kvm not supported",      "KVM not supported for this guest architecture"),
                ("cannot set up guest memory",   "Not enough RAM — reduce memory_mb or free host RAM"),
                ("hugepages",                    "Hugepages not allocated — run: sudo sysctl vm.nr_hugepages=2048"),
                ("no such file or directory",    "A required file is missing — check disk/ISO/firmware paths"),
                ("invalid parameter",            "Invalid QEMU argument — check machine type and CPU flags"),
                ("unknown cpu model",            "CPU model not supported by this QEMU version"),
                ("pflash",                       "OVMF/UEFI firmware file not found or wrong format"),
                ("virtio",                       "Virtio device error — may be incompatible with machine type"),
                ("sdl_init failed",              "SDL display init failed — check DISPLAY env var"),
                ("gtk_init_check failed",        "GTK display init failed — check DISPLAY env var"),
                ("socket",                       "Socket error — another instance may be running"),
                ("address family not supported", "Network config error — check bridge/network settings"),
                ("tap: failed to connect",       "TAP network error — bridge may not exist"),
                ("if=sd",                        "SD card interface error — check disk image format"),
                ("raspi",                        "Raspberry Pi machine error — needs kernel8.img + dtb file"),
                ("arm",                          "ARM machine error — check qemu-system-aarch64 is installed"),
                ("out of memory",                "Host is out of memory — reduce VM memory_mb"),
                ("segfault",                     "QEMU crashed (segfault) — try updating qemu-system"),
                ("aborted",                      "QEMU aborted — likely an incompatible argument combination"),
            ]
            for line in tail:
                line_lower = line.lower()
                for pattern, meaning in error_patterns:
                    if pattern in line_lower:
                        entry = {"line": line.strip(), "meaning": meaning}
                        if entry not in result["errors"]:
                            result["errors"].append(entry)
        else:
            result["diagnosis"] = (
                "No log file found — QEMU crashed immediately before writing output. "
                "This usually means the QEMU binary is wrong, a required argument is "
                "completely invalid, or the binary couldn't be executed at all."
            )

        try:
            cfg = MachineConfig.load(name)

            if cfg.machine_arch in ("aarch64", "arm") and "x86_64" in cfg.qemu_binary:
                result["errors"].append({
                    "line": f"qemu_binary = {cfg.qemu_binary}",
                    "meaning": "Wrong QEMU binary — ARM machine needs qemu-system-aarch64"
                })
            if cfg.kvm and cfg.machine_arch in ("aarch64", "arm"):
                result["errors"].append({
                    "line": "kvm=True on ARM guest",
                    "meaning": "KVM cannot be used for ARM guests on an x86 host"
                })
            if cfg.hugepages and sys.platform == "linux":
                try:
                    with open("/proc/sys/vm/nr_hugepages") as f:
                        if int(f.read().strip()) == 0:
                            result["errors"].append({
                                "line": "hugepages=True but nr_hugepages=0",
                                "meaning": "Hugepages requested but not allocated on host — run: sudo sysctl vm.nr_hugepages=2048"
                            })
                except Exception:
                    pass
            if cfg.iso_path and not os.path.exists(cfg.iso_path):
                result["errors"].append({"line": f"iso_path = {cfg.iso_path}", "meaning": f"ISO file not found: {cfg.iso_path}"})
            for i, disk in enumerate(cfg.disks):
                dp = os.path.expanduser(disk.path)
                if not os.path.exists(dp):
                    result["errors"].append({"line": f"disk[{i}].path = {disk.path}", "meaning": f"Disk image not found: {dp}"})
            if cfg.bios in ("ovmf", "ovmf_ms") and cfg.uefi:
                from qemu_config import OVMF as _OVMF
                if not _OVMF["available"]:
                    result["errors"].append({"line": "bios=ovmf but OVMF not installed", "meaning": "UEFI firmware not found — run: sudo apt install ovmf"})

            valid_machine_types = {"q35","pc","pc-i440fx","microvm","virt","raspi3b","raspi2b","raspi0"}
            mt = cfg.machine_type.lower().split(",")[0].strip()
            if mt not in valid_machine_types and not mt.startswith("pc-"):
                result["errors"].append({
                    "line": f"machine_type = {cfg.machine_type}",
                    "meaning": f"'{cfg.machine_type}' is not a valid QEMU machine type — it looks like a profile name was used by mistake. Should be 'q35' for modern x86 or 'pc' for legacy."
                })

            if cfg.iso_path:
                iso_lower  = os.path.basename(cfg.iso_path).lower()
                is_iso_arm = any(k in iso_lower for k in ("arm64","aarch64","arm_","_arm"))
                is_iso_x86 = any(k in iso_lower for k in ("amd64","x86_64","x64","i386","i686"))
                is_vm_arm  = cfg.machine_arch in ("aarch64","arm")
                is_vm_x86  = cfg.machine_arch == "x86_64"
                if is_iso_arm and is_vm_x86:
                    result["errors"].append({"line": f"iso={os.path.basename(cfg.iso_path)}, arch={cfg.machine_arch}", "meaning": "Architecture mismatch — ARM64 ISO cannot boot on an x86_64 VM."})
                elif is_iso_x86 and is_vm_arm:
                    result["errors"].append({"line": f"iso={os.path.basename(cfg.iso_path)}, arch={cfg.machine_arch}", "meaning": "Architecture mismatch — x86_64 ISO cannot boot on an ARM VM."})

            result["config_summary"] = {
                "qemu_binary": cfg.qemu_binary, "machine_type": cfg.machine_type,
                "machine_arch": cfg.machine_arch, "kvm": cfg.kvm, "bios": cfg.bios,
                "hugepages": cfg.hugepages, "iso_path": cfg.iso_path,
                "display": cfg.display, "memory_mb": cfg.memory_mb,
                "disk_paths": [d.path for d in cfg.disks],
            }
        except FileNotFoundError:
            result["config_error"] = f"No config found for VM '{name}'"
        except Exception as e:
            result["config_error"] = str(e)

        if result["errors"] and not result["diagnosis"]:
            result["diagnosis"] = result["errors"][0]["meaning"]

        suggestions = []
        for err in result["errors"]:
            m = err["meaning"].lower()
            if "hugepages" in m:    suggestions.append("sudo sysctl vm.nr_hugepages=2048")
            if "kvm permission" in m: suggestions.append("sudo usermod -aG kvm $USER  (then log out and back in)")
            if "arm" in m and "binary" in m: suggestions.append("sudo apt install qemu-system-arm")
            if "ovmf" in m or "uefi" in m: suggestions.append("sudo apt install ovmf")
            if "no bootable" in m:  suggestions.append("Check iso_path in VM config — run: qemu-api config " + name)
            if "port" in m or "address already" in m: suggestions.append("Change vnc_port or spice_port in VM config to a free port")
            if "display" in m:      suggestions.append("Check DISPLAY env var: echo $DISPLAY  (should be :0 or :1)")
            if "not a valid qemu machine type" in m or "profile name" in m:
                suggestions.append(f"Fix machine_type: run: qemu-api cmd {name} '' — or delete and recreate the VM with machine_type=q35")
            if "architecture mismatch" in m:
                suggestions.append("Fix: delete the VM and recreate it — the ISO arch and VM arch must match")
        result["suggestions"] = list(dict.fromkeys(suggestions))

        return result

    # Sends a raw command string to the QEMU human monitor socket.
    # In: str name, str cmd → Out: dict with output
    def send_monitor_cmd(self, name: str, cmd: str) -> Dict[str, Any]:
        try:
            cfg       = MachineConfig.load(name)
            sock_path = cfg.get_monitor_socket()
            if sock_path.startswith("tcp:"):
                host, port = sock_path[4:].rsplit(":", 1)
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(_TIMEOUTS["qmp_connect"])
                s.connect((host, int(port)))
            else:
                if not os.path.exists(sock_path):
                    return {"success": False, "error": "Monitor socket not found."}
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(_TIMEOUTS["qmp_connect"])
                s.connect(sock_path)
            time.sleep(_TIMEOUTS["monitor_recv_sleep"])
            s.recv(_BUFFERS["monitor_send"])
            s.sendall((cmd + "\n").encode())
            time.sleep(_TIMEOUTS["monitor_recv_sleep"])
            response = s.recv(_BUFFERS["monitor_recv"]).decode()
            s.close()
            return {"success": True, "output": response}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Builds and returns the full QEMU command string without running it.
    # In: str name → Out: dict with command
    def print_command(self, name: str) -> Dict[str, Any]:
        try:
            cfg = MachineConfig.load(name)
            cmd = QemuArgBuilder(cfg).build()
            return {"success": True, "command": " ".join(cmd)}
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

    # ── Isolated network pass-throughs ─────────────────────────────────────────

    def create_network(self, net_name: str)                       -> Dict[str, Any]: return self.iso_nets.create_network(net_name)
    def delete_network(self, net_name: str)                       -> Dict[str, Any]: return self.iso_nets.delete_network(net_name)
    def list_networks(self)                                       -> List[Dict]:     return self.iso_nets.list_networks()
    def add_vm_to_network(self, net_name: str, vm_name: str)     -> Dict[str, Any]: return self.iso_nets.add_vm_to_network(net_name, vm_name)

    # ── Private helpers ────────────────────────────────────────────────────────

    # Checks Popen.poll() or psutil liveness; cleans up stale state if dead.
    # In: str name → Out: bool
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

    # Scans all VM configs and collects already-assigned VNC or SPICE ports.
    # In: str kind ("vnc"|"spice") → Out: List[int]
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
                    if kind == "vnc"   and data.get("vnc_port"):   ports.append(data["vnc_port"])
                    if kind == "spice" and data.get("spice_port"): ports.append(data["spice_port"])
                except Exception:
                    pass
        return ports

    # Calls taskset to pin a process to specific host CPU cores (Linux only).
    # In: int pid, List[int] cpus → Out: nothing
    def _apply_cpu_pinning(self, pid: int, cpus: List[int]):
        if sys.platform != "linux":
            return
        subprocess.run(["taskset", "-cp", ",".join(map(str, cpus)), str(pid)], capture_output=True)

"""
qemu_manager.py — VM Orchestration Layer

QemuManager is the single public façade for all VM lifecycle operations:
create, clone, launch, stop, status, monitor, snapshots, disk resize,
resource limits, network, display, shell, config, and log analysis.
"""

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

_CFG                 = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_TIMEOUTS            = _CFG["timeouts"]
_BUFFERS             = _CFG["buffers"]
_MACOS_OVMF          = _CFG["ovmf_macos_vars_paths"]
_WIN_OVMF            = _CFG["ovmf_win_vars_paths"]
_LOG_ERROR_PATTERNS  = [tuple(p) for p in _CFG["log_error_patterns"]]
_VALID_MACHINE_TYPES = set(_CFG["valid_machine_types"])

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

_LINUX_DISTROS = [
    "ubuntu", "debian", "fedora", "mint", "linuxmint", "arch", "manjaro",
    "opensuse", "suse", "kali", "parrot", "tails", "centos", "rocky", "alma",
    "pop", "elementary", "zorin", "rhel", "void", "gentoo", "slackware",
    "deepin", "mx", "antiX", "antix",
]


def _infer_distro(iso_path: Optional[str], os_type: str) -> str:
    if iso_path:
        needle = os.path.basename(iso_path).lower()
        for distro in _LINUX_DISTROS:
            if distro in needle:
                return "mint" if distro == "linuxmint" else distro
    return os_type


class QemuManager:
    # Creates the VM base dir, initializes state and net managers, reconnects to surviving VMs.
    # In: nothing → Out: nothing
    def __init__(self):
        os.makedirs(VM_BASE_DIR, exist_ok=True)
        self._state         = VMState()
        self._procs:        Dict[str, subprocess.Popen] = {}
        self._setup_srvs:   Dict[str, tuple] = {}  # name → (HTTPServer, port)
        self.iso_nets       = IsolatedNetManager()
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
                "os":          cfg.os_name or _infer_distro(cfg.iso_path, cfg.os_type),
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
        if config.bios in ("ovmf", "ovmf_ms"):
            vars_dst = os.path.join(vm_dir, "OVMF_VARS.fd")
            if not os.path.exists(vars_dst):
                code_path = OVMF.get("code", "")
                prefer_4m = "4M" in (code_path or "")

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

        # Auto-attach a matching ISO if none was provided
        if not config.iso_path:
            matches = self._match_iso(config.os_type, config.os_name, config.machine_arch)
            matches.sort(key=lambda x: x["match_score"], reverse=True)
            if matches and matches[0]["match_score"] > 0:
                config.iso_path  = matches[0]["path"]
                config.boot_order = "dc"

        config.save()
        _iso_basename = os.path.basename(config.iso_path) if config.iso_path else ""
        return {
            "success":       True,
            "name":          config.name,
            "vm_dir":        vm_dir,
            "bios":          config.bios,
            "uefi":          config.uefi,
            "iso_path":      config.iso_path,
            "iso_name":      _iso_basename,
            "os_name":       config.os_name,
            "message": (
                f"VM '{config.name}' created successfully."
                + (f" Attached ISO: {_iso_basename} (os_name={config.os_name})" if _iso_basename else " No ISO attached.")
            ),
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
    def _start_swtpm(self, config: "MachineConfig") -> Optional[str]:
        vm_dir   = config.get_vm_dir()
        tpm_dir  = os.path.join(vm_dir, "tpm")
        tpm_sock = os.path.join(vm_dir, "tpm.sock")
        tpm_pid  = os.path.join(vm_dir, "tpm.pid")
        os.makedirs(tpm_dir, exist_ok=True)
        if os.path.exists(tpm_sock):
            os.unlink(tpm_sock)
        try:
            subprocess.Popen([
                "swtpm", "socket",
                "--tpmstate", f"dir={tpm_dir}",
                "--ctrl",     f"type=unixio,path={tpm_sock}",
                "--tpm2",
                "--log",      "level=0",
                "--pid",      f"file={tpm_pid}",
                "--daemon",
            ])
        except FileNotFoundError:
            return "swtpm not found — install it with: sudo apt install swtpm"
        for _ in range(30):
            if os.path.exists(tpm_sock):
                return None
            time.sleep(0.1)
        return "swtpm started but socket never appeared — check swtpm installation"

    def _stop_swtpm(self, name: str) -> None:
        tpm_pid = os.path.join(os.path.expanduser(f"~/.qemu_vms/{name}"), "tpm.pid")
        if not os.path.exists(tpm_pid):
            return
        try:
            with open(tpm_pid) as f:
                pid = int(f.read().strip())
            import signal as _signal
            os.kill(pid, _signal.SIGTERM)
            os.unlink(tpm_pid)
        except (ValueError, ProcessLookupError, OSError):
            pass

    def _maybe_auto_detach_iso(self, config: "MachineConfig") -> bool:
        """If an ISO is attached but the disk already has a substantial OS install
        (actual qcow2 data > 2 GB), auto-detach the ISO and persist the config.
        Returns True if detached."""
        if not config.iso_path:
            return False
        disks = getattr(config, "disks", [])
        if not disks:
            return False
        disk_path = getattr(disks[0], "path", None)
        if not disk_path or not os.path.exists(disk_path):
            return False
        try:
            r = subprocess.run(
                ["qemu-img", "info", "--output=json", disk_path],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return False
            actual = json.loads(r.stdout).get("actual-size", 0)
            if actual < 2 * 1024 ** 3:
                return False
        except Exception:
            return False
        config.iso_path = None
        config.save()
        return True

    def launch_vm(self, name: str, display: Optional[str] = None,
                  dry_run: bool = False) -> Dict[str, Any]:
        _qemu_version_warn()
        try:
            config = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

        if display:
            config.display = display

        if dry_run:
            cmd = QemuArgBuilder(config).build()
            return {"success": True, "dry_run": True, "command": " ".join(cmd), "message": "Dry run — command not executed."}

        if self._is_running(name):
            return {"success": False, "error": f"VM '{name}' is already running."}

        if config.tpm and not config.machine_arch == "aarch64":
            tpm_err = self._start_swtpm(config)
            if tpm_err:
                return {"success": False, "error": tpm_err}

        auto_detached_iso = self._maybe_auto_detach_iso(config)

        cmd     = QemuArgBuilder(config).build()
        cmd_str = " ".join(cmd)

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

        result: Dict[str, Any] = {
            "success": True, "name": name, "pid": proc.pid, "display": config.display,
            "message": f"VM '{name}' launched (PID {proc.pid}).",
        }
        if auto_detached_iso:
            result["note"] = f"ISO detached automatically — disk has an installed OS. VM will boot from disk."

        # Auto-serve guest setup script for stealth VMs that haven't been set up yet.
        if config.stealth:
            sentinel = os.path.join(config.get_vm_dir(), ".stealth_done")
            if not os.path.exists(sentinel):
                gr = self.generate_guest_setup(name)
                if gr.get("success"):
                    port = self._start_setup_server(name, gr["path"])
                    result["setup_cmd"]     = gr["cmd_template"].format(port=port)
                    result["setup_pending"] = True

        # When an ISO is attached, -no-reboot makes "Restart Now" exit QEMU cleanly.
        # Spawn a detached watcher process (not a daemon thread — those die when the
        # CLI process exits) that waits for QEMU to finish, then relaunches the VM so
        # _maybe_auto_detach_iso can strip the ISO and boot the installed OS.
        if config.iso_path:
            relaunch_flag = os.path.join(config.get_vm_dir(), ".relaunch_after_install")
            open(relaunch_flag, "w").close()
            watcher_script = (
                f"import sys, os, time, psutil\n"
                f"sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(__file__)))})\n"
                f"pid, flag, name = {proc.pid}, {repr(relaunch_flag)}, {repr(name)}\n"
                f"try:\n"
                f"    p = psutil.Process(pid)\n"
                f"    p.wait()\n"
                f"    rc = p.returncode if hasattr(p, 'returncode') else 0\n"
                f"except Exception: rc = 0\n"
                f"if os.path.exists(flag):\n"
                f"    os.unlink(flag)\n"
                f"    time.sleep(2)\n"
                f"    from api.qemu_manager import QemuManager\n"
                f"    mgr = QemuManager()\n"
                f"    mgr.launch_vm(name)\n"
                f"    stealth_done = os.path.join(os.path.expanduser('~/.qemu_vms'), name, '.stealth_done')\n"
                f"    while not os.path.exists(stealth_done):\n"
                f"        time.sleep(5)\n"
            )
            subprocess.Popen(
                [sys.executable, "-c", watcher_script],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        return result

    # ── Stop ───────────────────────────────────────────────────────────────────

    # Tries graceful QMP system_powerdown, then terminates/kills the process.
    # In: str name, bool force → Out: dict with success
    def _find_qemu_pid(self, name: str) -> Optional[int]:
        """Find an orphaned QEMU process by VM name (survives session restarts)."""
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = " ".join(proc.info["cmdline"] or [])
                if f"process={name}" in cmdline and "qemu" in cmdline.lower():
                    return proc.info["pid"]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return None

    def stop_vm(self, name: str, force: bool = False) -> Dict[str, Any]:
        if not self._is_running(name):
            pid = self._find_qemu_pid(name)
            if not pid:
                return {"success": False, "error": f"VM '{name}' is not running."}
            try:
                p = psutil.Process(pid)
                self._procs[name] = _PsutilProcWrapper(p)
                self._state.set_running(name, pid)
            except psutil.NoSuchProcess:
                return {"success": False, "error": f"VM '{name}' is not running."}

        # Cancel auto-relaunch before stopping — this is an intentional stop.
        relaunch_flag = os.path.join(os.path.expanduser(f"~/.qemu_vms/{name}"), ".relaunch_after_install")
        if os.path.exists(relaunch_flag):
            os.unlink(relaunch_flag)

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
        self._stop_swtpm(name)
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
        if disk_index < 0 or disk_index >= len(cfg.disks):
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

            for line in tail:
                line_lower = line.lower()
                for pattern, meaning in _LOG_ERROR_PATTERNS:
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
                else:
                    try:
                        r = subprocess.run(
                            ["qemu-img", "info", "--output=json", dp],
                            capture_output=True, text=True, timeout=10,
                        )
                        if r.returncode == 0:
                            info = json.loads(r.stdout)
                            if info.get("actual-size", 0) < 1024 * 1024:
                                result["errors"].append({
                                    "line": f"disk[{i}] actual size = {info.get('actual-size', 0)} bytes",
                                    "meaning": f"Disk {i} is blank — no OS installed. Attach an ISO and boot from it to install.",
                                })
                    except Exception:
                        pass
            if cfg.bios in ("ovmf", "ovmf_ms") and cfg.uefi:
                from qemu_config import OVMF as _OVMF
                if not _OVMF["available"]:
                    result["errors"].append({"line": "bios=ovmf but OVMF not installed", "meaning": "UEFI firmware not found — run: sudo apt install ovmf"})

            mt = cfg.machine_type.lower().split(",")[0].strip()
            if mt not in _VALID_MACHINE_TYPES and not mt.startswith("pc-"):
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
            if "blank" in m and "disk" in m: suggestions.append("Call scan_isos to find an ISO, then update_config with iso_path, then launch_vm")
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

    # Runs qemu-img info --output=json on each disk and reports blank/non-blank state.
    # A disk is considered blank if its actual disk_size < 1 MB (just the qcow2 header).
    # In: str name → Out: dict with per-disk info and top-level has_blank_disk flag
    def check_disk(self, name: str) -> Dict[str, Any]:
        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

        disks_info = []
        has_blank  = False
        for i, disk in enumerate(cfg.disks):
            disk_path = os.path.expanduser(disk.path)
            if not os.path.exists(disk_path):
                disks_info.append({
                    "index": i, "path": disk.path,
                    "exists": False, "blank": True,
                    "error": "Disk image file not found",
                })
                has_blank = True
                continue
            result = subprocess.run(
                ["qemu-img", "info", "--output=json", disk_path],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                disks_info.append({
                    "index": i, "path": disk.path,
                    "exists": True, "blank": False,
                    "error": result.stderr.strip(),
                })
                continue
            try:
                info = json.loads(result.stdout)
            except json.JSONDecodeError:
                disks_info.append({
                    "index": i, "path": disk.path,
                    "exists": True, "blank": False,
                    "error": "Could not parse qemu-img output",
                })
                continue
            actual_bytes  = info.get("actual-size", 0)
            virtual_bytes = info.get("virtual-size", 0)
            blank         = actual_bytes < 1024 * 1024  # < 1 MB means just the header
            if blank:
                has_blank = True
            disks_info.append({
                "index":         i,
                "path":          disk.path,
                "exists":        True,
                "blank":         blank,
                "actual_size_mb":  round(actual_bytes  / 1024**2, 2),
                "virtual_size_gb": round(virtual_bytes / 1024**3, 1),
                "format":        info.get("format", disk.format),
            })

        diagnosis      = ""
        suggestions    = []
        suggested_iso  = None
        compatible_isos: List[Dict[str, Any]] = []

        if has_blank:
            diagnosis = (
                "One or more disks are blank — no OS has been installed. "
                "Attach an ISO and boot from it to install an OS."
            )
            compatible_isos = self._match_iso(cfg.os_type, cfg.os_name, cfg.machine_arch)
            compatible_isos.sort(key=lambda x: x["match_score"], reverse=True)

            if compatible_isos and compatible_isos[0]["match_score"] > 0:
                suggested_iso = compatible_isos[0]["path"]
                suggestions = [
                    f"Auto-matched ISO based on os_type='{cfg.os_type}' os_name='{cfg.os_name}': {compatible_isos[0]['name']}",
                    f"Call update_config with iso_path='{suggested_iso}'",
                    "Call launch_vm — the VM will boot the ISO installer",
                    "After installation completes, call update_config with iso_path=null to remove the ISO",
                ]
            elif compatible_isos:
                suggested_iso = compatible_isos[0]["path"]
                suggestions = [
                    f"No OS keyword match found — using first compatible ISO: {compatible_isos[0]['name']}",
                    f"Call update_config with iso_path='{suggested_iso}'",
                    "Call launch_vm — the VM will boot the ISO installer",
                    "After installation completes, call update_config with iso_path=null to remove the ISO",
                ]
            else:
                suggestions = [
                    "No compatible ISO found on this system — download one first",
                    "Call scan_isos after placing the ISO in ~/Downloads or ~/Desktop",
                    "Call update_config with iso_path set to the ISO path",
                    "Call launch_vm — the VM will boot the ISO installer",
                    "After installation completes, call update_config with iso_path=null to remove the ISO",
                ]

        return {
            "success":        True,
            "name":           name,
            "os_type":        cfg.os_type,
            "os_name":        cfg.os_name,
            "machine_arch":   cfg.machine_arch,
            "has_blank_disk": has_blank,
            "disks":          disks_info,
            "diagnosis":      diagnosis,
            "suggested_iso":  suggested_iso,
            "compatible_isos": compatible_isos,
            "suggestions":    suggestions,
        }

    # Builds and returns the full QEMU command string without running it.
    # In: str name → Out: dict with command
    def print_command(self, name: str) -> Dict[str, Any]:
        try:
            cfg = MachineConfig.load(name)
            cmd = QemuArgBuilder(cfg).build()
            return {"success": True, "command": " ".join(cmd)}
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

    def _start_setup_server(self, name: str, script_path: str) -> int:
        script_dir = os.path.dirname(script_path)
        with socket.socket() as s:
            s.bind(('', 0))
            port = s.getsockname()[1]
        class _H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw): super().__init__(*a, directory=script_dir, **kw)
            def log_message(self, *_): pass
        srv = http.server.HTTPServer(('0.0.0.0', port), _H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self._setup_srvs[name] = (srv, port)
        return port

    def mark_stealth_done(self, name: str) -> Dict[str, Any]:
        vm_dir   = os.path.expanduser(f"~/.qemu_vms/{name}")
        sentinel = os.path.join(vm_dir, ".stealth_done")
        try:
            open(sentinel, "w").close()
        except OSError as e:
            return {"success": False, "error": str(e)}
        if name in self._setup_srvs:
            srv, _ = self._setup_srvs.pop(name)
            threading.Thread(target=srv.shutdown, daemon=True).start()
        return {"success": True, "message": f"Stealth setup for '{name}' marked complete — won't prompt again."}

    def generate_guest_setup(self, name: str) -> Dict[str, Any]:
        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

        vm_dir = os.path.expanduser(f"~/.qemu_vms/{name}")
        os.makedirs(vm_dir, exist_ok=True)

        mfr            = cfg.manufacturer or "Unknown"
        product        = cfg.product_name or "Unknown"
        webgl_renderer = "Intel(R) Iris(R) Xe Graphics"
        webgl_vendor   = "Intel"

        is_windows = "windows" in (cfg.os_type or "").lower()

        if is_windows:
            return self._generate_guest_setup_windows(
                name, vm_dir, mfr, product, webgl_renderer, webgl_vendor)
        return self._generate_guest_setup_linux(
            name, vm_dir, mfr, product, webgl_renderer, webgl_vendor)

    def _generate_guest_setup_linux(self, name, vm_dir, mfr, product,
                                     webgl_renderer, webgl_vendor) -> Dict[str, Any]:
        script_path = os.path.join(vm_dir, "guest_setup.sh")
        script = f"""\
#!/usr/bin/env bash
# Guest stealth setup for: {name} ({mfr} {product})
# Run once inside the VM as a regular user with sudo access.
set -euo pipefail

# ── Preflight: refuse to run on a live/uninstalled system ─────────────────────
if grep -qE '\\bboot=casper\\b|\\blive\\b|\\brd\\.live\\b' /proc/cmdline 2>/dev/null; then
    echo "ERROR: Live session detected (boot=casper / live in /proc/cmdline)."
    echo "       Install {mfr} {product} to disk first, then run this script."
    exit 1
fi
if [ ! -f /etc/fstab ] || ! grep -qv '^#' /etc/fstab 2>/dev/null; then
    echo "ERROR: No installed system detected (empty or missing /etc/fstab)."
    echo "       Complete the OS installation, reboot, then run this script."
    exit 1
fi
if ! command -v update-initramfs &>/dev/null && ! command -v mkinitcpio &>/dev/null && ! command -v dracut &>/dev/null; then
    echo "ERROR: No initramfs tool found (tried update-initramfs, mkinitcpio, dracut)."
    echo "       This script requires a fully installed Linux system."
    exit 1
fi

echo "=== Stealth guest setup: {name} ==="

# ── 1. Blacklist qemu_fw_cfg ─────────────────────────────────────────────────
echo "[1/4] Blacklisting qemu_fw_cfg..."
printf 'blacklist qemu_fw_cfg\nblacklist cirrus_qemu\n' | sudo tee /etc/modprobe.d/blacklist-qemu.conf >/dev/null
if command -v update-initramfs &>/dev/null; then
    sudo update-initramfs -u -k all
elif command -v mkinitcpio &>/dev/null; then
    sudo mkinitcpio -P
elif command -v dracut &>/dev/null; then
    sudo dracut --force
fi
echo "      Done — takes effect after reboot."

# ── 2. Firefox stealth profile ────────────────────────────────────────────────
echo "[2/4] Creating Firefox stealth profile..."
FIREFOX_BIN="$(command -v firefox 2>/dev/null || command -v firefox-esr 2>/dev/null || echo '')"
PROF_DIR="$HOME/.mozilla/firefox/stealth"
mkdir -p "$PROF_DIR"
cat > "$PROF_DIR/user.js" << 'USERJS'
user_pref("webgl.renderer-string.override", "{webgl_renderer}");
user_pref("webgl.vendor-string.override",   "{webgl_vendor}");
user_pref("webgl.disabled",       false);
user_pref("webgl.force-enabled",  true);
USERJS

if [ -n "$FIREFOX_BIN" ]; then
    PROF_INI="$HOME/.mozilla/firefox/profiles.ini"
    if ! grep -q "\\[Profile.*stealth\\]" "$PROF_INI" 2>/dev/null; then
        printf "\\n[Profile999]\\nName=stealth\\nIsRelative=1\\nPath=stealth\\n" >> "$PROF_INI"
    fi
fi
echo "      Profile written to $PROF_DIR"

# ── 3. Stealth browser launcher ───────────────────────────────────────────────
echo "[3/4] Creating stealth browser launcher..."
mkdir -p "$HOME/Desktop"
LAUNCHER="$HOME/Desktop/stealth-browser.sh"
if [ -n "$FIREFOX_BIN" ]; then
    printf '#!/usr/bin/env bash\nexec %s --profile "$HOME/.mozilla/firefox/stealth" --no-remote "$@"\n' "$FIREFOX_BIN" > "$LAUNCHER"
else
    printf '#!/usr/bin/env bash\nexec firefox --profile "$HOME/.mozilla/firefox/stealth" --no-remote "$@"\n' > "$LAUNCHER"
    echo "      WARNING: Firefox not found — install it then re-run, or edit the launcher."
fi
chmod +x "$LAUNCHER"
echo "      Launcher: $LAUNCHER"

# ── 4. lspci / lsmod stealth wrappers ────────────────────────────────────────
echo "[4/4] Installing lspci/lsmod stealth wrappers..."

LSPCI_BIN="$(command -v lspci 2>/dev/null || echo /usr/bin/lspci)"
if [ ! -x "${{LSPCI_BIN}}.real" ]; then
    sudo mv "$LSPCI_BIN" "${{LSPCI_BIN}}.real"
    sudo tee "$LSPCI_BIN" > /dev/null << 'LSPCI_WRAP'
#!/usr/bin/env python3
import subprocess, sys, re, os
real = os.path.realpath(sys.argv[0]) + '.real'
result = subprocess.run([real] + sys.argv[1:], capture_output=True, text=True)
out = result.stdout
out = re.sub(
    r'^[0-9a-f:.]+\\s+VGA compatible controller: VMware.*$',
    '00:02.0 VGA compatible controller: Intel Corporation Alder Lake-P GT2 [Iris Xe Graphics] (rev 0c)',
    out, flags=re.MULTILINE
)
def patch_block(block):
    if 'VMware' not in block or 'VGA' not in block:
        return block
    block = re.sub(r'^(Vendor:\\t).*$', r'\\1Intel Corporation', block, flags=re.MULTILINE)
    block = re.sub(r'^(Device:\\t).*$', r'\\1Alder Lake-P GT2 [Iris Xe Graphics]', block, flags=re.MULTILINE)
    block = re.sub(r'^(SVendor:\\t).*$', r'\\1Intel Corporation', block, flags=re.MULTILINE)
    block = re.sub(r'^(SDevice:\\t).*$', r'\\1Iris Xe Graphics', block, flags=re.MULTILINE)
    block = re.sub(r'^(Rev:\\t).*$', r'\\10c', block, flags=re.MULTILINE)
    return block
out = '\\n\\n'.join(patch_block(b) for b in out.split('\\n\\n'))
sys.stdout.write(out)
sys.exit(result.returncode)
LSPCI_WRAP
    sudo chmod +x "$LSPCI_BIN"
    echo "      lspci wrapper installed."
else
    echo "      lspci wrapper already present, skipping."
fi

LSMOD_BIN="$(command -v lsmod 2>/dev/null || echo /usr/sbin/lsmod)"
if [ ! -x "${{LSMOD_BIN}}.real" ]; then
    sudo mv "$LSMOD_BIN" "${{LSMOD_BIN}}.real"
    sudo tee "$LSMOD_BIN" > /dev/null << 'LSMOD_WRAP'
#!/usr/bin/env bash
REAL="$(dirname "$(readlink -f "$0")")/$(basename "$0").real"
"$REAL" "$@" | grep -v '^qemu'
LSMOD_WRAP
    sudo chmod +x "$LSMOD_BIN"
    echo "      lsmod wrapper installed."
else
    echo "      lsmod wrapper already present, skipping."
fi

echo ""
echo "=== Setup complete ==="
echo "REBOOT the VM for the qemu_fw_cfg blacklist to take effect."
echo "After reboot:"
echo "  lsmod | grep qemu          # should be empty"
echo "  cat /sys/class/dmi/id/chassis_type   # should be 9"
echo "  inxi -F                    # should show: Type: Laptop  System: {mfr}"
"""
        with open(script_path, "w") as f:
            f.write(script)
        os.chmod(script_path, 0o755)
        return {
            "success": True, "path": script_path, "vm": name,
            "cmd_template": "curl http://10.0.2.2:{port}/guest_setup.sh | sudo bash",
        }

    def _generate_guest_setup_windows(self, name, vm_dir, mfr, product,
                                       webgl_renderer, webgl_vendor) -> Dict[str, Any]:
        script_path = os.path.join(vm_dir, "guest_setup.ps1")
        # Double {{ }} escapes f-string braces; PowerShell uses $var syntax so
        # no conflict, but we need literal {} for the port template placeholder only
        # in cmd_template, not in the script itself.
        script = f"""\
# Stealth guest setup for: {name} ({mfr} {product})
# Run once in an elevated PowerShell window (Run as Administrator) inside the VM.
$ErrorActionPreference = 'Stop'

Write-Host "=== Stealth guest setup: {name} ===" -ForegroundColor Cyan

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {{
    Write-Host "WARNING: Not running as Administrator. GPU spoof (step 3) will be skipped." -ForegroundColor Yellow
    Write-Host "         Re-run as Administrator for full stealth." -ForegroundColor Yellow
}}

# ── 1. Firefox stealth profile ────────────────────────────────────────────────
Write-Host "[1/3] Creating Firefox stealth profile..."
$profDir = "$env:APPDATA\\Mozilla\\Firefox\\Profiles\\stealth"
New-Item -ItemType Directory -Force -Path $profDir | Out-Null

Set-Content -Path "$profDir\\user.js" -Value @"
user_pref(`"webgl.renderer-string.override`", `"{webgl_renderer}`");
user_pref(`"webgl.vendor-string.override`",   `"{webgl_vendor}`");
user_pref(`"webgl.disabled`",      `$false);
user_pref(`"webgl.force-enabled`", `$true);
"@

$iniPath = "$env:APPDATA\\Mozilla\\Firefox\\profiles.ini"
if (Test-Path $iniPath) {{
    $ini = Get-Content $iniPath -Raw
    if ($ini -notmatch 'Profile999') {{
        Add-Content -Path $iniPath -Value "`n[Profile999]`nName=stealth`nIsRelative=1`nPath=Profiles/stealth"
    }}
}}
Write-Host "   Profile written to $profDir"

# ── 2. Desktop shortcut for stealth Firefox ───────────────────────────────────
Write-Host "[2/3] Creating desktop shortcut..."
$ffPaths = @(
    "$env:ProgramFiles\\Mozilla Firefox\\firefox.exe",
    "$env:LOCALAPPDATA\\Mozilla Firefox\\firefox.exe"
)
$created = $false
foreach ($ff in $ffPaths) {{
    if (Test-Path $ff) {{
        $ws  = New-Object -ComObject WScript.Shell
        $lnk = $ws.CreateShortcut("$env:USERPROFILE\\Desktop\\Stealth Browser.lnk")
        $lnk.TargetPath  = $ff
        $lnk.Arguments   = "--profile `"$profDir`" --no-remote"
        $lnk.Description = "Firefox with stealth WebGL profile"
        $lnk.Save()
        Write-Host "   Shortcut: $env:USERPROFILE\\Desktop\\Stealth Browser.lnk"
        $created = $true
        break
    }}
}}
if (-not $created) {{
    Write-Host "   Firefox not found — install it, then re-run this script." -ForegroundColor Yellow
}}

# ── 3. GPU display name spoof (admin required) ───────────────────────────────
Write-Host "[3/3] Spoofing GPU display name..."
if ($isAdmin) {{
    # Strategy 1: video class DriverDesc (works when VMware/virtio driver is installed)
    $videoClass = 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\{{4d36e968-e325-11ce-bfc1-08002be10318}}'
    if (Test-Path $videoClass) {{
        Get-ChildItem $videoClass -ErrorAction SilentlyContinue | ForEach-Object {{
            $desc = (Get-ItemProperty $_.PSPath -Name DriverDesc -ErrorAction SilentlyContinue).DriverDesc
            if ($desc -and ($desc -like '*VMware*' -or $desc -like '*SVGA*' -or $desc -like '*Standard VGA*' -or $desc -like '*Basic Display*')) {{
                Set-ItemProperty $_.PSPath -Name DriverDesc -Value '{webgl_renderer}' -ErrorAction SilentlyContinue
                $prov = (Get-ItemProperty $_.PSPath -Name ProviderName -ErrorAction SilentlyContinue).ProviderName
                if ($prov) {{ Set-ItemProperty $_.PSPath -Name ProviderName -Value '{webgl_vendor} Corporation' -ErrorAction SilentlyContinue }}
                Write-Host "   DriverDesc renamed: '$desc' -> '{webgl_renderer}'"
            }}
        }}
    }}
    # Strategy 2: FriendlyName in PCI enum key (works for basicdisplay.sys / no-driver case)
    $enumPci = 'HKLM:\\SYSTEM\\CurrentControlSet\\Enum\\PCI'
    Get-ChildItem $enumPci -ErrorAction SilentlyContinue |
        Where-Object {{ $_.Name -like '*VEN_1234*' }} |
        ForEach-Object {{
            Get-ChildItem $_.PSPath -ErrorAction SilentlyContinue | ForEach-Object {{
                $p = $_.PSPath
                try {{
                    $acl = Get-Acl $p
                    $acl.SetAccessRule((New-Object System.Security.AccessControl.RegistryAccessRule(
                        $env:USERNAME, 'FullControl', 'Allow')))
                    Set-Acl $p $acl
                    Set-ItemProperty $p -Name FriendlyName -Value '{webgl_renderer}' -ErrorAction Stop
                    Write-Host "   FriendlyName set on $(Split-Path $p -Leaf)"
                }} catch {{
                    Write-Host "   Could not set FriendlyName: $_" -ForegroundColor Yellow
                }}
            }}
        }}
    Write-Host "   Reboot for Device Manager to reflect the change."
}} else {{
    Write-Host "   Skipped (not Administrator)." -ForegroundColor Yellow
}}

Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Green
Write-Host "Launch Firefox via 'Stealth Browser' on your Desktop."
Write-Host "Verify WebGL at: https://browserleaks.com/webgl (expect: {webgl_renderer})"
"""
        with open(script_path, "w", newline="\r\n") as f:
            f.write(script)
        return {
            "success": True, "path": script_path, "vm": name,
            "cmd_template": (
                "irm http://10.0.2.2:{port}/guest_setup.ps1 | iex"
            ),
        }

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
        pid = self._find_qemu_pid(name)
        if pid:
            try:
                p = psutil.Process(pid)
                if p.is_running():
                    self._procs[name] = _PsutilProcWrapper(p)
                    self._state.set_running(name, pid)
                    return True
            except psutil.NoSuchProcess:
                pass
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

    # Scores available ISOs against os_type/os_name keywords and filters by arch.
    # In: str os_type, str os_name, str machine_arch → Out: List[dict] with match_score
    def _match_iso(self, os_type: str, os_name: str, machine_arch: str) -> List[Dict[str, Any]]:
        _OS_KEYWORDS: Dict[str, List[str]] = {
            "windows": ["windows", "win11", "win10", "win"],
            "linux":   ["linux", "ubuntu", "debian", "fedora", "mint", "arch",
                        "opensuse", "manjaro", "pop", "elementary", "zorin",
                        "kali", "parrot", "tails", "centos", "rocky", "alma"],
            "macos":   ["macos", "mac", "osx", "darwin", "ventura", "sonoma",
                        "monterey", "sequoia"],
        }
        _ARM_MARKERS = ("arm64", "aarch64", "_arm_", "-arm-", "arm_v")
        _X86_MARKERS = ("amd64", "x86_64", "x64", "i386", "i686", "64bit", "64-bit")

        os_type_l = (os_type or "").lower()
        os_name_l = (os_name or "").lower()
        vm_is_x86 = machine_arch == "x86_64"
        vm_is_arm = machine_arch in ("aarch64", "arm")

        generic_keywords: List[str] = []
        for key, kws in _OS_KEYWORDS.items():
            if key in os_type_l or key in os_name_l:
                generic_keywords.extend(kws)

        # Words from os_name get a 10x score bonus over generic type keywords so that
        # e.g. "ubuntu" always outranks "linuxmint" (which matches both "linux" and "mint").
        specific_words = [w for w in os_name_l.split() if len(w) > 3]

        results: List[Dict[str, Any]] = []
        for iso in self.scan_isos():
            fname = iso["name"].lower()
            if vm_is_x86 and any(m in fname for m in _ARM_MARKERS):
                continue
            if vm_is_arm and any(m in fname for m in _X86_MARKERS):
                continue
            specific_score = sum(10 for w in specific_words if w in fname)
            generic_score  = sum(1  for kw in generic_keywords if kw in fname and kw not in specific_words)
            results.append({**iso, "match_score": specific_score + generic_score})
        return results

    # Calls taskset to pin a process to specific host CPU cores (Linux only).
    # In: int pid, List[int] cpus → Out: nothing
    def _apply_cpu_pinning(self, pid: int, cpus: List[int]):
        if sys.platform != "linux":
            return
        subprocess.run(["taskset", "-cp", ",".join(map(str, cpus)), str(pid)], capture_output=True)

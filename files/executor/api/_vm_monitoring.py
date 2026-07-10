"""
_vm_monitoring.py — VM Monitoring Mixin (status / metrics / display / shell).

Provides _VmMonitoringMixin which is composed into QemuManager.
"""
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

import psutil

from ._vm_constants import VM_BASE_DIR, infer_os_name
from .qemu_config import MachineConfig
from .qmp_client import QMPClient




class _VmMonitoringMixin:
    """Mixin providing VM listing, status, resource monitoring, and display/shell."""

    # ------------------------------------------------------------------
    # VM listing
    # ------------------------------------------------------------------

    def list_vms(self) -> List[Dict[str, Any]]:
        """Scan ``~/.qemu_vms/`` and return status info for every VM directory.

        Returns:
            List of dicts with ``name``, ``id``, ``description``, ``os``,
            ``cpu_cores``, ``memory_mb``, ``disks``, and ``status`` keys.

        Example::
            >>> mgr.list_vms()
            [{"name": "my-linux", "status": "stopped", "memory_mb": 4096, ...}]
        """
        vms: List[Dict[str, Any]] = []
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
                "os":          cfg.os_name or infer_os_name(cfg.iso_path, cfg.os_type),
                "cpu_cores":   cfg.cpu_cores,
                "memory_mb":   cfg.memory_mb,
                "disks":       len(cfg.disks),
                "status":      status["state"],
            })
        return vms

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def vm_status(self, name: str) -> Dict[str, Any]:
        """Return state, PID, CPU%, RSS, uptime, and QMP internal status.

        Args:
            name: VM name.

        Returns:
            Dict with at minimum ``{"name": str, "state": "running"|"stopped"}``;
            running VMs also include ``pid``, ``cpu_percent``, ``rss_mb``,
            ``uptime_s``, and ``qemu_status``.

        Example::
            >>> mgr.vm_status("my-linux")
            {"name": "my-linux", "state": "stopped", "pid": None}
        """
        running = self._is_running(name)
        pid     = self._state.get_pid(name) if running else None
        status: Dict[str, Any] = {
            "name":  name,
            "state": "running" if running else "stopped",
            "pid":   pid,
        }

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

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------

    def monitor_vm(self, name: str) -> Dict[str, Any]:
        """Return a deep resource report: CPU times, IO counters, open files, QMP block stats.

        Args:
            name: VM name.

        Returns:
            Extended ``vm_status()`` result with ``cpu_times``, ``cpu_affinity``,
            ``disk_io``, ``open_files``, ``block_stats``, and ``timestamp``.

        Example::
            >>> mgr.monitor_vm("my-linux")
            {"name": "my-linux", "state": "running", "cpu_times": {...}, ...}
        """
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
            bs  = qmp.execute("query-blockstats")
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
        """Return ``monitor_vm()`` results for all running VMs.

        Returns:
            Dict keyed by VM name.

        Example::
            >>> mgr.monitor_all()
            {"vm1": {"state": "running", ...}, "vm2": {...}}
        """
        results: Dict[str, Any] = {
            name: self.monitor_vm(name) for name in list(self._procs.keys())
        }
        for vm in self.list_vms():
            if vm["name"] not in results and vm.get("status") == "running":
                results[vm["name"]] = self.monitor_vm(vm["name"])
        return results

    # ------------------------------------------------------------------
    # Display / shell
    # ------------------------------------------------------------------

    def open_display(self, name: str) -> Dict[str, Any]:
        """Launch the appropriate viewer (SPICE or VNC) for the VM's graphical output.

        Args:
            name: VM name (must be running).

        Returns:
            ``{"success": True, "message": str}`` or error dict.

        Example::
            >>> mgr.open_display("my-linux")
            {"success": True, "message": "Opened VNC display on port 5900."}
        """
        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}
        if not self._is_running(name):
            return {"success": False, "error": f"VM '{name}' is not running."}

        if cfg.display == "spice":
            port = cfg.spice_port or 5930
            conn = {"protocol": "spice", "host": "localhost", "port": port,
                    "connect": f"spice://localhost:{port}"}
            for viewer in ["remote-viewer", "spicy"]:
                if shutil.which(viewer):
                    subprocess.Popen([viewer, f"spice://localhost:{port}"])
                    return {"success": True, "message": f"Opened SPICE display on port {port}.",
                            **conn}
            if sys.platform == "darwin":
                subprocess.Popen(["open", f"spice://localhost:{port}"])
                return {"success": True, "message": f"SPICE on port {port}.", **conn}
            return {"success": True,
                    "message": f"SPICE display on port {port} — connect with: spice://localhost:{port}",
                    **conn}

        if cfg.display == "vnc":
            port = cfg.vnc_port or 5900
            conn = {"protocol": "vnc", "host": "localhost", "port": port,
                    "connect": f"localhost:{port}"}
            for viewer in ["vncviewer", "tigervnc", "xtigervncviewer"]:
                if shutil.which(viewer):
                    subprocess.Popen([viewer, f"localhost:{port}"])
                    return {"success": True, "message": f"Opened VNC display on port {port}.",
                            **conn}
            if sys.platform == "darwin":
                subprocess.Popen(["open", f"vnc://localhost:{port}"])
                return {"success": True, "message": f"Opening VNC in Screen Sharing on port {port}.",
                        **conn}
            if sys.platform == "win32":
                for viewer in ["tvnviewer", "vncviewer"]:
                    if shutil.which(viewer):
                        subprocess.Popen([viewer, f"localhost:{port}"])
                        return {"success": True, "message": f"Opened VNC display on port {port}.",
                                **conn}
            return {"success": True,
                    "message": f"VNC display on port {port} — connect with: vncviewer localhost:{port}",
                    **conn}

        return {"success": True, "protocol": cfg.display,
                "message": f"VM uses {cfg.display} — window should already be open."}

    def open_shell(self, name: str) -> Dict[str, Any]:
        """Open a serial console in the first available terminal emulator.

        Uses ``socat`` + Unix socket on Linux/macOS; ``telnet`` on Windows.

        Args:
            name: VM name (must be running).

        Returns:
            ``{"success": True, "message": str}`` or error dict.

        Example::
            >>> mgr.open_shell("my-linux")
            {"success": True, "message": "Opened serial console in xterm."}
        """
        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}
        if not self._is_running(name):
            return {"success": False, "error": f"VM '{name}' is not running."}

        if sys.platform == "win32":
            port = cfg.serial_tcp_port
            if not port:
                return {"success": False,
                        "error": "Serial TCP port not configured — launch the VM first."}
            subprocess.Popen(["cmd", "/c", "start", "telnet", "127.0.0.1", str(port)])
            return {"success": True, "serial_port": port,
                    "message": f"Opened serial console via telnet on port {port}."}

        serial_sock = os.path.join(cfg.get_vm_dir(), "serial.sock")
        conn = {"serial_sock": serial_sock,
                "connect": f"socat - UNIX-CONNECT:{serial_sock}"}
        if not os.path.exists(serial_sock):
            return {"success": False, "error": f"Serial socket not found: {serial_sock}",
                    **conn}

        if sys.platform == "darwin":
            script = f'tell app "Terminal" to do script "socat - UNIX-CONNECT:{serial_sock}"'
            subprocess.Popen(["osascript", "-e", script])
            return {"success": True, "message": "Opened serial console in Terminal.app.", **conn}

        for term in ["gnome-terminal", "xterm", "konsole", "lxterminal", "xfce4-terminal"]:
            if shutil.which(term):
                cmd = (
                    [term, "--", "socat", "-", f"UNIX-CONNECT:{serial_sock}"]
                    if term == "gnome-terminal"
                    else [term, "-e", f"socat - UNIX-CONNECT:{serial_sock}"]
                )
                subprocess.Popen(cmd)
                return {"success": True, "message": f"Opened serial console in {term}.", **conn}
        return {"success": True,
                "message": f"Serial socket: {serial_sock} — run: socat - UNIX-CONNECT:{serial_sock}",
                **conn}

"""
_vm_operations.py — VM Operations Mixin (disk, snapshots, resources, config, logs).

Provides _VmOperationsMixin which is composed into QemuManager.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from ._vm_constants import (
    _BUFFERS, _LOG_DEFAULT_LINES, _LOG_ERROR_PATTERNS,
    _MONITOR_ALLOWED_CMDS, _TIMEOUTS, _UPDATE_ALLOWED_FIELDS,
    _VALID_MACHINE_TYPES, VM_BASE_DIR,
)
from .qemu_config import MachineConfig
from .qemu_arg_builder import QemuArgBuilder
from .qmp_client import QMPClient


class _VmOperationsMixin:
    """Mixin providing disk ops, snapshots, resource limits, config, and log analysis."""

    # ------------------------------------------------------------------
    # Disk
    # ------------------------------------------------------------------

    def resize_disk(self, name: str, disk_index: int, new_size_gb: int) -> Dict[str, Any]:
        """Grow a stopped VM's disk image and update its config.

        Args:
            name:        VM name (must be stopped).
            disk_index:  Zero-based index into ``config.disks``.
            new_size_gb: New disk size in GiB (must be larger than current).

        Returns:
            ``{"success": True, "message": str}`` or error dict.

        Example::
            >>> mgr.resize_disk("my-linux", 0, 60)
            {"success": True, "message": "Disk 0 resized to 60GB. Remember to expand..."}
        """
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
        return {
            "success": True,
            "message": (
                f"Disk {disk_index} resized to {new_size_gb}GB. "
                "Remember to expand the partition inside the guest."
            ),
        }

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def snapshot_create(self, name: str, snap_name: str) -> Dict[str, Any]:
        """Create an internal qcow2 snapshot — live via QMP or offline via qemu-img.

        Live mode (VM running): uses ``blockdev-snapshot-internal-sync`` on each
        data disk, which works on UEFI/pflash VMs unlike the legacy ``savevm``
        HMP command.  Offline mode (VM stopped): uses ``qemu-img snapshot -c``.

        Args:
            name:      VM name.
            snap_name: Tag for the new snapshot.

        Returns:
            ``{"success": True, "message": str}`` or error dict.

        Example::
            >>> mgr.snapshot_create("my-linux", "pre-update")
            {"success": True, "message": "Snapshot 'pre-update' created on 1 disk(s)."}
        """
        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError:
            return {"success": False, "error": f"VM '{name}' does not exist."}
        if self._is_running(name):
            try:
                qmp = QMPClient(cfg.get_qmp_socket())
                qmp.connect()
                r       = qmp.execute("query-block")
                created = 0
                errors  = []
                for dev in r.get("return", []):
                    dev_name = dev.get("device", "")
                    # Skip pflash (OVMF vars/code), CD-ROMs, and read-only drives
                    if dev_name.startswith("pflash") or dev_name.startswith("cdrom"):
                        continue
                    inserted = dev.get("inserted")
                    if not inserted or inserted.get("ro", True):
                        continue
                    resp = qmp.execute("blockdev-snapshot-internal-sync",
                                       {"device": dev_name, "name": snap_name})
                    if "error" in resp:
                        errors.append(f"{dev_name}: {resp['error'].get('desc','?')}")
                    else:
                        created += 1
                qmp.close()
                if errors:
                    return {"success": False, "error": "; ".join(errors)}
                if created == 0:
                    return {"success": False, "error": "No writable data disks found to snapshot."}
                return {"success": True,
                        "message": f"Snapshot '{snap_name}' created on {created} disk(s)."}
            except Exception as e:
                return {"success": False, "error": str(e)}
        else:
            errors = []
            created = 0
            for disk in cfg.disks:
                disk_path = os.path.expanduser(disk.path)
                result = subprocess.run(
                    ["qemu-img", "snapshot", "-c", snap_name, disk_path],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    errors.append(result.stderr.strip())
                else:
                    created += 1
            if errors:
                return {"success": False, "error": "; ".join(errors)}
            return {"success": True,
                    "message": f"Snapshot '{snap_name}' created (offline) on {created} disk(s)."}

    def snapshot_list(self, name: str) -> Dict[str, Any]:
        """List all snapshots for a VM — live via QMP or offline via qemu-img.

        Args:
            name: VM name.

        Returns:
            ``{"success": True, "snapshots": list, "raw": str}`` or error dict.
            Each snapshot dict has ``id``, ``tag``, ``date``, ``vm_state_size``.

        Example::
            >>> mgr.snapshot_list("my-linux")
            {"success": True, "snapshots": [{"id": "1", "tag": "pre-update", ...}], ...}
        """
        try:
            cfg = MachineConfig.load(name)
            if not cfg.disks:
                return {"success": False, "error": "No disks."}
            if self._is_running(name):
                qmp  = QMPClient(cfg.get_qmp_socket())
                qmp.connect()
                r    = qmp.execute("query-block")
                qmp.close()
                snaps = []
                seen  = set()
                for dev in r.get("return", []):
                    for s in dev.get("inserted", {}).get("image", {}).get("snapshots", []):
                        tag = s.get("name", "")
                        if tag and tag not in seen:
                            seen.add(tag)
                            snaps.append({
                                "id":            s.get("id", ""),
                                "tag":           tag,
                                "date":          str(s.get("date-sec", "")),
                                "vm_state_size": s.get("vm-state-size", 0),
                            })
                return {"success": True, "snapshots": snaps, "raw": ""}
            else:
                disk_path = os.path.expanduser(cfg.disks[0].path)
                result    = subprocess.run(
                    ["qemu-img", "snapshot", "-l", disk_path],
                    capture_output=True, text=True,
                )
                snaps = []
                for line in result.stdout.splitlines()[2:]:
                    parts = line.split()
                    if len(parts) >= 4:
                        snaps.append({
                            "id":            parts[0],
                            "tag":           parts[1],
                            "date":          parts[3],
                            "vm_state_size": parts[2],
                        })
                return {"success": True, "snapshots": snaps, "raw": result.stdout}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def snapshot_restore(self, name: str, snap_name: str) -> Dict[str, Any]:
        """Restore a snapshot — live via QMP ``loadvm`` or offline via ``qemu-img -a``.

        Args:
            name:      VM name.
            snap_name: Tag of the snapshot to restore.

        Returns:
            ``{"success": True, "message": str}`` or error dict.
            Message indicates whether the restore was live or offline.

        Example::
            >>> mgr.snapshot_restore("my-linux", "pre-update")
            {"success": True, "message": "Snapshot 'pre-update' restored (offline)."}
        """
        if self._is_running(name):
            try:
                cfg  = MachineConfig.load(name)
                qmp  = QMPClient(cfg.get_qmp_socket())
                qmp.connect()
                resp = qmp.execute("human-monitor-command",
                                   {"command-line": f"loadvm {snap_name}"})
                qmp.close()
                # HMP loadvm returns empty string on success; any text is an error
                hmp_out = resp.get("return", "").strip()
                if hmp_out:
                    return {"success": False, "error": hmp_out}
                return {"success": True,
                        "message": f"Snapshot '{snap_name}' restored (live)."}
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
                return {"success": True,
                        "message": f"Snapshot '{snap_name}' restored (offline)."}
            except Exception as e:
                return {"success": False, "error": str(e)}

    def snapshot_delete(self, name: str, snap_name: str) -> Dict[str, Any]:
        """Delete a snapshot — live via QMP or offline via qemu-img.

        Args:
            name:      VM name.
            snap_name: Tag of the snapshot to delete.

        Returns:
            ``{"success": True, "message": str}`` or error dict.

        Example::
            >>> mgr.snapshot_delete("my-linux", "old-snap")
            {"success": True, "message": "Snapshot 'old-snap' deleted."}
        """
        cfg = MachineConfig.load(name)
        if self._is_running(name):
            try:
                qmp = QMPClient(cfg.get_qmp_socket())
                qmp.connect()
                r   = qmp.execute("query-block")
                deleted = 0
                errors  = []
                for dev in r.get("return", []):
                    dev_name = dev.get("device", "")
                    snaps    = dev.get("inserted", {}).get("image", {}).get("snapshots", [])
                    if not any(s.get("name") == snap_name for s in snaps):
                        continue
                    resp = qmp.execute("blockdev-snapshot-delete-internal-sync",
                                       {"device": dev_name, "name": snap_name})
                    if "error" in resp:
                        errors.append(f"{dev_name}: {resp['error'].get('desc','?')}")
                    else:
                        deleted += 1
                qmp.close()
                if errors:
                    return {"success": False, "error": "; ".join(errors)}
                if deleted == 0:
                    return {"success": False, "error": f"Snapshot '{snap_name}' not found."}
                return {"success": True, "message": f"Snapshot '{snap_name}' deleted."}
            except Exception as e:
                return {"success": False, "error": str(e)}
        else:
            errors  = []
            deleted = 0
            for disk in cfg.disks:
                disk_path = os.path.expanduser(disk.path)
                result = subprocess.run(
                    ["qemu-img", "snapshot", "-d", snap_name, disk_path],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    errors.append(result.stderr.strip())
                else:
                    deleted += 1
            if deleted == 0:
                return {"success": False, "error": "; ".join(errors) or f"Snapshot '{snap_name}' not found."}
            return {"success": True, "message": f"Snapshot '{snap_name}' deleted."}

    # ------------------------------------------------------------------
    # Resource limits
    # ------------------------------------------------------------------

    def set_resource_limits(
        self,
        name: str,
        cpu_percent: Optional[int] = None,
        memory_mb:   Optional[int] = None,
    ) -> Dict[str, Any]:
        """Cap CPU via cpulimit/cgroups and adjust balloon memory via QMP.

        Args:
            name:        VM name (must be running).
            cpu_percent: CPU cap as a percentage of one core (e.g. ``50``).
                         Linux only; no-op on other platforms.
            memory_mb:   Target balloon size in MiB.

        Returns:
            ``{"success": True, "name": str, "results": dict}`` where ``results``
            holds per-resource outcome or error keys.

        Example::
            >>> mgr.set_resource_limits("my-linux", cpu_percent=50, memory_mb=2048)
            {"success": True, "name": "my-linux", "results": {"cpu_limit": "...", ...}}
        """
        if not self._is_running(name):
            return {"success": False, "error": f"VM '{name}' is not running."}

        results: Dict[str, Any] = {}
        pid = self._state.get_pid(name)

        if cpu_percent is not None:
            if sys.platform != "linux":
                results["cpu_limit_error"] = (
                    f"CPU limiting via cpulimit/cgroups is Linux-only "
                    f"(current platform: {sys.platform})."
                )
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
                    period = 100_000
                    with open(f"{cgroup_path}/cpu.max", "w") as f:
                        f.write(f"{quota} {period}\n")
                    with open(f"{cgroup_path}/cgroup.procs", "w") as f:
                        f.write(str(pid))
                    results["cpu_limit"] = f"cgroup cpu.max set to {cpu_percent}%"
                except PermissionError:
                    results["cpu_limit_error"] = (
                        "Need sudo for cgroups. "
                        "Install cpulimit instead: sudo apt install cpulimit"
                    )

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

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def show_config(self, name: str) -> Dict[str, Any]:
        """Return the VM's full config as a dict.

        Args:
            name: VM name.

        Returns:
            ``{"success": True, "config": dict}`` or error dict.

        Example::
            >>> mgr.show_config("my-linux")
            {"success": True, "config": {"name": "my-linux", "memory_mb": 4096, ...}}
        """
        try:
            cfg = MachineConfig.load(name)
            return {"success": True, "config": cfg.to_dict()}
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

    def update_config(self, name: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Apply a dict of field updates to a stopped VM's config and persist it.

        Only fields present in ``_UPDATE_ALLOWED_FIELDS`` may be changed.

        Args:
            name:    VM name (must be stopped).
            updates: Mapping of field name → new value.

        Returns:
            ``{"success": True, "message": str}`` or error dict.

        Example::
            >>> mgr.update_config("my-linux", {"memory_mb": 8192})
            {"success": True, "message": "Updated ['memory_mb'] for 'my-linux'."}
        """
        if self._is_running(name):
            return {"success": False, "error": "Stop the VM before updating config."}
        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

        changed = []
        for key, value in updates.items():
            if key not in _UPDATE_ALLOWED_FIELDS:
                return {"success": False,
                        "error": f"Field '{key}' cannot be updated via API."}
            if not hasattr(cfg, key):
                return {"success": False, "error": f"Unknown config field: '{key}'"}
            setattr(cfg, key, value)
            changed.append(key)
        cfg.save()
        return {"success": True, "message": f"Updated {changed} for '{name}'."}

    # ------------------------------------------------------------------
    # Command
    # ------------------------------------------------------------------

    def print_command(self, name: str) -> Dict[str, Any]:
        """Build and return the full QEMU CLI string without executing it.

        Args:
            name: VM name.

        Returns:
            ``{"success": True, "command": str}`` or error dict.

        Example::
            >>> mgr.print_command("my-linux")
            {"success": True, "command": "qemu-system-x86_64 -name my-linux ..."}
        """
        try:
            cfg = MachineConfig.load(name)
            cmd = QemuArgBuilder(cfg).build()
            return {"success": True, "command": " ".join(cmd)}
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Monitor
    # ------------------------------------------------------------------

    def send_monitor_cmd(self, name: str, cmd: str) -> Dict[str, Any]:
        """Send a command to the QEMU human monitor socket.

        Only commands matching a prefix in ``_MONITOR_ALLOWED_CMDS`` are permitted.

        Args:
            name: VM name.
            cmd:  HMP command string (e.g. ``"info status"``).

        Returns:
            ``{"success": True, "output": str}`` or error dict.

        Example::
            >>> mgr.send_monitor_cmd("my-linux", "info status")
            {"success": True, "output": "VM status: running\\r\\n(qemu) "}
        """
        cmd_stripped = cmd.strip()
        if not any(
            cmd_stripped == allowed.rstrip() or cmd_stripped.startswith(allowed)
            for allowed in _MONITOR_ALLOWED_CMDS
        ):
            return {"success": False, "error": f"Command not permitted: '{cmd_stripped}'"}
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

    # ------------------------------------------------------------------
    # Disk check
    # ------------------------------------------------------------------

    def check_disk(self, name: str) -> Dict[str, Any]:
        """Run ``qemu-img info`` on every disk and diagnose blank/missing images.

        A disk is considered blank when its actual on-disk data is < 1 MiB
        (i.e. only the qcow2 header — no OS has been installed).

        Args:
            name: VM name.

        Returns:
            Dict with ``has_blank_disk``, per-disk ``disks`` list, ``diagnosis``,
            ``suggested_iso``, ``compatible_isos``, and ``suggestions``.

        Example::
            >>> mgr.check_disk("my-linux")
            {"success": True, "has_blank_disk": True, "diagnosis": "...", ...}
        """
        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

        disks_info: List[Dict[str, Any]] = []
        has_blank = False
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
            blank         = actual_bytes < 1024 * 1024
            if blank:
                has_blank = True
            disks_info.append({
                "index":           i,
                "path":            disk.path,
                "exists":          True,
                "blank":           blank,
                "actual_size_mb":  round(actual_bytes  / 1024**2, 2),
                "virtual_size_gb": round(virtual_bytes / 1024**3, 1),
                "format":          info.get("format", disk.format),
            })

        diagnosis:      str                    = ""
        suggestions:    List[str]              = []
        suggested_iso:  Optional[str]          = None
        compatible_isos: List[Dict[str, Any]]  = []

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
                    f"Auto-matched ISO based on os_type='{cfg.os_type}' "
                    f"os_name='{cfg.os_name}': {compatible_isos[0]['name']}",
                    f"Call update_config with iso_path='{suggested_iso}'",
                    "Call launch_vm — the VM will boot the ISO installer",
                    "After installation completes, call update_config with "
                    "iso_path=null to remove the ISO",
                ]
            elif compatible_isos:
                suggested_iso = compatible_isos[0]["path"]
                suggestions = [
                    f"No OS keyword match found — using first compatible ISO: "
                    f"{compatible_isos[0]['name']}",
                    f"Call update_config with iso_path='{suggested_iso}'",
                    "Call launch_vm — the VM will boot the ISO installer",
                    "After installation completes, call update_config with "
                    "iso_path=null to remove the ISO",
                ]
            else:
                suggestions = [
                    "No compatible ISO found on this system — download one first",
                    "Call scan_isos after placing the ISO in ~/Downloads or ~/Desktop",
                    "Call update_config with iso_path set to the ISO path",
                    "Call launch_vm — the VM will boot the ISO installer",
                    "After installation completes, call update_config with "
                    "iso_path=null to remove the ISO",
                ]

        return {
            "success":         True,
            "name":            name,
            "os_type":         cfg.os_type,
            "os_name":         cfg.os_name,
            "machine_arch":    cfg.machine_arch,
            "has_blank_disk":  has_blank,
            "disks":           disks_info,
            "diagnosis":       diagnosis,
            "suggested_iso":   suggested_iso,
            "compatible_isos": compatible_isos,
            "suggestions":     suggestions,
        }

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------

    def get_vm_logs(self, name: str, lines: int = _LOG_DEFAULT_LINES) -> Dict[str, Any]:
        """Read the VM launch log and return a structured failure report.

        Parses known error patterns and cross-checks the live config for common
        misconfigurations (wrong arch binary, missing hugepages, blank disks,
        architecture mismatches).

        Args:
            name:  VM name.
            lines: Number of log tail lines to scan (default from config).

        Returns:
            Dict with ``raw_tail``, ``errors``, ``warnings``, ``last_line``,
            ``diagnosis``, ``suggestions``, and optionally ``config_summary``.

        Example::
            >>> mgr.get_vm_logs("my-linux")
            {"name": "my-linux", "log_exists": True, "errors": [...], "diagnosis": "..."}
        """
        vm_dir   = os.path.join(VM_BASE_DIR, name)
        log_path = os.path.join(vm_dir, "launch.log")
        result: Dict[str, Any] = {
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
                    "line":    f"qemu_binary = {cfg.qemu_binary}",
                    "meaning": "Wrong QEMU binary — ARM machine needs qemu-system-aarch64",
                })
            if cfg.kvm and cfg.machine_arch in ("aarch64", "arm"):
                result["errors"].append({
                    "line":    "kvm=True on ARM guest",
                    "meaning": "KVM cannot be used for ARM guests on an x86 host",
                })
            if cfg.hugepages and sys.platform == "linux":
                try:
                    with open("/proc/sys/vm/nr_hugepages") as f:
                        if int(f.read().strip()) == 0:
                            result["errors"].append({
                                "line":    "hugepages=True but nr_hugepages=0",
                                "meaning": "Hugepages requested but not allocated on host — "
                                           "run: sudo sysctl vm.nr_hugepages=2048",
                            })
                except Exception:
                    pass
            if cfg.iso_path and not os.path.exists(cfg.iso_path):
                result["errors"].append({
                    "line":    f"iso_path = {cfg.iso_path}",
                    "meaning": f"ISO file not found: {cfg.iso_path}",
                })
            for i, disk in enumerate(cfg.disks):
                dp = os.path.expanduser(disk.path)
                if not os.path.exists(dp):
                    result["errors"].append({
                        "line":    f"disk[{i}].path = {disk.path}",
                        "meaning": f"Disk image not found: {dp}",
                    })
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
                                    "line":    f"disk[{i}] actual size = "
                                               f"{info.get('actual-size', 0)} bytes",
                                    "meaning": f"Disk {i} is blank — no OS installed. "
                                               "Attach an ISO and boot from it to install.",
                                })
                    except Exception:
                        pass
            if cfg.bios in ("ovmf", "ovmf_ms") and cfg.uefi:
                from .qemu_config import OVMF as _OVMF
                if not _OVMF["available"]:
                    result["errors"].append({
                        "line":    "bios=ovmf but OVMF not installed",
                        "meaning": "UEFI firmware not found — run: sudo apt install ovmf",
                    })

            mt = cfg.machine_type.lower().split(",")[0].strip()
            if mt not in _VALID_MACHINE_TYPES and not mt.startswith("pc-"):
                result["errors"].append({
                    "line":    f"machine_type = {cfg.machine_type}",
                    "meaning": (
                        f"'{cfg.machine_type}' is not a valid QEMU machine type — "
                        "it looks like a profile name was used by mistake. "
                        "Should be 'q35' for modern x86 or 'pc' for legacy."
                    ),
                })

            if cfg.iso_path:
                iso_lower  = os.path.basename(cfg.iso_path).lower()
                is_iso_arm = any(k in iso_lower for k in ("arm64", "aarch64", "arm_", "_arm"))
                is_iso_x86 = any(k in iso_lower for k in ("amd64", "x86_64", "x64", "i386", "i686"))
                is_vm_arm  = cfg.machine_arch in ("aarch64", "arm")
                is_vm_x86  = cfg.machine_arch == "x86_64"
                if is_iso_arm and is_vm_x86:
                    result["errors"].append({
                        "line":    f"iso={os.path.basename(cfg.iso_path)}, "
                                   f"arch={cfg.machine_arch}",
                        "meaning": "Architecture mismatch — ARM64 ISO cannot boot on an x86_64 VM.",
                    })
                elif is_iso_x86 and is_vm_arm:
                    result["errors"].append({
                        "line":    f"iso={os.path.basename(cfg.iso_path)}, "
                                   f"arch={cfg.machine_arch}",
                        "meaning": "Architecture mismatch — x86_64 ISO cannot boot on an ARM VM.",
                    })

            result["config_summary"] = {
                "qemu_binary":  cfg.qemu_binary,
                "machine_type": cfg.machine_type,
                "machine_arch": cfg.machine_arch,
                "kvm":          cfg.kvm,
                "bios":         cfg.bios,
                "hugepages":    cfg.hugepages,
                "iso_path":     cfg.iso_path,
                "display":      cfg.display,
                "memory_mb":    cfg.memory_mb,
                "disk_paths":   [d.path for d in cfg.disks],
            }
        except FileNotFoundError:
            result["config_error"] = f"No config found for VM '{name}'"
        except Exception as e:
            result["config_error"] = str(e)

        if result["errors"] and not result["diagnosis"]:
            result["diagnosis"] = result["errors"][0]["meaning"]

        suggestions: List[str] = []
        for err in result["errors"]:
            m = err["meaning"].lower()
            if "hugepages"              in m:
                suggestions.append("sudo sysctl vm.nr_hugepages=2048")
            if "kvm permission"         in m:
                suggestions.append("sudo usermod -aG kvm $USER  (then log out and back in)")
            if "arm" in m and "binary"  in m:
                suggestions.append("sudo apt install qemu-system-arm")
            if "ovmf" in m or "uefi"    in m:
                suggestions.append("sudo apt install ovmf")
            if "no bootable"            in m:
                suggestions.append("Check iso_path in VM config — run: qemu-api config " + name)
            if "blank" in m and "disk"  in m:
                suggestions.append(
                    "Call scan_isos to find an ISO, then update_config with iso_path, "
                    "then launch_vm"
                )
            if "port" in m or "address already" in m:
                suggestions.append(
                    "Change vnc_port or spice_port in VM config to a free port"
                )
            if "display"                in m:
                suggestions.append("Check DISPLAY env var: echo $DISPLAY  (should be :0 or :1)")
            if "not a valid qemu machine type" in m or "profile name" in m:
                suggestions.append(
                    f"Fix machine_type: run: qemu-api cmd {name} '' — "
                    "or delete and recreate the VM with machine_type=q35"
                )
            if "architecture mismatch"  in m:
                suggestions.append(
                    "Fix: delete the VM and recreate it — "
                    "the ISO arch and VM arch must match"
                )
        result["suggestions"] = list(dict.fromkeys(suggestions))

        return result

"""
_vm_diagnostics.py — VM diagnostics mixin (disk inspection + log analysis).

Provides _VmDiagnosticsMixin (check_disk, get_vm_logs), composed into
QemuManager alongside _VmOperationsMixin. Split out of _vm_operations.py to keep
each mixin focused; these two methods are the file's heaviest.
"""
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

from ._vm_constants import (
    _LOG_DEFAULT_LINES, _LOG_ERROR_PATTERNS, _VALID_MACHINE_TYPES, VM_BASE_DIR,
)
from .qemu_config import MachineConfig


class _VmDiagnosticsMixin:
    """Mixin providing disk-image inspection and VM log collection/analysis."""

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
                    pass  # hugepages diagnostic is advisory — skip it if the host read fails
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
                        pass  # disk-size diagnostic is advisory — skip it if the query fails
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


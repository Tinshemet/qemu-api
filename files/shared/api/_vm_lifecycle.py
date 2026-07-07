"""
_vm_lifecycle.py — VM Lifecycle Mixin (create / clone / delete / scan).

Provides _VmLifecycleMixin which is composed into QemuManager.
"""
import json
import os
import shutil
import subprocess
import uuid as _uuid
from typing import Any, Dict, List

from ._vm_constants import _MACOS_OVMF, _WIN_OVMF, VM_BASE_DIR
from .qemu_config import DiskConfig, MachineConfig, NetworkConfig, OVMF, apply_os_hints
from .qemu_arg_builder import SPICE_PORT_START, VNC_PORT_START, next_free_port, build_iso_search_dirs

_CFG = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_ISO_OS_KEYWORDS  = _CFG.get("iso_os_keywords", {})
_ISO_ARM_MARKERS  = tuple(_CFG.get("iso_arm_markers", []))
_ISO_X86_MARKERS  = tuple(_CFG.get("iso_x86_markers", []))


class _VmLifecycleMixin:
    """Mixin providing VM create, clone, delete, and ISO-scan operations."""

    # ------------------------------------------------------------------
    # ISO scan
    # ------------------------------------------------------------------

    def scan_isos(self) -> List[Dict[str, str]]:
        """Scan common directories for ISO files.

        Returns:
            list of ``{"name": str, "path": str, "size_gb": float}``.

        Example::
            >>> mgr.scan_isos()
            [{"name": "ubuntu-24.04.iso", "path": "/home/user/Downloads/...", "size_gb": 5.1}]
        """
        found: List[Dict[str, str]] = []
        seen: set = set()
        for d in build_iso_search_dirs():
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

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_vm(self, config: MachineConfig, force: bool = False) -> Dict[str, Any]:
        """Create VM directory, OVMF vars, auto-assign ports, and disk images.

        Args:
            config: Fully populated MachineConfig. The ``name`` field must be
                    set. ``force`` is deprecated — callers must pre-delete.
            force:  Deprecated; ignored. Callers should call delete_vm first.

        Returns:
            ``{"success": True, "name": str, "vm_dir": str, "bios": str,
            "uefi": bool, "iso_path": str, "message": str}`` on success,
            or ``{"success": False, "error": str}`` on failure.

        Example::
            >>> from shared.api.qemu_config import MachineConfig
            >>> cfg = MachineConfig(name="test", os_type="linux")
            >>> mgr.create_vm(cfg)
            {"success": True, "name": "test", "vm_dir": "~/.qemu_vms/test", ...}
        """
        vm_dir = config.get_vm_dir()
        if os.path.exists(vm_dir) and not force:
            return {"success": False,
                    "error": f"VM '{config.name}' already exists. Use force=True to overwrite."}

        os.makedirs(vm_dir, exist_ok=True)
        config = apply_os_hints(config)

        # UEFI VARS — find, copy, and bind
        if config.bios in ("ovmf", "ovmf_ms"):
            vars_dst = os.path.join(vm_dir, "OVMF_VARS.fd")
            if not os.path.exists(vars_dst):
                code_path  = OVMF.get("code", "")
                prefer_4m  = "4M" in (code_path or "")

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
            config.vnc_port = next_free_port(VNC_PORT_START, used_vnc)
        if config.display == "spice" and not config.spice_port:
            config.spice_port = next_free_port(SPICE_PORT_START, used_spice)

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
                config.iso_path   = matches[0]["path"]
                config.boot_order = "dc"

        config.save()
        _iso_basename = os.path.basename(config.iso_path) if config.iso_path else ""
        return {
            "success":  True,
            "name":     config.name,
            "vm_dir":   vm_dir,
            "bios":     config.bios,
            "uefi":     config.uefi,
            "iso_path": config.iso_path,
            "iso_name": _iso_basename,
            "os_name":  config.os_name,
            "message":  (
                f"VM '{config.name}' created successfully."
                + (f" Attached ISO: {_iso_basename} (os_name={config.os_name})"
                   if _iso_basename else " No ISO attached.")
            ),
        }

    # ------------------------------------------------------------------
    # Clone
    # ------------------------------------------------------------------

    def clone_vm(self, source_name: str, new_name: str) -> Dict[str, Any]:
        """Clone an existing VM — creates a CoW qcow2 copy and a fresh config.

        Args:
            source_name: Name of the VM to clone (must be stopped).
            new_name:    Name for the new VM.

        Returns:
            ``{"success": True, "message": str, "new_vm": str}`` or error dict.

        Example::
            >>> mgr.clone_vm("base-linux", "dev-machine")
            {"success": True, "message": "...", "new_vm": "dev-machine"}
        """
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
                    ["qemu-img", "create", "-f", "qcow2",
                     "-b", src_path, "-F", disk.format, new_path],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    shutil.rmtree(new_vm_dir)
                    return {"success": False, "error": f"Disk clone failed: {result.stderr}"}
            new_disks.append(DiskConfig(
                path=new_path, size_gb=disk.size_gb, format="qcow2", bus=disk.bus,
            ))

        src_vars = os.path.join(src_cfg.get_vm_dir(), "OVMF_VARS.fd")
        if os.path.exists(src_vars):
            shutil.copy2(src_vars, os.path.join(new_vm_dir, "OVMF_VARS.fd"))

        src_cfg.name      = new_name
        src_cfg.vm_id     = str(_uuid.uuid4())[:8]
        src_cfg.disks     = new_disks
        src_cfg.uefi_vars = os.path.join(new_vm_dir, "OVMF_VARS.fd")
        for net in src_cfg.networks:
            net.mac = None
            net.__post_init__()

        src_cfg.save()
        return {"success": True,
                "message": f"VM '{source_name}' cloned to '{new_name}'.",
                "new_vm": new_name}

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_vm(self, name: str, delete_disks: bool = False) -> Dict[str, Any]:
        """Remove a VM's directory and optionally its disk images.

        Args:
            name:         VM name (must be stopped).
            delete_disks: When True, also remove disk image files listed in
                          config before deleting the directory.

        Returns:
            ``{"success": True, "message": str}``; ``message`` may include a
            warning if individual disk files could not be removed.

        Example::
            >>> mgr.delete_vm("old-test", delete_disks=True)
            {"success": True, "message": "VM 'old-test' deleted."}
        """
        if self._is_running(name):
            return {"success": False, "error": "Stop the VM before deleting."}
        vm_dir = os.path.join(VM_BASE_DIR, name)
        if not os.path.exists(vm_dir):
            return {"success": False, "error": f"VM '{name}' not found."}

        disk_errors: List[str] = []
        if delete_disks:
            try:
                cfg = MachineConfig.load(name)
                for disk in cfg.disks:
                    p = os.path.expanduser(disk.path)
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except OSError as e:
                            disk_errors.append(f"{p}: {e}")
            except FileNotFoundError:
                pass

        shutil.rmtree(vm_dir)
        self._state.set_stopped(name)
        msg = f"VM '{name}' deleted."
        if disk_errors:
            msg += f" Warning: could not remove disk(s): {'; '.join(disk_errors)}"
        return {"success": True, "message": msg}

    # ------------------------------------------------------------------
    # ISO matching (private helper — used by create_vm and check_disk)
    # ------------------------------------------------------------------

    def _match_iso(
        self, os_type: str, os_name: str, machine_arch: str
    ) -> List[Dict[str, Any]]:
        """Score available ISOs against os_type/os_name keywords, filtering by arch.

        Args:
            os_type:      Normalised OS type (``"linux"``, ``"windows"``, …).
            os_name:      More specific OS name (``"ubuntu"``, ``"mint"``, …).
            machine_arch: VM architecture (``"x86_64"`` or ``"aarch64"``).

        Returns:
            List of ISO dicts (from ``scan_isos()``) extended with
            ``"match_score": int``. Higher score → better match.
        """
        os_type_l = (os_type or "").lower()
        os_name_l = (os_name or "").lower()
        vm_is_x86 = machine_arch == "x86_64"
        vm_is_arm = machine_arch in ("aarch64", "arm")

        generic_keywords: List[str] = []
        for key, kws in _ISO_OS_KEYWORDS.items():
            if key in os_type_l or key in os_name_l:
                generic_keywords.extend(kws)

        # Words from os_name get 10× bonus so "ubuntu" always outranks "linux".
        specific_words = [w for w in os_name_l.split() if len(w) > 3]

        results: List[Dict[str, Any]] = []
        for iso in self.scan_isos():
            fname = iso["name"].lower()
            if vm_is_x86 and any(m in fname for m in _ISO_ARM_MARKERS):
                continue
            if vm_is_arm and any(m in fname for m in _ISO_X86_MARKERS):
                continue
            specific_score = sum(10 for w in specific_words if w in fname)
            generic_score  = sum(1  for kw in generic_keywords
                                 if kw in fname and kw not in specific_words)
            results.append({**iso, "match_score": specific_score + generic_score})
        return results

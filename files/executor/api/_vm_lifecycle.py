"""
_vm_lifecycle.py — VM Lifecycle Mixin (create / clone / delete / scan).

Provides _VmLifecycleMixin which is composed into QemuManager.
"""
import json
import os
import secrets
import shutil
import subprocess
import uuid as _uuid
from typing import Any, Dict, List

from ._vm_constants import (
    _MACOS_OVMF, _WIN_OVMF, VM_BASE_DIR, TEMPLATES_DIR, TEMPLATE_LABEL, infer_os_name,
)
from .qemu_config import DiskConfig, MachineConfig, NetworkConfig, OVMF, apply_os_hints
from .qemu_arg_builder import SPICE_PORT_START, VNC_PORT_START, next_free_port, build_iso_search_dirs
from .label_registry import register_label

_CFG = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_ISO_OS_KEYWORDS  = _CFG.get("iso_os_keywords", {})
_ISO_ARM_MARKERS  = tuple(_CFG.get("iso_arm_markers", []))
_ISO_X86_MARKERS  = tuple(_CFG.get("iso_x86_markers", []))


def _template_dir(name: str) -> str:
    """Return the golden-image directory for a template name."""
    return os.path.join(TEMPLATES_DIR, name)


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
            >>> from executor.api.qemu_config import MachineConfig
            >>> cfg = MachineConfig(name="test", os_type="linux")
            >>> mgr.create_vm(cfg)
            {"success": True, "name": "test", "vm_dir": "~/.qemu_vms/test", ...}
        """
        vm_dir = config.get_vm_dir()
        # `force` is deprecated and intentionally ignored (see docstring): an
        # in-place overwrite skipped existing disk files (create_disks' `continue`)
        # while config.save() rewrote the config — binding a fresh config to STALE
        # disk contents. Callers must delete_vm first. No in-tree caller passes it.
        if os.path.exists(vm_dir):
            return {"success": False,
                    "error": f"VM '{config.name}' already exists — delete it first."}

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

        # Create disk images — either blank, or backing-file clones of a golden
        # template's disks when config.template names one (see mark_as_template()).
        template_meta = None
        if config.template:
            template_json = os.path.join(_template_dir(config.template), "template.json")
            if not os.path.exists(template_json):
                return {"success": False, "error": f"Template '{config.template}' not found."}
            with open(template_json) as _f:
                template_meta = json.load(_f)

        for i, disk in enumerate(config.disks):
            disk_path = os.path.expanduser(disk.path)
            if os.path.exists(disk_path):
                continue
            os.makedirs(os.path.dirname(disk_path), exist_ok=True)
            if template_meta:
                template_disk = os.path.join(_template_dir(config.template), f"disk{i}.qcow2")
                if not os.path.exists(template_disk):
                    return {"success": False,
                            "error": f"Template '{config.template}' has no disk{i} — it has "
                                     f"{len(template_meta.get('disks', []))} disk(s)."}
                result = subprocess.run(
                    ["qemu-img", "create", "-f", "qcow2",
                     "-b", template_disk, "-F", "qcow2", disk_path],
                    capture_output=True, text=True,
                )
            else:
                result = subprocess.run(
                    ["qemu-img", "create", "-f", disk.format, disk_path, f"{disk.size_gb}G"],
                    capture_output=True, text=True,
                )
            if result.returncode != 0:
                return {"success": False, "error": f"qemu-img failed: {result.stderr}"}

        # Golden-image clones inherit the template disk's /etc/shadow byte-for-byte —
        # every clone shares the exact same root password unless something changes it.
        # Offline-edit it on this new disk before the VM ever boots. Linux only
        # (Windows credentials live in the SAM hive, not /etc/shadow — needs a
        # different tool). Best-effort: a failure here shouldn't block VM creation,
        # since the clone is already usable with the template's original password.
        root_password_warning = None
        new_root_password = None
        user_password_warning = None
        new_user_password = None
        randomized_username = None
        username_rename_warning = None
        renamed_username = None
        _is_linux_template = (template_meta
                               and "windows" not in template_meta.get("os_type", "").lower())
        if config.randomize_root_password and _is_linux_template and config.disks:
            from ._vm_credentials import randomize_root_password
            try:
                new_root_password = randomize_root_password(
                    os.path.expanduser(config.disks[0].path))
                config.root_password = new_root_password
            except Exception as _e:
                root_password_warning = str(_e)
        if config.new_username and _is_linux_template and config.disks:
            from ._vm_credentials import rename_user
            try:
                renamed_username = rename_user(
                    os.path.expanduser(config.disks[0].path), config.new_username)
            except Exception as _e:
                username_rename_warning = str(_e)
        if config.randomize_user_password and _is_linux_template and config.disks:
            from ._vm_credentials import find_primary_user, randomize_user_password
            _disk_path = os.path.expanduser(config.disks[0].path)
            try:
                # Target whichever account is actually current — the just-renamed
                # one if new_username was also given this call, else auto-detect.
                randomized_username = renamed_username or find_primary_user(_disk_path)
                if not randomized_username:
                    raise RuntimeError("could not auto-detect a primary user account on this disk")
                new_user_password = randomize_user_password(_disk_path, randomized_username)
                config.user_password = new_user_password
                config.randomized_username = randomized_username
            except Exception as _e:
                user_password_warning = str(_e)

        # Hostname/computer-name randomization applies to both Linux and
        # Windows template clones (unlike the credential fields above, which
        # are Linux-only since Windows credentials need a different tool).
        hostname_warning = None
        new_hostname = None
        if config.randomize_hostname and template_meta and config.disks:
            _disk_path = os.path.expanduser(config.disks[0].path)
            _is_windows_template = "windows" in template_meta.get("os_type", "").lower()
            try:
                if _is_windows_template:
                    from ._vm_hostname import randomize_windows_hostname
                    new_hostname = randomize_windows_hostname(_disk_path, config.new_hostname)
                else:
                    from ._vm_hostname import randomize_linux_hostname
                    new_hostname = randomize_linux_hostname(_disk_path, config.new_hostname)
                config.new_hostname = new_hostname
            except Exception as _e:
                hostname_warning = str(_e)

        if template_meta and not config.os_type:
            config.os_type = template_meta.get("os_type", config.os_type)

        # Auto-attach a matching ISO if none was provided — but never for a
        # template-based clone, which already has a real, bootable OS on disk.
        # Attaching an install ISO there just risks the VM booting the
        # installer's own boot menu instead of the cloned OS (the CD-ROM device
        # gets an explicit bootindex; the cloned disk doesn't).
        if not config.iso_path and not template_meta:
            matches = self._match_iso(config.os_type, config.os_name, config.machine_arch)
            matches.sort(key=lambda x: x["match_score"], reverse=True)
            if matches and matches[0]["match_score"] > 0:
                config.iso_path   = matches[0]["path"]
                config.boot_order = "dc"

        # Infer os_name from ISO filename when not explicitly provided
        if config.iso_path and not config.os_name:
            config.os_name = infer_os_name(config.iso_path, config.os_type)

        # Stealth VMs can't use the standard virtio-serial QGA channel (a
        # hypervisor tell), so they get a PSK-authenticated serial-agent
        # channel instead (see _vm_guest.py / qemu_arg_builder._serial_agent).
        # Generate the PSK once, here, at creation time — never lazily in
        # generate_guest_agent_setup, which can be called repeatedly to
        # re-serve the install script and must keep handing out the same PSK
        # the guest was actually provisioned with.
        if config.stealth and config.guest_agent and not config.guest_agent_psk:
            config.guest_agent_psk = secrets.token_hex(32)

        config.save()
        _iso_basename = os.path.basename(config.iso_path) if config.iso_path else ""
        _message = (
            f"VM '{config.name}' created successfully."
            + (f" Attached ISO: {_iso_basename} (os_name={config.os_name})"
               if _iso_basename else " No ISO attached.")
        )
        if renamed_username:
            _message += f" Renamed user to '{renamed_username}'."
        elif username_rename_warning:
            _message += f" (user NOT renamed — {username_rename_warning})"
        if new_root_password:
            _message += f" New root password: {new_root_password}"
        elif root_password_warning:
            _message += f" (root password NOT randomized — {root_password_warning})"
        if new_user_password:
            _message += f" New '{randomized_username}' password: {new_user_password}"
        elif user_password_warning:
            _message += f" (user password NOT randomized — {user_password_warning})"
        if new_hostname:
            _message += f" New hostname: {new_hostname}"
        elif hostname_warning:
            _message += f" (hostname NOT randomized — {hostname_warning})"
        result = {
            "success":  True,
            "name":     config.name,
            "vm_dir":   vm_dir,
            "bios":     config.bios,
            "uefi":     config.uefi,
            "iso_path": config.iso_path,
            "iso_name": _iso_basename,
            "os_name":  config.os_name,
            "message":  _message,
        }
        if renamed_username:
            result["renamed_username"] = renamed_username
        if new_root_password:
            result["root_password"] = new_root_password
        if new_user_password:
            result["user_password"] = new_user_password
            result["randomized_username"] = randomized_username
        if new_hostname:
            result["hostname"] = new_hostname
        return result

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

        # A clone is a disposable, ordinary VM — not the protected golden
        # image it was cut from. Strip the "template" tag so delete_vm and
        # any other template-aware logic don't mistake the clone for one.
        src_cfg.labels = [l for l in src_cfg.labels if l != TEMPLATE_LABEL]

        # The source's disk is already installed — a clone must boot straight
        # into it, never repeat the source's install-time boot path (which
        # would re-run the OS installer against the fresh CoW disk instead of
        # booting the OS already on it).
        src_cfg.unattended  = False
        src_cfg.iso_path    = ""
        src_cfg.kernel_path = ""
        src_cfg.initrd_path = ""
        src_cfg.kernel_cmdline = ""

        # A clone must not share the source's stealth serial-agent PSK — that
        # secret, if the source ever had generate_guest_agent_setup run
        # against it, would let two different guests authenticate as the same
        # channel. Clear it here; the clone's disk (copied byte-for-byte from
        # the source) may still have the OLD PSK installed in-guest until
        # generate_guest_agent_setup is re-run and the setup script re-served.
        src_cfg.guest_agent_psk = ""

        src_cfg.save()
        return {"success": True,
                "message": f"VM '{source_name}' cloned to '{new_name}'.",
                "new_vm": new_name}

    # ------------------------------------------------------------------
    # Templates (golden images)
    # ------------------------------------------------------------------

    def mark_as_template(self, name: str) -> Dict[str, Any]:
        """Snapshot a stopped VM's current disk state into a reusable golden template.

        Flattens each disk (qemu-img convert, not a backing-file link) into
        ``~/.qemu_vms/_templates/<name>/diskN.qcow2`` so the template never depends on the
        source VM's own disk surviving. Tags the source VM with the protected "template"
        label; the template.json copy also records "template" in its own labels.

        Args:
            name: VM to snapshot (must be stopped).

        Returns:
            ``{"success": True, "message": str, "template": str}`` or error dict.

        Example::
            >>> mgr.mark_as_template("vm_perfect_kali")
            {"success": True, "message": "...", "template": "vm_perfect_kali"}
        """
        if self._is_running(name):
            return {"success": False, "error": "Stop the VM before marking it as a template."}
        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

        if TEMPLATE_LABEL in cfg.labels:
            return {"success": False, "error": f"'{name}' is already marked as a template."}

        tmpl_dir = _template_dir(name)
        if os.path.exists(tmpl_dir):
            return {"success": False, "error": f"A template named '{name}' already exists."}
        os.makedirs(tmpl_dir, exist_ok=True)

        disks_meta = []
        for i, disk in enumerate(cfg.disks):
            src_path = os.path.expanduser(disk.path)
            dst_path = os.path.join(tmpl_dir, f"disk{i}.qcow2")
            if not os.path.exists(src_path):
                shutil.rmtree(tmpl_dir)
                return {"success": False, "error": f"Disk not found: {src_path}"}
            result = subprocess.run(
                ["qemu-img", "convert", "-O", "qcow2", src_path, dst_path],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                shutil.rmtree(tmpl_dir)
                return {"success": False, "error": f"Template disk conversion failed: {result.stderr}"}
            disks_meta.append({"size_gb": disk.size_gb, "format": "qcow2", "bus": disk.bus})

        with open(os.path.join(tmpl_dir, "template.json"), "w") as f:
            json.dump({
                "name":     name,
                "os_type":  cfg.os_type,
                "disks":    disks_meta,
                "labels":   [TEMPLATE_LABEL],
            }, f, indent=2)

        cfg.labels.append(TEMPLATE_LABEL)
        cfg.save()
        register_label(TEMPLATE_LABEL)

        return {"success": True,
                "message": f"'{name}' marked as a template ({len(disks_meta)} disk(s) saved to {tmpl_dir}).",
                "template": name}

    def remove_template(self, name: str) -> Dict[str, Any]:
        """Delete a golden template's disk copy and un-tag the source VM if it still exists.

        Gated behind a Yes/Cancel confirmation at the preflight layer (see
        _PREFLIGHT_TOOLS/"remove_template" in the preflight validator) — this method itself
        performs the deletion unconditionally once called, same as delete_vm.

        Args:
            name: Template name (matches the VM name it was created from).

        Returns:
            ``{"success": True, "message": str}`` or error dict.

        Example::
            >>> mgr.remove_template("vm_perfect_kali")
            {"success": True, "message": "Template 'vm_perfect_kali' removed."}
        """
        tmpl_dir = _template_dir(name)
        if not os.path.exists(tmpl_dir):
            return {"success": False, "error": f"No template named '{name}'."}
        shutil.rmtree(tmpl_dir)

        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError:
            return {"success": True, "message": f"Template '{name}' removed (source VM no longer exists)."}

        if TEMPLATE_LABEL in cfg.labels:
            cfg.labels = [l for l in cfg.labels if l != TEMPLATE_LABEL]
            cfg.save()
        return {"success": True, "message": f"Template '{name}' removed."}

    def list_templates(self) -> List[Dict[str, Any]]:
        """List every registered golden-image template.

        Returns:
            List of ``{"name": str, "os_type": str, "disks": int}`` dicts.

        Example::
            >>> mgr.list_templates()
            [{"name": "vm_perfect_kali", "os_type": "linux", "disks": 1}]
        """
        if not os.path.isdir(TEMPLATES_DIR):
            return []
        result = []
        for name in sorted(os.listdir(TEMPLATES_DIR)):
            meta_path = os.path.join(TEMPLATES_DIR, name, "template.json")
            if not os.path.isfile(meta_path):
                continue
            with open(meta_path) as f:
                meta = json.load(f)
            result.append({
                "name":    name,
                "os_type": meta.get("os_type", ""),
                "disks":   len(meta.get("disks", [])),
            })
        return result

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
                pass  # VM dir already gone — nothing left to remove

        shutil.rmtree(vm_dir)
        self._state.set_stopped(name)
        self.iso_nets.remove_vm_from_all_networks(name)
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

"""
linux.py — unattended Linux installs (Ubuntu/Mint via casper/Subiquity autoinstall,
Kali via classic debian-installer preseed). Edit the templates in ``templates/``
directly to change what gets automated; add a new distro by adding an entry to
``unattended_linux`` in ../config.json (see the package docstring in __init__.py).

QEMU can only pass a custom kernel boot parameter (autoinstall/preseed pointer) via
direct kernel boot (-kernel/-initrd/-append), not by just attaching an ISO as -cdrom —
so this extracts the installer's own kernel+initrd from the ISO (xorriso, no root/mount
needed) and boots those directly, same as windows.py's Windows answer-file CD does
conceptually (an opt-in second medium the installer auto-detects).

Two distinct mechanisms, one per installer family:
  - casper (Ubuntu/Mint): a small ISO volume labeled "cidata" holding cloud-init
    user-data/meta-data (NoCloud datasource) — cloud-init auto-detects it by volume
    label, no exact mount path needed. `interactive-sections: [identity]` in the
    autoinstall YAML automates everything else but leaves account creation prompted.
  - debian-installer (Kali): no separate medium — the extracted initrd already
    contains a default preseed.cfg at its root; a small cpio archive containing just
    our replacement preseed.cfg is concatenated onto the original initrd.gz. The
    kernel's initramfs unpacker (unlike the plain `cpio` tool) processes concatenated
    cpio archives in sequence with later files overriding earlier ones — confirmed by
    manually simulating that sequential-extraction-with-override behavior against the
    real Kali initrd before relying on it here. passwd/* questions are left unset so
    account creation is still prompted.
"""

import gzip
import json
import os
import subprocess
import tempfile

from .windows import iso_tool_available

_HERE = os.path.dirname(__file__)
_TEMPLATES_DIR = os.path.join(_HERE, "templates")
with open(os.path.join(os.path.dirname(_HERE), "config.json")) as _f:
    _CFG = json.load(_f)
_UNATTENDED_LINUX = _CFG.get("unattended_linux", {})


def iso_extract(iso_path: str, internal_path: str, dest_path: str) -> None:
    """Extract one file from an ISO9660 image without mounting (needs no root).

    Example::
        iso_extract("/isos/ubuntu.iso", "/casper/vmlinuz", "/tmp/vmlinuz")
    """
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    subprocess.run(
        ["xorriso", "-indev", iso_path, "-osirrox", "on",
         "-extract", internal_path, dest_path],
        check=True, capture_output=True,
    )


def linux_autoinstall_config(os_name: str) -> dict:
    """Return the unattended-install metadata for a distro name, or {} if unsupported.

    Example::
        linux_autoinstall_config("ubuntu")
        # -> {"installer_family": "casper", "kernel_path": "/casper/vmlinuz", ...}
    """
    return _UNATTENDED_LINUX.get((os_name or "").strip().lower(), {})


def extract_kernel_initrd(iso_path: str, vm_dir: str, os_name: str) -> "tuple[str, str]":
    """Extract the installer's kernel + initrd for a supported distro into vm_dir.

    Returns (kernel_path, initrd_path) on the local filesystem.

    Raises:
        ValueError: os_name has no unattended_linux config entry.

    Example::
        extract_kernel_initrd("/isos/ubuntu.iso", "/home/u/.qemu_vms/dev", "ubuntu")
        # -> ("/home/u/.qemu_vms/dev/linux-kernel", "/home/u/.qemu_vms/dev/linux-initrd")
    """
    meta = linux_autoinstall_config(os_name)
    if not meta:
        raise ValueError(f"No unattended-install support for os_name={os_name!r}")
    kernel_dest = os.path.join(vm_dir, "linux-kernel")
    initrd_dest = os.path.join(vm_dir, "linux-initrd")
    iso_extract(iso_path, meta["kernel_path"], kernel_dest)
    iso_extract(iso_path, meta["initrd_path"], initrd_dest)
    return kernel_dest, initrd_dest


def _fill_template(name: str, *, locale: str, keyboard: str) -> str:
    with open(os.path.join(_TEMPLATES_DIR, name)) as f:
        text = f.read()
    return text.replace("__LOCALE__", locale).replace("__KEYBOARD__", keyboard)


# ── casper / Subiquity (Ubuntu, Mint) ───────────────────────────────────────────

def generate_cidata_iso(vm_dir: str, *, locale: str = "en_US.UTF-8",
                         keyboard: str = "us") -> str:
    """Build <vm_dir>/cidata.iso — a NoCloud (cloud-init) datasource volume that
    automates a casper/Subiquity install except the account-creation screen.

    cloud-init's NoCloud datasource finds this by volume label ("cidata"), not by
    mount path, so it works regardless of which QEMU device index it lands on.

    Returns the ISO path.

    Raises:
        RuntimeError: no xorriso/genisoimage/mkisofs available.

    Example::
        generate_cidata_iso("/home/u/.qemu_vms/dev")
        # -> "/home/u/.qemu_vms/dev/cidata.iso"
    """
    tool = iso_tool_available()
    if not tool:
        raise RuntimeError(
            "no ISO tool (genisoimage/xorriso/mkisofs) — cannot build the cidata volume"
        )
    os.makedirs(vm_dir, exist_ok=True)
    iso_path = os.path.join(vm_dir, "cidata.iso")
    user_data = _fill_template("casper-user-data.yaml.template", locale=locale, keyboard=keyboard)
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, "user-data"), "w") as f:
            f.write(user_data)
        with open(os.path.join(td, "meta-data"), "w") as f:
            f.write("instance-id: gorgon-autoinstall\nlocal-hostname: linux-vm\n")
        if tool == "xorriso":
            cmd = ["xorriso", "-as", "mkisofs", "-J", "-R", "-V", "cidata", "-o", iso_path, td]
        else:
            cmd = [tool, "-J", "-R", "-V", "cidata", "-o", iso_path, td]
        subprocess.run(cmd, check=True, capture_output=True)
    return iso_path


# ── debian-installer (Kali) ──────────────────────────────────────────────────────

def inject_preseed_into_initrd(base_initrd_path: str, vm_dir: str, template_name: str, *,
                                 locale: str = "en_US.UTF-8", keyboard: str = "us") -> str:
    """Build <vm_dir>/linux-initrd-preseeded by concatenating a small cpio archive
    (containing an updated preseed.cfg, filled in from templates/<template_name>)
    onto the extracted initrd.

    The kernel's initramfs unpacker processes concatenated cpio archives in order,
    with later files overriding earlier ones of the same name — this is the standard
    "initrd injection" preseeding technique, confirmed here against the real Kali
    initrd by simulating that sequential-extraction-with-override behavior (the
    plain `cpio` CLI tool does NOT auto-continue past the first archive's trailer
    the way the kernel does, so don't use it to "verify" this file — it under-reports).
    Reused as-is for casper-derived initrds too (e.g. Mint's Ubiquity installer,
    which — unlike Ubuntu's Subiquity — doesn't consume a cloud-init/autoinstall
    volume at all, so its preseed has to go in via this same initrd-injection route
    with its own template rather than cidata.iso).

    NOTE: only preseed.cfg itself is guaranteed to survive — casper's live boot
    switch_roots into the ISO's squashfs, and casper's own scripts specifically
    know to carry that one file across; arbitrary extra files bundled the same way
    are NOT known to persist onto the live filesystem (tried this for an
    Ubiquity auto-focus workaround; the injected files never took effect).

    Returns the path to the combined initrd.

    Example::
        inject_preseed_into_initrd("/home/u/.qemu_vms/dev/linux-initrd", "/home/u/.qemu_vms/dev",
                                    "kali-preseed-extra.cfg.template")
        # -> "/home/u/.qemu_vms/dev/linux-initrd-preseeded"
    """
    preseed = _fill_template(template_name, locale=locale, keyboard=keyboard)
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, "preseed.cfg"), "w") as f:
            f.write(preseed)
        cpio_path = os.path.join(td, "override.cpio")
        subprocess.run(
            ["cpio", "-o", "-H", "newc", "-O", cpio_path],
            input=b"preseed.cfg\n", cwd=td, check=True, capture_output=True,
        )
        gz_path = os.path.join(td, "override.cpio.gz")
        with open(cpio_path, "rb") as raw, gzip.open(gz_path, "wb") as gz:
            gz.write(raw.read())

        combined_path = os.path.join(vm_dir, "linux-initrd-preseeded")
        with open(combined_path, "wb") as out:
            with open(base_initrd_path, "rb") as base:
                out.write(base.read())
            with open(gz_path, "rb") as override:
                out.write(override.read())
    return combined_path

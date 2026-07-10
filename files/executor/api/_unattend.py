"""
_unattend.py — build an ``autounattend.xml`` answer-file ISO for unattended
Windows installs.

The ISO (autounattend.xml at its root) is attached to a Windows VM as a second
CD; Windows Setup auto-detects it and installs fully hands-off: disk partition,
hardware-check bypass, OOBE skip, and a local admin account — no Microsoft
account, no clicking. Opt-in only (it wipes the target disk).
"""

import json
import os
import shutil
import subprocess
import tempfile

_HERE = os.path.dirname(__file__)
with open(os.path.join(_HERE, "config.json")) as _f:
    _UA = json.load(_f).get("unattended_windows", {})
_TEMPLATE = os.path.join(_HERE, "assets", "autounattend.xml.template")

# If OVMF misses the "Press any key to boot from CD" window it drops to the UEFI
# shell, which auto-runs startup.nsh from a mounted volume. This one finds the
# Windows installer's bootloader on whatever filesystem holds it and launches it,
# so the install boots without depending on catching that prompt. (The bootloader
# then shows its own "press any key" — launch_vm's keystroke burst clears that.)
def _startup_nsh() -> str:
    """UEFI shell startup.nsh: try each filesystem's Windows bootloader in turn.
    Explicit per-FS checks (the shell's for-loop %var interpolation is unreliable)."""
    lines = ["@echo -off", "echo qemu-api: auto-booting Windows installer..."]
    for n in range(10):
        p = "fs%d:\\efi\\boot\\bootx64.efi" % n
        lines += ["if exist %s then" % p, "    echo booting fs%d:" % n, "    %s" % p, "endif"]
    lines.append("echo No Windows bootloader found.")
    return "\n".join(lines) + "\n"


_STARTUP_NSH = _startup_nsh()


def iso_tool_available() -> str:
    """Return the first available ISO-building tool, or "" if none."""
    for t in ("genisoimage", "xorriso", "mkisofs"):
        if shutil.which(t):
            return t
    return ""


def fat_tool_available() -> bool:
    """True if mtools (mformat + mcopy) is installed to build the FAT boot media."""
    return bool(shutil.which("mformat") and shutil.which("mcopy"))


def _build_fat_media(vm_dir: str, xml_path: str) -> str:
    """Build ``<vm_dir>/autounattend.img`` — a 16 MB FAT16 image holding
    ``autounattend.xml`` + ``startup.nsh`` at its root, built with mtools (no root).

    Why FAT and not just the ISO: OVMF never mounts a plain ISO9660 as a
    filesystem (it shows only as a raw block device), so the UEFI shell can't
    auto-run a ``startup.nsh`` from the answer CD. A FAT volume OVMF *does*
    mount, so the shell auto-runs its ``startup.nsh`` — which launches the
    Windows installer — making the install boot hands-off. Windows Setup also
    scans this removable FAT volume for ``autounattend.xml``, so it doubles as
    answer-file delivery. Attached over USB (see qemu_arg_builder._usb).

    Returns the image path, or "" if mtools is missing (caller falls back to the
    ISO alone, which still applies the answer file but needs a manual boot nudge).
    """
    if not fat_tool_available():
        return ""
    img = os.path.join(vm_dir, "autounattend.img")
    subprocess.run(["dd", "if=/dev/zero", "of=%s" % img, "bs=1M", "count=16", "status=none"],
                   check=True, capture_output=True)
    subprocess.run(["mformat", "-i", img, "-v", "AUTOUNAT", "::"], check=True, capture_output=True)
    with tempfile.TemporaryDirectory() as td:
        nsh = os.path.join(td, "startup.nsh")
        with open(nsh, "w") as f:
            f.write(_STARTUP_NSH)
        subprocess.run(["mcopy", "-i", img, xml_path, "::/autounattend.xml"], check=True, capture_output=True)
        subprocess.run(["mcopy", "-i", img, nsh, "::/startup.nsh"], check=True, capture_output=True)
    return img


def generate_autounattend_iso(
    vm_dir: str,
    *,
    computer_name: str = "",
    username: str = "",
    password: str = "",
    locale: str = "",
    organization: str = None,
    product_key: str = "",
    autologon: bool = None,
) -> str:
    """Fill the answer-file template (config defaults + overrides) and build
    ``<vm_dir>/autounattend.iso``.

    Args:
        vm_dir:        VM directory to write autounattend.xml + autounattend.iso into.
        computer_name: Windows computer name (truncated to 15 chars).
        username/password/locale/organization/product_key/autologon:
                       Overrides; empty/None falls back to the ``unattended_windows``
                       config block.

    Returns:
        Path to the generated ISO.

    Raises:
        RuntimeError: If no ISO-building tool is installed.

    Example::

        generate_autounattend_iso("/home/u/.qemu_vms/win11", computer_name="win11",
                                  username="lab")
        # → "/home/u/.qemu_vms/win11/autounattend.iso"
    """
    tool = iso_tool_available()
    if not tool:
        raise RuntimeError(
            "no ISO tool (genisoimage/xorriso/mkisofs) — cannot build the unattended CD; "
            "install acpica-tools/genisoimage on the executor"
        )
    with open(_TEMPLATE) as f:
        xml = f.read()

    _autolog = autologon if autologon is not None else _UA.get("autologon", True)
    repl = {
        "__COMPUTER_NAME__": (computer_name or "WIN-VM")[:15],
        "__USERNAME__":      username or _UA.get("username", "user"),
        "__PASSWORD__":      password or _UA.get("password", "Passw0rd!"),
        "__LOCALE__":        locale or _UA.get("locale", "en-US"),
        "__ORGANIZATION__":  organization if organization is not None else _UA.get("organization", ""),
        "__PRODUCT_KEY__":   product_key or _UA.get("product_key", ""),
        "__AUTOLOGON__":     "true" if _autolog else "false",
    }
    for k, v in repl.items():
        xml = xml.replace(k, str(v))

    os.makedirs(vm_dir, exist_ok=True)
    xml_path = os.path.join(vm_dir, "autounattend.xml")
    with open(xml_path, "w") as f:
        f.write(xml)

    iso_path = os.path.join(vm_dir, "autounattend.iso")
    # Build from a temp dir so autounattend.xml lands at the ISO ROOT (where
    # Windows Setup looks for it).
    with tempfile.TemporaryDirectory() as td:
        shutil.copy(xml_path, os.path.join(td, "autounattend.xml"))
        with open(os.path.join(td, "startup.nsh"), "w") as f:
            f.write(_STARTUP_NSH)
        if tool == "xorriso":
            cmd = ["xorriso", "-as", "mkisofs", "-J", "-R", "-V", "AUTOUNATTEND", "-o", iso_path, td]
        else:
            cmd = [tool, "-J", "-R", "-V", "AUTOUNATTEND", "-o", iso_path, td]
        subprocess.run(cmd, check=True, capture_output=True)

    # Also build the FAT boot medium so the install boots hands-off (see
    # _build_fat_media). Best-effort: if mtools is absent the ISO alone still
    # applies the answer file, it just needs one manual boot nudge.
    _build_fat_media(vm_dir, xml_path)
    return iso_path

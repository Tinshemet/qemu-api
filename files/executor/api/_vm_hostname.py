"""
_vm_hostname.py — offline per-clone hostname randomization.

Golden-image cloning (create_vm(template=...)) backing-clones the template's
disk byte-for-byte, so every clone inherits the exact same OS-level hostname
(/etc/hostname on Linux, the ComputerName/Tcpip registry values on Windows)
unless something changes it — a real cross-clone fingerprint. This edits the
cloned disk directly, offline, no boot required, mirroring _vm_credentials.py.

Linux uses virt-customize's built-in --hostname (updates /etc/hostname and
the /etc/hosts 127.0.1.1 line). Windows has no equivalent tool — the computer
name lives in the SYSTEM registry hive, so this shells out to guestfish +
hivexregedit directly: extract SYSTEM, patch ComputerName/Tcpip Hostname
values, write the hive back. A freshly-installed Windows disk is typically
still marked hibernated (Fast Startup's hybrid shutdown), which makes
libguestfs mount NTFS read-only by default — the "remove_hiberfile" mount
option is required to get a writable mount, confirmed empirically.
"""

import re
import secrets
import shutil
import string
import subprocess
import tempfile
import os

_HOSTNAME_ALPHABET = string.ascii_lowercase + string.digits
_WINDOWS_ALPHABET = string.ascii_uppercase + string.digits
# Windows partition holding the OS — matches how the rest of this project
# identifies it (confirmed via virt-filesystems against the real template).
_WINDOWS_PARTITION = "/dev/sda3"
_WINDOWS_MOUNT_OPTS = "remove_hiberfile,rw"


def virt_customize_available() -> bool:
    """True if virt-customize (libguestfs-tools) is installed."""
    return bool(shutil.which("virt-customize"))


def windows_hostname_tools_available() -> bool:
    """True if guestfish and hivexregedit (libguestfs-tools + hivex) are installed."""
    return bool(shutil.which("guestfish")) and bool(shutil.which("hivexregedit"))


def _current_linux_hostname(disk_path: str) -> "str | None":
    if not shutil.which("virt-cat"):
        return None
    try:
        result = subprocess.run(
            ["virt-cat", "-a", disk_path, "/etc/hostname"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return name or None


def generate_linux_hostname(disk_path: str) -> str:
    """Generate a new, realistic hostname for a Linux disk image, based on the
    disk's current /etc/hostname (falls back to "linux-vm" if it can't be
    read) — keeps the distro-flavored prefix (e.g. "kali", "ubuntu") and
    replaces any generic "-vm"/"-template" suffix with a random one.

    Example::
        generate_linux_hostname("/home/u/.qemu_vms/test/disk0.qcow2")
        # -> "kali-9f2ab71"
    """
    base = _current_linux_hostname(disk_path) or "linux-vm"
    base = re.sub(r"[-_]?(vm|template)$", "", base, flags=re.I) or base
    suffix = "".join(secrets.choice(_HOSTNAME_ALPHABET) for _ in range(7))
    return f"{base}-{suffix}"


def generate_windows_hostname() -> str:
    """Generate a new Windows computer name matching the real
    auto-generated-name convention Windows itself uses (DESKTOP-XXXXXXX).

    Example::
        generate_windows_hostname()
        # -> "DESKTOP-7QP4K1X"
    """
    suffix = "".join(secrets.choice(_WINDOWS_ALPHABET) for _ in range(7))
    return f"DESKTOP-{suffix}"


def randomize_linux_hostname(disk_path: str, hostname: "str | None" = None) -> str:
    """Set a new hostname directly on a Linux disk image, offline, via
    virt-customize --hostname (updates /etc/hostname and /etc/hosts).

    The disk must not be attached to a running VM.

    Returns:
        The hostname that was set (auto-generated if not given).

    Raises:
        RuntimeError: virt-customize isn't installed, or the operation failed.

    Example::
        randomize_linux_hostname("/home/u/.qemu_vms/test/disk0.qcow2")
        # -> "kali-9f2ab71"
    """
    if not virt_customize_available():
        raise RuntimeError(
            "virt-customize not installed — install libguestfs-tools "
            "(see files/complementary/install_executor.sh)"
        )
    hostname = hostname or generate_linux_hostname(disk_path)
    result = subprocess.run(
        ["virt-customize", "-a", disk_path, "--hostname", hostname],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"virt-customize failed: {result.stderr.strip() or result.stdout.strip()}")
    return hostname


def _utf16_hex(value: str) -> str:
    data = value.encode("utf-16-le") + b"\x00\x00"
    return ",".join(f"{b:02x}" for b in data)


def randomize_windows_hostname(disk_path: str, hostname: "str | None" = None) -> str:
    """Set a new computer name directly on a Windows disk image, offline —
    extracts the SYSTEM registry hive via guestfish, patches ComputerName
    (ControlSet001\\Control\\ComputerName\\ComputerName and
    \\ActiveComputerName) and the Tcpip Hostname/"NV Hostname" values via
    hivexregedit, then writes the hive back. No boot required.

    The disk must not be attached to a running VM.

    Returns:
        The hostname that was set (auto-generated if not given).

    Raises:
        RuntimeError: guestfish/hivexregedit aren't installed, or any step
            of the extract/patch/write-back sequence fails.

    Example::
        randomize_windows_hostname("/home/u/.qemu_vms/test/disk0.qcow2")
        # -> "DESKTOP-7QP4K1X"
    """
    if not windows_hostname_tools_available():
        raise RuntimeError(
            "guestfish/hivexregedit not installed — install libguestfs-tools "
            "and hivex (see files/complementary/install_executor.sh)"
        )
    hostname = hostname or generate_windows_hostname()
    hex_value = _utf16_hex(hostname)

    with tempfile.TemporaryDirectory() as tmp:
        hive_path = os.path.join(tmp, "SYSTEM")
        reg_path = os.path.join(tmp, "hostname.reg")

        extract = subprocess.run(
            ["guestfish", "--ro", "-a", disk_path],
            input=(
                "run\n"
                f"mount-ro {_WINDOWS_PARTITION} /\n"
                f"copy-out /Windows/System32/config/SYSTEM {tmp}/\n"
            ),
            capture_output=True, text=True,
        )
        if extract.returncode != 0 or not os.path.exists(hive_path):
            raise RuntimeError(
                f"guestfish extract failed: {extract.stderr.strip() or extract.stdout.strip()}"
            )

        with open(reg_path, "w") as f:
            f.write(
                "Windows Registry Editor Version 5.00\n\n"
                "[\\ControlSet001\\Control\\ComputerName\\ComputerName]\n"
                f'"ComputerName"=hex(1):{hex_value}\n\n'
                "[\\ControlSet001\\Control\\ComputerName\\ActiveComputerName]\n"
                f'"ComputerName"=hex(1):{hex_value}\n\n'
                "[\\ControlSet001\\Services\\Tcpip\\Parameters]\n"
                f'"Hostname"=hex(1):{hex_value}\n'
                f'"NV Hostname"=hex(1):{hex_value}\n'
            )

        merge = subprocess.run(
            ["hivexregedit", "--merge", hive_path, reg_path],
            capture_output=True, text=True,
        )
        if merge.returncode != 0:
            raise RuntimeError(f"hivexregedit failed: {merge.stderr.strip() or merge.stdout.strip()}")

        # A normal Windows shutdown typically leaves the NTFS volume "unclean"
        # (dirty journal), which ntfs-3g refuses to mount read-write even with
        # remove_hiberfile. Running ntfsfix first clears that — even though
        # ntfsfix itself errors out on its own separate hibernation check, it
        # still resets the journal as a side effect, and the subsequent mount
        # with remove_hiberfile succeeds. Confirmed empirically; best-effort,
        # its exit code is intentionally not checked.
        subprocess.run(
            ["guestfish", "-a", disk_path],
            input="run\nntfsfix " + _WINDOWS_PARTITION + "\n",
            capture_output=True, text=True,
        )

        write_back = subprocess.run(
            ["guestfish", "-a", disk_path],
            input=(
                "run\n"
                f"mount-options {_WINDOWS_MOUNT_OPTS} {_WINDOWS_PARTITION} /\n"
                f"copy-in {hive_path} /Windows/System32/config/\n"
                "umount /\n"
            ),
            capture_output=True, text=True,
        )
        if write_back.returncode != 0:
            raise RuntimeError(
                f"guestfish write-back failed: {write_back.stderr.strip() or write_back.stdout.strip()}"
            )

    return hostname

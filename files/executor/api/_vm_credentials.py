"""
_vm_credentials.py — offline per-clone credential randomization.

Golden-image cloning (create_vm(template=...)) backing-clones the template's
disk byte-for-byte, so every clone inherits the exact same /etc/shadow content
unless something changes it. This edits the cloned disk's root password
directly via virt-customize (libguestfs) — no boot required, so it runs safely
during create_vm before the VM is ever started. Linux only: Windows credentials
live in the SAM registry hive, not /etc/shadow, and need a different tool.
"""

import re
import secrets
import shutil
import string
import subprocess

_PASSWORD_ALPHABET = string.ascii_letters + string.digits
# POSIX portable username rules — also closes off shell injection into the
# --run-command string below, which interpolates this value into a real shell
# command executed inside the guest via the libguestfs appliance.
_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def virt_customize_available() -> bool:
    """True if virt-customize (libguestfs-tools) is installed."""
    return bool(shutil.which("virt-customize"))


def linux_os_installed(disk_path: str) -> bool:
    """Check whether a disk image has a genuinely installed, bootable Linux OS
    — not just a partitioned-but-otherwise-empty disk — via an offline,
    read-only check for /etc/os-release (partitioning alone can't produce that
    file, unlike a raw disk-size check which can false-positive on a disk an
    unattended install has only partitioned so far, not finished installing).

    Returns:
        True if /etc/os-release exists and is non-empty. False on any error —
        a failed check just means "can't confirm yet", not "definitely not".

    Example::
        linux_os_installed("/home/u/.qemu_vms/dev/disk0.qcow2")
    """
    if not shutil.which("virt-cat"):
        return False
    try:
        result = subprocess.run(
            ["virt-cat", "-a", disk_path, "/etc/os-release"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _generate_password(length: int = 16) -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def randomize_root_password(disk_path: str) -> str:
    """Set a new random root password directly on a Linux disk image, offline.

    The disk must not be attached to a running VM — libguestfs mounts it itself
    via a small internal appliance.

    Returns:
        The generated password.

    Raises:
        RuntimeError: virt-customize isn't installed, or the operation failed
            (e.g. the disk has no recognizable Linux root filesystem).

    Example::
        randomize_root_password("/home/u/.qemu_vms/test/disk0.qcow2")
        # -> "kJ3xQ9mPz2Rn8wLt"
    """
    if not virt_customize_available():
        raise RuntimeError(
            "virt-customize not installed — install libguestfs-tools "
            "(see files/complementary/install_executor.sh)"
        )
    password = _generate_password()
    result = subprocess.run(
        ["virt-customize", "-a", disk_path, "--root-password", f"password:{password}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"virt-customize failed: {result.stderr.strip() or result.stdout.strip()}")
    return password


def rename_user(disk_path: str, new_username: str, old_username: "str | None" = None) -> str:
    """Rename the primary login account on a Linux disk image, offline — runs
    the guest's own usermod/groupmod against its own passwd/shadow/group files
    via the libguestfs appliance (same mechanism as randomize_root_password),
    correctly handling the private-group-per-user convention Debian/Kali/Ubuntu
    use. Moves the home directory too (usermod -m -d).

    old_username: auto-detected via find_primary_user() if not given.

    The disk must not be attached to a running VM.

    Returns:
        The new username (same as new_username, returned for convenience/logging).

    Raises:
        RuntimeError: virt-customize isn't installed, new_username isn't a
            valid POSIX username, old_username can't be found, or the rename
            itself fails.

    Example::
        rename_user("/home/u/.qemu_vms/test/disk0.qcow2", "alice")
        # -> "alice"
    """
    if not _USERNAME_RE.match(new_username):
        raise RuntimeError(
            f"'{new_username}' isn't a valid Linux username (lowercase letters/"
            "digits/underscore/hyphen, starting with a letter or underscore, "
            "max 32 chars)"
        )
    if not virt_customize_available():
        raise RuntimeError(
            "virt-customize not installed — install libguestfs-tools "
            "(see files/complementary/install_executor.sh)"
        )
    if old_username and not _USERNAME_RE.match(old_username):
        raise RuntimeError(f"'{old_username}' isn't a valid Linux username")
    if not old_username:
        old_username = find_primary_user(disk_path)
    if not old_username:
        raise RuntimeError("could not auto-detect a primary user account on this disk")
    # Validate the auto-detected name too — an explicit old_username is checked
    # above, but an auto-detected one was flowing unchecked into the usermod shell
    # command below. Same bar for both paths.
    if not _USERNAME_RE.match(old_username):
        raise RuntimeError(f"auto-detected username '{old_username}' isn't a valid Linux username")
    result = subprocess.run(
        ["virt-customize", "-a", disk_path,
         "--run-command", f"usermod -l {new_username} -m -d /home/{new_username} {old_username}",
         "--run-command", f"groupmod -n {new_username} {old_username} || true"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"virt-customize failed: {result.stderr.strip() or result.stdout.strip()}")
    return new_username


def find_primary_user(disk_path: str) -> "str | None":
    """Auto-detect the main human login account on a Linux disk image, offline
    — the first non-system account (UID >= 1000, excluding "nobody"), reading
    /etc/passwd directly via virt-cat.

    Returns:
        The username, or None if it can't be read or none qualifies.

    Example::
        find_primary_user("/home/u/.qemu_vms/test/disk0.qcow2")
        # -> "masteruser"
    """
    if not shutil.which("virt-cat"):
        return None
    try:
        result = subprocess.run(
            ["virt-cat", "-a", disk_path, "/etc/passwd"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        name, uid = parts[0], parts[2]
        if name == "nobody":
            continue
        try:
            if int(uid) >= 1000:
                return name
        except ValueError:
            continue
    return None


def randomize_user_password(disk_path: str, username: str) -> str:
    """Set a new random password for a named (non-root) user directly on a
    Linux disk image, offline. Same mechanism as randomize_root_password.

    The disk must not be attached to a running VM.

    Returns:
        The generated password.

    Raises:
        RuntimeError: virt-customize isn't installed, or the operation failed
            (e.g. the username doesn't exist on this disk).

    Example::
        randomize_user_password("/home/u/.qemu_vms/test/disk0.qcow2", "masteruser")
        # -> "Rz8tQmXpL3ohWK9v"
    """
    if not virt_customize_available():
        raise RuntimeError(
            "virt-customize not installed — install libguestfs-tools "
            "(see files/complementary/install_executor.sh)"
        )
    password = _generate_password()
    result = subprocess.run(
        ["virt-customize", "-a", disk_path, "--password", f"{username}:password:{password}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"virt-customize failed: {result.stderr.strip() or result.stdout.strip()}")
    return password

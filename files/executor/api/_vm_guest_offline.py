"""
_vm_guest_offline.py — offline stealth serial-agent provisioning.

generate_guest_agent_setup() (see _vm_guest.py) writes a script a human runs
*inside* a booted VM via curl-pipe-sudo — fine for interactive setup, useless
for headless/automated lab provisioning where nothing should have to boot
first. This installs the same daemon (as /usr/lib/sysdiag/sysdiagd, unit
sysdiag-agent.service — an innocuous name, not "gorgon"/"stealth", so casual
in-guest triage doesn't trip over it) + PSK + systemd unit directly onto a
stopped VM's disk via virt-customize, no boot required, mirroring
_vm_hostname.py / _vm_credentials.py's offline-edit pattern.

The whole payload is written by a script passed to virt-customize's --run,
which executes AS ROOT INSIDE THE GUEST — never by --copy-in'ing host files
into the guest. --copy-in preserves the host file's UID/GID verbatim, so
anything written from a host-side scratch file lands owned by whatever user
that UID happens to map to in the guest (silently breaking anything that
checks ownership, e.g. NetworkManager ignoring non-root keyfiles, and leaving
secrets group/other-readable if the host UID collides with a guest user).
--run sidesteps that class of bug entirely — the guest's own root writes the
files, so ownership is correct by construction.
"""

import os
import shutil
import subprocess
import tempfile

from ._vm_guest import _SERIAL_AGENT_DAEMON_PY

_UNIT = """[Unit]
Description=System diagnostics agent
After=multi-user.target

[Service]
ExecStart=/usr/bin/python3 /usr/lib/sysdiag/sysdiagd
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
"""


def virt_customize_available() -> bool:
    """True if virt-customize (libguestfs-tools) is installed."""
    return bool(shutil.which("virt-customize"))


def provision_guest_agent_offline(disk_path: str, psk: str) -> None:
    """Install the stealth serial-agent daemon + PSK + systemd unit on a
    stopped Linux VM's disk, offline — no boot required.

    The disk must not be attached to a running VM.

    Args:
        disk_path: Path to the VM's primary disk image.
        psk:       Pre-shared key the daemon authenticates host connections
                   against (same value as ``MachineConfig.guest_agent_psk``).

    Raises:
        RuntimeError: virt-customize isn't installed, or the operation failed.

    Example::
        provision_guest_agent_offline("/home/u/.qemu_vms/dev/disk0.qcow2", psk)
    """
    if not virt_customize_available():
        raise RuntimeError(
            "virt-customize not installed — install libguestfs-tools "
            "(see files/complementary/install_executor.sh)"
        )

    fd, script_path = tempfile.mkstemp(suffix=".sh")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("set -e\n")
            f.write("mkdir -p /usr/lib/sysdiag\n")
            f.write("cat > /usr/lib/sysdiag/sysdiagd <<'PYEOF'\n")
            f.write(_SERIAL_AGENT_DAEMON_PY)
            f.write("PYEOF\n")
            f.write("cat > /usr/lib/sysdiag/.sysdiag.key <<'PSKEOF'\n")
            f.write(psk + "\n")
            f.write("PSKEOF\n")
            f.write("chmod 755 /usr/lib/sysdiag/sysdiagd\n")
            f.write("chmod 600 /usr/lib/sysdiag/.sysdiag.key\n")
            f.write("cat > /etc/systemd/system/sysdiag-agent.service <<'UNITEOF'\n")
            f.write(_UNIT)
            f.write("UNITEOF\n")
            f.write("systemctl enable sysdiag-agent.service\n")

        result = subprocess.run(
            ["virt-customize", "-a", disk_path, "--run", script_path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"virt-customize failed: {result.stderr.strip() or result.stdout.strip()}"
            )
    finally:
        os.unlink(script_path)

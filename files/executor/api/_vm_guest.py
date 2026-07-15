"""
_vm_guest.py — Guest Agent mixin (in-VM command execution + liveness).

Provides _VmGuestMixin (run_guest_command, guest_ping, generate_guest_agent_setup,
provision_guest_agent_offline), composed into QemuManager. This is the host's
side of the guest agent: it talks to
an in-guest daemon over one of two transports, picked by VM type so callers stay
transport-agnostic:

  * non-stealth VMs: the real qemu-guest-agent daemon over the dedicated
    virtio-serial channel wired by qemu_arg_builder._qga (QGAClient).
  * stealth VMs: a second plain UART (COM2, no virtio tell) wired by
    qemu_arg_builder._serial_agent, speaking a PSK-authenticated JSON-line
    protocol to our own guest-side daemon (SerialAgentClient). See
    _STEALTH_AGENT_SETUP_SH below for the daemon's actual source.

Both clients expose the same run_exec()/exec_status()/ping() surface (see
qga_client.QGAClient / serial_agent_client.SerialAgentClient), so
run_guest_command/guest_ping drive either one through _get_guest_client()
without branching on which transport they got. The public return shape is
kept stable and per-VM so a future fleet layer can broadcast
run_guest_command across a label and aggregate {vm_name: result}.
"""

import json
import os
import secrets
import socket
import time
from typing import Any, Dict, List, Optional, Union

from .qemu_config import MachineConfig
from .qga_client import QGAClient
from .serial_agent_client import SerialAgentClient

_CFG      = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_TIMEOUTS = _CFG["timeouts"]

GuestClient = Union[QGAClient, SerialAgentClient]


# Picks the transport for a VM's guest channel — QGA over virtio-serial for
# non-stealth VMs, the PSK-authenticated serial-agent for stealth ones.
# Returns an unconnected, fully-parameterized client. In: MachineConfig → Out: GuestClient
def _get_guest_client(cfg: MachineConfig) -> GuestClient:
    """Return the right (not-yet-connected) guest client for this VM's config."""
    if cfg.stealth:
        return SerialAgentClient(cfg.get_serial_agent_socket(), cfg.guest_agent_psk)
    return QGAClient(cfg.get_qga_socket())


class _VmGuestMixin:
    """Mixin providing in-guest command execution and liveness over the guest agent."""

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def run_guest_command(
        self,
        name:    str,
        command: str,
        args:    Optional[List[str]] = None,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Run a command inside a VM via its guest agent and return the output.

        With ``args`` omitted, ``command`` is run through ``/bin/sh -c`` so the
        natural "run this shell line" usage works (pipes, redirection, arguments).
        With ``args`` given, ``command`` is the executable path and ``args`` its
        argv — no shell. Works for both non-stealth (QGA/virtio-serial) and
        stealth (PSK-authenticated serial-agent over COM2) VMs transparently.

        Args:
            name:    VM name (must be running, with ``guest_agent`` enabled).
            command: Shell line (default) or executable path (when ``args`` given).
            args:    Optional argv list; presence switches off the shell wrapper.
            timeout: Seconds to wait for completion (default from config).

        Returns:
            ``{"success": True, "exit_code": int, "stdout": str, "stderr": str}``
            on completion, or ``{"success": False, "error": str}`` with a distinct
            message for each failure mode (not running / agent disabled / channel
            missing / agent unreachable / timeout).

        Example::
            >>> mgr.run_guest_command("dev", "uptime")
            {"success": True, "exit_code": 0, "stdout": " 12:01:03 up 3 min...", "stderr": ""}
        """
        timeout = int(timeout) if timeout is not None else _TIMEOUTS.get("qga_command", 30)
        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

        if not self._is_running(name):
            return {"success": False, "error": f"VM '{name}' is not running."}
        if not cfg.guest_agent:
            return {"success": False,
                    "error": f"Guest agent not enabled for '{name}' "
                             "(create the VM with guest_agent=True)."}
        if cfg.stealth and not cfg.guest_agent_psk:
            return {"success": False,
                    "error": f"No serial-agent PSK for '{name}' — call "
                             "generate_guest_agent_setup to provision one "
                             "(clones don't inherit their source's PSK)."}

        sock_path = cfg.get_serial_agent_socket() if cfg.stealth else cfg.get_qga_socket()
        if not sock_path.startswith("tcp:") and not os.path.exists(sock_path):
            return {"success": False,
                    "error": f"Guest agent channel socket not found for '{name}' "
                             "(was the VM launched after enabling guest_agent?)."}

        client = _get_guest_client(cfg)
        try:
            client.connect()
        except (socket.timeout, ConnectionRefusedError, FileNotFoundError, OSError) as e:
            return {"success": False,
                    "error": f"Guest agent channel unreachable for '{name}': {e}"}
        except ConnectionError as e:
            daemon_hint = ("is sysdiag-agent.service running?" if cfg.stealth
                            else "is qemu-guest-agent running in the guest?")
            return {"success": False,
                    "error": f"Guest agent not responding for '{name}' ({daemon_hint}): {e}"}

        try:
            resp = client.run_exec(command, args, shell=args is None)
            if "error" in resp:
                return {"success": False, "error": resp["error"]}
            pid = resp["pid"]

            deadline = time.monotonic() + timeout
            while True:
                status = client.exec_status(pid)
                if "error" in status:
                    return {"success": False, "error": status["error"]}
                if status.get("exited"):
                    return {
                        "success":   True,
                        "exit_code": status.get("exit_code"),
                        "stdout":    status.get("stdout", ""),
                        "stderr":    status.get("stderr", ""),
                        "truncated": status.get("truncated", False),
                    }
                if time.monotonic() >= deadline:
                    return {"success": False,
                            "error": f"Command timed out after {timeout}s (still running in guest)."}
                time.sleep(0.1)
        except (socket.timeout, ConnectionError, OSError) as e:
            return {"success": False, "error": f"Guest agent channel error: {e}"}
        finally:
            client.close()

    # ------------------------------------------------------------------
    # Liveness
    # ------------------------------------------------------------------

    def guest_ping(self, name: str, timeout: int = 5) -> Dict[str, Any]:
        """Check whether the guest agent answers (guest OS alive, not just the process).

        The shared liveness primitive for HA and harness readiness checks. Works
        for both non-stealth and stealth VMs.

        Returns:
            ``{"success": True, "alive": bool}``; ``alive`` is False when the VM is
            stopped, the agent is disabled, the PSK is missing (stealth), or the
            daemon isn't responding.
        """
        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}
        if not self._is_running(name) or not cfg.guest_agent:
            return {"success": True, "alive": False}
        if cfg.stealth and not cfg.guest_agent_psk:
            return {"success": True, "alive": False}
        sock_path = cfg.get_serial_agent_socket() if cfg.stealth else cfg.get_qga_socket()
        if not sock_path.startswith("tcp:") and not os.path.exists(sock_path):
            return {"success": True, "alive": False}
        client = _get_guest_client(cfg)
        try:
            client.connect(timeout=timeout)
            return {"success": True, "alive": client.ping()}
        except (socket.timeout, ConnectionError, OSError):
            return {"success": True, "alive": False}
        finally:
            client.close()

    # ------------------------------------------------------------------
    # Guest-side setup
    # ------------------------------------------------------------------

    def generate_guest_agent_setup(self, name: str) -> Dict[str, Any]:
        """Write a guest-side script that installs + enables the guest agent (Linux).

        Non-stealth VMs get the real qemu-guest-agent (apt/dnf/pacman install).
        Stealth VMs get our own PSK-authenticated serial-agent daemon instead —
        a systemd service listening on COM2, no virtio-serial package involved.
        Either way, delivered by the same "human runs it once inside the VM"
        HTTP-serve pattern as generate_guest_setup.

        Returns:
            ``{"success": True, "path": str, "vm": str, "cmd_template": str}`` or
            ``{"success": False, "error": str}``.
        """
        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}
        if "windows" in (cfg.os_type or "").lower():
            return {"success": False,
                    "error": "Windows guest agent uses the QEMU Guest Agent MSI "
                             "(manual install) — not scripted yet."}

        vm_dir = os.path.expanduser(f"~/.qemu_vms/{name}")
        os.makedirs(vm_dir, exist_ok=True)

        if cfg.stealth:
            if not cfg.guest_agent:
                return {"success": False,
                        "error": f"Guest agent not enabled for '{name}' "
                                 "(create/update the VM with guest_agent=True)."}
            # A clone clears the PSK it would otherwise have shared with its
            # source (see clone_vm) — regenerate here rather than requiring a
            # separate "provision a PSK" step before setup can be served.
            if not cfg.guest_agent_psk:
                cfg.guest_agent_psk = secrets.token_hex(32)
                cfg.save()
            script_path = os.path.join(vm_dir, "guest_agent_setup.sh")
            script = _STEALTH_AGENT_SETUP_SH.replace("__PSK_PLACEHOLDER__", cfg.guest_agent_psk)
            with open(script_path, "w") as f:
                f.write(script)
        else:
            script_path = os.path.join(vm_dir, "guest_agent_setup.sh")
            with open(script_path, "w") as f:
                f.write(_GUEST_AGENT_SETUP_SH)

        os.chmod(script_path, 0o755)
        return {
            "success":      True,
            "path":         script_path,
            "vm":           name,
            "cmd_template": "curl http://10.0.2.2:{port}/guest_agent_setup.sh | sudo bash",
        }

    # ------------------------------------------------------------------
    # Offline provisioning (headless — no boot required)
    # ------------------------------------------------------------------

    def provision_guest_agent_offline(self, name: str) -> Dict[str, Any]:
        """Install the stealth serial-agent directly onto a stopped VM's disk.

        The offline counterpart to ``generate_guest_agent_setup`` for stealth
        VMs: no boot, no human running a curl-pipe-sudo script inside a live
        session — useful for automated/headless lab provisioning. Linux only
        (mirrors ``generate_guest_agent_setup``'s Windows restriction); the VM
        must be stopped, since this edits the disk image directly.

        Returns:
            ``{"success": True, "vm": str, "psk_provisioned": bool}`` or
            ``{"success": False, "error": str}``.

        Example::
            >>> mgr.provision_guest_agent_offline("stealth-dev")
            {"success": True, "vm": "stealth-dev", "psk_provisioned": True}
        """
        try:
            cfg = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}
        if not cfg.stealth:
            return {"success": False,
                    "error": f"'{name}' is not a stealth VM — offline provisioning "
                             "only applies to the serial-agent transport."}
        if "windows" in (cfg.os_type or "").lower():
            return {"success": False,
                    "error": "Windows guest agent uses the QEMU Guest Agent MSI "
                             "(manual install) — not scripted yet."}
        if self._is_running(name):
            return {"success": False,
                    "error": f"Stop '{name}' before offline-provisioning its disk."}
        if not cfg.disks:
            return {"success": False, "error": f"VM '{name}' has no disks."}

        psk_provisioned = False
        if not cfg.guest_agent_psk:
            cfg.guest_agent_psk = secrets.token_hex(32)
            psk_provisioned = True
        cfg.guest_agent = True

        from ._vm_guest_offline import provision_guest_agent_offline as _provision
        disk_path = os.path.expanduser(cfg.disks[0].path)
        try:
            _provision(disk_path, cfg.guest_agent_psk)
        except RuntimeError as e:
            return {"success": False, "error": str(e)}

        cfg.save()
        return {"success": True, "vm": name, "psk_provisioned": psk_provisioned}


_GUEST_AGENT_SETUP_SH = r"""#!/usr/bin/env bash
# Installs and enables qemu-guest-agent so the host can run commands in this VM.
set -e
echo "[guest-agent] installing qemu-guest-agent..."
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y qemu-guest-agent
elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y qemu-guest-agent
elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -Sy --noconfirm qemu-guest-agent
else
    echo "ERROR: no supported package manager found (apt/dnf/pacman)." >&2
    exit 1
fi
# The unit is qemu-guest-agent on most distros, qemu-ga on some.
sudo systemctl enable --now qemu-guest-agent 2>/dev/null \
  || sudo systemctl enable --now qemu-ga 2>/dev/null \
  || echo "[guest-agent] enable the qemu-guest-agent service manually if it did not start."
echo "[guest-agent] done."
"""


# The guest-side daemon for stealth VMs: speaks the PSK-authenticated JSON-line
# protocol (see serial_agent_client.SerialAgentClient) over /dev/ttyS1 — the
# second plain UART wired by qemu_arg_builder._serial_agent. No PSK is baked
# into this source; the stealth setup script below writes it to a separate
# 0600 file the daemon reads at startup. Single in-flight command at a time —
# a raw UART has no multiplexing, so a second exec while one is outstanding
# gets {"error": "busy"} rather than queueing or racing.
_SERIAL_AGENT_DAEMON_PY = r'''#!/usr/bin/env python3
"""sysdiagd — system diagnostics helper.

Provides a local command channel over the serial console for automated
health checks and diagnostics collection. Each session authenticates via a
pre-shared key (HMAC-SHA256 challenge-response) before any command runs.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import threading
import tty

SERIAL_DEV = "/dev/ttyS1"
PSK_PATH   = "/usr/lib/sysdiag/.sysdiag.key"

_lock = threading.Lock()
_current = None  # {"pid": int, "proc": Popen, "out": bytearray, "err": bytearray}


def _read_psk():
    with open(PSK_PATH) as f:
        return f.read().strip()


def _open_serial():
    fd = os.open(SERIAL_DEV, os.O_RDWR)
    tty.setraw(fd)
    return fd


def _send(fd, obj):
    os.write(fd, (json.dumps(obj) + "\n").encode())


def _recv_line(fd, buf):
    while b"\n" not in buf:
        chunk = os.read(fd, 4096)
        if not chunk:
            raise ConnectionError("serial channel closed")
        buf.extend(chunk)
    line, _, rest = bytes(buf).partition(b"\n")
    buf[:] = rest
    return json.loads(line.decode())


def _drain(stream, sink):
    for chunk in iter(lambda: stream.read(4096), b""):
        with _lock:
            sink.extend(chunk)


def _start_exec(command, args, shell):
    global _current
    with _lock:
        if _current is not None and _current["proc"].poll() is None:
            return {"error": "busy"}
    argv = ["/bin/sh", "-c", command] if shell else [command] + list(args or [])
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = bytearray(), bytearray()
    threading.Thread(target=_drain, args=(proc.stdout, out), daemon=True).start()
    threading.Thread(target=_drain, args=(proc.stderr, err), daemon=True).start()
    with _lock:
        _current = {"pid": proc.pid, "proc": proc, "out": out, "err": err}
    return {"pid": proc.pid}


def _exec_status(pid):
    with _lock:
        if _current is None or _current["pid"] != pid:
            return {"error": "unknown pid"}
        proc = _current["proc"]
        if proc.poll() is None:
            return {"exited": False}
        return {
            "exited":    True,
            "exit_code": proc.returncode,
            "stdout":    base64.b64encode(bytes(_current["out"])).decode(),
            "stderr":    base64.b64encode(bytes(_current["err"])).decode(),
        }


def _handle(obj):
    cmd = obj.get("cmd")
    if cmd == "ping":
        return {"pong": True}
    if cmd == "exec":
        return _start_exec(obj.get("command", ""), obj.get("args"), obj.get("shell", True))
    if cmd == "exec-status":
        return _exec_status(obj.get("pid"))
    return {"error": "unknown command: %r" % (cmd,)}


def main():
    psk = _read_psk()
    fd = _open_serial()
    buf = bytearray()
    authed = False
    while True:
        try:
            msg = _recv_line(fd, buf)
        except ConnectionError:
            authed = False
            continue
        if "hello" in msg:
            nonce = secrets.token_bytes(32)
            _send(fd, {"challenge": nonce.hex()})
            try:
                resp = _recv_line(fd, buf)
            except ConnectionError:
                authed = False
                continue
            expected = hmac.new(psk.encode(), nonce, hashlib.sha256).hexdigest()
            if hmac.compare_digest(resp.get("response", ""), expected):
                _send(fd, {"auth": "ok"})
                authed = True
            else:
                _send(fd, {"auth": "fail"})
                authed = False
            continue
        if not authed:
            _send(fd, {"error": "not authenticated"})
            continue
        _send(fd, _handle(msg))


if __name__ == "__main__":
    main()
'''


_STEALTH_AGENT_SETUP_SH = r"""#!/usr/bin/env bash
# Installs and enables the stealth serial-agent daemon (COM2 command channel).
# No package manager involved — the daemon is our own script, embedded below.
set -e
echo "[sysdiag] installing..."
sudo mkdir -p /usr/lib/sysdiag

sudo tee /usr/lib/sysdiag/sysdiagd > /dev/null <<'PYEOF'
""" + _SERIAL_AGENT_DAEMON_PY + r"""PYEOF
sudo chmod 755 /usr/lib/sysdiag/sysdiagd

sudo tee /usr/lib/sysdiag/.sysdiag.key > /dev/null <<'PSKEOF'
__PSK_PLACEHOLDER__
PSKEOF
sudo chmod 600 /usr/lib/sysdiag/.sysdiag.key

sudo tee /etc/systemd/system/sysdiag-agent.service > /dev/null <<'UNITEOF'
[Unit]
Description=System diagnostics agent
After=multi-user.target

[Service]
ExecStart=/usr/bin/python3 /usr/lib/sysdiag/sysdiagd
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
UNITEOF

sudo systemctl daemon-reload
sudo systemctl enable --now sysdiag-agent
echo "[sysdiag] done."
"""

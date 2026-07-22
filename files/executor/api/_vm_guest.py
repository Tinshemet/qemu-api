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

from ._vm_constants import VM_BASE_DIR
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
    # Grounding — read-only assertions
    # ------------------------------------------------------------------

    # A CLOSED set of read-only assertions. Each runs a guest command that EXITS 0
    # iff the assertion holds — truth comes from the exit_code, never from
    # run_guest_command's success flag (True on any completion). `file_contains`
    # also takes a `value` (the string to find). Every target/value is passed as
    # ARGV (never spliced into a shell string), so a probe can't become an action.
    _PROBE_ASSERTIONS = frozenset({
        "path_exists", "path_is_dir", "port_listening", "process_running",
        "user_exists", "service_active", "command_available", "file_contains",
        "is_writable", "is_executable", "is_setuid", "file_matches", "user_in_group",
        "host_reachable", "connection_to", "cron_has",
    })

    def guest_probe(self, name: str, assertion: str, target: str,
                    value: Optional[str] = None, timeout: Optional[int] = None) -> Dict[str, Any]:
        """Verify one read-only fact inside a VM — the grounding primitive.

        assertion ∈ {path_exists, path_is_dir, port_listening, process_running,
        user_exists, service_active, command_available, file_contains}. ``target`` is
        the path / port / process / user / service / command; ``file_contains`` also
        needs ``value`` (the string to find in ``target``). Truth comes from the
        command's EXIT CODE (0 = holds), NOT run_guest_command's success flag.

        Returns {"success": True, "assertion", "target", "holds": bool} on a completed
        probe (plus "value" for file_contains), or {"success": False, "error"} on a
        channel/agent failure or a bad request. Targets/values are argv, so a probe
        is always read-only.
        """
        t = str(target)
        if assertion == "path_exists":        exe, argv = "test", ["-e", t]
        elif assertion == "path_is_dir":      exe, argv = "test", ["-d", t]
        elif assertion == "process_running":  exe, argv = "pgrep", ["-x", "--", t]
        elif assertion == "user_exists":      exe, argv = "id", ["-u", "--", t]
        elif assertion == "service_active":   exe, argv = "systemctl", ["is-active", "--quiet", "--", t]
        elif assertion == "command_available":
            exe, argv = "sh", ["-c", 'command -v -- "$1" >/dev/null 2>&1', "_", t]
        elif assertion == "port_listening":
            exe, argv = "sh", ["-c", 'ss -Hltn 2>/dev/null | grep -qE "[:.]$1[[:space:]]"', "_", t]
        elif assertion == "is_writable":      exe, argv = "test", ["-w", t]
        elif assertion == "is_executable":    exe, argv = "test", ["-x", t]
        elif assertion == "is_setuid":        exe, argv = "test", ["-u", t]
        elif assertion == "file_contains":
            if not value:
                return {"success": False, "error": "file_contains needs a `value` (the string to find)"}
            exe, argv = "grep", ["-qF", "--", str(value), t]
        elif assertion == "file_matches":     # regex, vs file_contains' fixed string
            if not value:
                return {"success": False, "error": "file_matches needs a `value` (the regex)"}
            exe, argv = "grep", ["-qE", "-e", str(value), "--", t]
        elif assertion == "user_in_group":    # target = user, value = group (privesc recon)
            if not value:
                return {"success": False, "error": "user_in_group needs a `value` (the group)"}
            exe, argv = "sh", ["-c", 'id -nG -- "$1" 2>/dev/null | tr " " "\\n" | grep -qxF -- "$2"',
                               "_", t, str(value)]
        elif assertion == "host_reachable":   # target = host, value = port (network/pivot recon)
            if not value:
                return {"success": False, "error": "host_reachable needs a `value` (the port)"}
            exe, argv = "timeout", ["2", "bash", "-c", 'exec 3<>/dev/tcp/"$1"/"$2"', "_", t, str(value)]
        elif assertion == "connection_to":    # target = host: an ESTABLISHED connection to it
            exe, argv = "sh", ["-c", 'ss -tnH state established 2>/dev/null | grep -qF -- "$1"', "_", t]
        elif assertion == "cron_has":         # target = pattern in any crontab (persistence recon)
            exe, argv = "sh", ["-c",
                               'crontab -l 2>/dev/null | grep -qF -- "$1" || '
                               'grep -rqsF -- "$1" /etc/crontab /etc/cron.d /etc/cron.daily '
                               '/etc/cron.hourly /var/spool/cron 2>/dev/null', "_", t]
        else:
            return {"success": False,
                    "error": f"unknown assertion '{assertion}' — probe supports "
                             f"{sorted(self._PROBE_ASSERTIONS)}"}
        res = self.run_guest_command(name, exe, args=argv, timeout=timeout)   # args → no shell
        if not res.get("success"):
            return res                                # channel / agent failure — propagate as-is
        out = {"success": True, "assertion": assertion, "target": t,
               "holds": res.get("exit_code") == 0}    # ← truth from exit code, not `success`
        if value is not None:
            out["value"] = str(value)
        return out

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

        vm_dir = os.path.join(VM_BASE_DIR, name)
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


# ── Guest-side scripts (externalized to data files under guest_scripts/) ───────
# Byte-exact extractions of what used to be embedded here: the qemu-guest-agent
# installer (non-stealth), the stealth serial-agent daemon source (PSK-authenticated,
# no PSK baked in), and the stealth setup wrapper — a bash template that embeds the
# daemon at __STEALTH_DAEMON_SRC__ and carries __PSK_PLACEHOLDER__, which
# generate_guest_agent_setup replaces with the VM's PSK before writing it out.
_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "guest_scripts")


def _load_script(name: str) -> str:
    with open(os.path.join(_SCRIPTS_DIR, name)) as _f:
        return _f.read()


_GUEST_AGENT_SETUP_SH   = _load_script("guest_agent_setup.sh")
_SERIAL_AGENT_DAEMON_PY = _load_script("serial_agent_daemon.py")
_STEALTH_AGENT_SETUP_SH = _load_script("stealth_agent_setup.sh.tmpl").replace(
    "__STEALTH_DAEMON_SRC__", _SERIAL_AGENT_DAEMON_PY)

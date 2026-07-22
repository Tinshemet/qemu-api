"""
_vm_launch_support.py — VM Launch Support Mixin (swtpm / unattended / setup server).

Provides _VmLaunchSupportMixin which is composed into QemuManager. The auxiliary
processes launch_vm/stop_vm spin up around a VM — the swtpm TPM-2.0 daemon, the
unattended-install finalizer, and the HTTP server that serves the guest stealth
setup script — carved out of _vm_runtime.py to keep that focused on launch/stop.
"""
import http.server
import os
import socket
import subprocess
import threading
import time

import psutil

from typing import Optional

from .qemu_config import MachineConfig


class _VmLaunchSupportMixin:
    """Mixin providing swtpm lifecycle, unattended-install finalize, and setup server."""

    def _start_swtpm(self, config: MachineConfig) -> Optional[str]:
        """Start the swtpm socket daemon for TPM 2.0 emulation.

        Args:
            config: VM config (used to derive the vm_dir paths).

        Returns:
            ``None`` on success, or an error string on failure.
        """
        vm_dir   = config.get_vm_dir()
        tpm_dir  = os.path.join(vm_dir, "tpm")
        tpm_sock = os.path.join(vm_dir, "tpm.sock")
        tpm_pid  = os.path.join(vm_dir, "tpm.pid")
        os.makedirs(tpm_dir, exist_ok=True)
        # Reap any swtpm left over from a previous launch of THIS VM before
        # starting a fresh one — otherwise a relaunch orphans the old daemon.
        self._stop_swtpm(os.path.basename(vm_dir.rstrip("/")))
        if os.path.exists(tpm_sock):
            os.unlink(tpm_sock)
        try:
            subprocess.Popen([
                "swtpm", "socket",
                "--tpmstate", f"dir={tpm_dir}",
                "--ctrl",     f"type=unixio,path={tpm_sock}",
                "--tpm2",
                "--log",      "level=0",
                "--pid",      f"file={tpm_pid}",
                # --terminate: swtpm exits by itself once QEMU disconnects. This is
                # the reliable teardown: Ubuntu confines swtpm with an AppArmor
                # profile (enforce) that permits signals only from libvirt-* peers,
                # so os.kill from this framework is denied and _stop_swtpm's signal
                # can't reap it. Self-termination sidesteps that entirely; the
                # persisted tpmstate dir means a relaunch just starts a fresh swtpm.
                "--terminate",
                "--daemon",
            ])
        except FileNotFoundError:
            return "swtpm not found — install it with: sudo apt install swtpm"
        for _ in range(30):
            if os.path.exists(tpm_sock):
                return None
            time.sleep(0.1)
        return "swtpm started but socket never appeared — check swtpm installation"

    def _stop_swtpm(self, name: str) -> None:
        """Reap the swtpm daemon for the named VM.

        Kills by pidfile (SIGTERM, escalating to SIGKILL) AND sweeps any stray
        swtpm still bound to this VM's tpm dir. The sweep matters because swtpm
        is a detached daemon: a relaunch (Windows install reboots) or a crash
        that skips stop_vm overwrites tpm.pid and orphans the old process, so the
        pidfile alone isn't enough — orphans accumulate otherwise.
        """
        import signal as _signal
        vm_dir  = os.path.expanduser(f"~/.qemu_vms/{name}")
        tpm_pid = os.path.join(vm_dir, "tpm.pid")
        tpm_dir = os.path.join(vm_dir, "tpm")

        def _reap(pid: int) -> None:
            """Terminate a PID (SIGTERM, then SIGKILL after a grace period); no-op if gone."""
            try:
                os.kill(pid, _signal.SIGTERM)
            except (ProcessLookupError, OSError):
                return
            for _ in range(10):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, OSError):
                    return
            try:
                os.kill(pid, _signal.SIGKILL)
            except OSError:
                pass  # SIGKILL raced an already-gone pid — treat as reaped

        if os.path.exists(tpm_pid):
            try:
                with open(tpm_pid) as f:
                    _reap(int(f.read().strip()))
            except (ValueError, OSError):
                pass  # unreadable/garbage TPM pid file — skip reaping; the unlink below clears it
            try:
                os.unlink(tpm_pid)
            except OSError:
                pass  # stale TPM pid file already gone or unremovable — non-fatal for teardown

        # Sweep strays whose --tpmstate dir is this VM's tpm dir (orphans from
        # relaunches/crashes where the pidfile no longer points at them).
        try:
            for p in psutil.process_iter(["name", "cmdline"]):
                if p.info["name"] == "swtpm" and any(
                    tpm_dir in a for a in (p.info["cmdline"] or [])
                ):
                    _reap(p.pid)
        except Exception:
            pass  # psutil swtpm sweep is best-effort cleanup — never block VM stop on it

    def _maybe_finish_unattended_install(self, config: MachineConfig) -> bool:
        """Detect whether an install has actually finished, and if so switch the
        VM back to normal disk-boot: clear ``iso_path`` and (crucially) the
        direct-kernel-boot fields ``kernel_path``/``initrd_path``/``kernel_cmdline``.

        Without clearing the kernel-boot fields, EVERY future relaunch of a VM
        that used an unattended installer (direct -kernel/-initrd boot, since
        that's the only way to pass an autoinstall/preseed kernel parameter)
        re-triggers the installer from scratch instead of booting the disk's own
        bootloader — including on a guest-initiated reboot right after the
        install finishes, which re-runs the SAME destructive auto-partitioning
        preseed and wipes the install that had just completed. This happened for
        real once; this function exists specifically to stop it recurring.

        Two-stage check, cheapest first:
          1. ``qemu-img info`` disk size — skip the expensive stage below for a
             disk that's obviously still blank/early (no unattended install ever
             gets this far without writing at least ~2 GB).
          2. A real, read-only check for ``/etc/os-release`` via virt-cat. Disk
             size alone isn't enough — partitioning writes real data too, well
             before an install actually finishes, so a size-only check (the
             previous version of this function) can false-positive mid-install
             and detach things prematurely. os-release can only exist if the OS
             is genuinely installed.

        Persists the config change if the install is detected as finished.

        Args:
            config: MachineConfig that may have ``iso_path``/``kernel_path`` set.

        Returns:
            ``True`` if the installer fields were cleared, ``False`` otherwise.
        """
        if not (config.iso_path or config.kernel_path):
            return False
        disks = getattr(config, "disks", [])
        if not disks:
            return False
        disk_path = getattr(disks[0], "path", None)
        if not disk_path or not os.path.exists(disk_path):
            return False
        try:
            r = subprocess.run(
                ["qemu-img", "info", "--output=json", disk_path],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode != 0:
                return False
            import json as _json
            actual = _json.loads(r.stdout).get("actual-size", 0)
            if actual < 2 * 1024 ** 3:
                return False
        except Exception:
            return False

        from ._vm_credentials import linux_os_installed
        if not linux_os_installed(disk_path):
            return False

        config.iso_path       = None
        config.kernel_path     = None
        config.initrd_path     = None
        config.kernel_cmdline  = ""
        config.save()
        return True

    def _start_setup_server(self, name: str, script_path: str) -> int:
        """Start an HTTP server that serves the guest stealth setup script.

        Picks a random free port, binds a SimpleHTTPRequestHandler to it,
        and stores the server in ``self._setup_srvs[name]``.

        Args:
            name:        VM name (used as key in ``_setup_srvs``).
            script_path: Absolute path to the script file to serve.

        Returns:
            The port number the server is listening on.
        """
        script_dir = os.path.dirname(script_path)
        with socket.socket() as s:
            s.bind(("", 0))
            port = s.getsockname()[1]

        class _H(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=script_dir, **kw)

            def log_message(self, *_) -> None:
                """Silence the default HTTP request logging."""
                pass

        srv = http.server.HTTPServer(("0.0.0.0", port), _H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self._setup_srvs[name] = (srv, port)
        return port

"""
_vm_runtime.py — VM Runtime Mixin (launch / stop / TPM helpers).

Provides _VmRuntimeMixin which is composed into QemuManager.
"""
import http.server
import os
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Dict, Optional

import psutil

from ._vm_constants import _TIMEOUTS, VM_BASE_DIR
from .qemu_config import MachineConfig
from .qemu_arg_builder import QemuArgBuilder, VNC_PORT_START, next_free_port
from .qmp_client import QMPClient
from .vm_state import _PsutilProcWrapper


class _VmRuntimeMixin:
    """Mixin providing VM launch, stop, and related runtime helpers."""

    # ------------------------------------------------------------------
    # Launch
    # ------------------------------------------------------------------

    def launch_vm(
        self,
        name: str,
        display: Optional[str] = None,
        dry_run: bool = False,
        vnc_bind_local: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Build the QEMU command, start the process, record PID, apply CPU pinning.

        Args:
            name:           VM name (must exist in ``~/.qemu_vms/``).
            display:        Override the display backend for this launch only
                            (e.g. ``"vnc"``).
            dry_run:        If True, build and return the command without executing.
            vnc_bind_local: Override the persisted config's ``vnc_bind_local`` for
                            this launch only (the orchestrator sets this per-request
                            for remote/split-mode launches). ``None`` means "use
                            whatever's saved in the VM's own config".

        Returns:
            On success: ``{"success": True, "pid": int, "display": str, ...}``.
            VNC launch also includes ``"vnc_port"`` and optionally
            ``"vnc_password"`` (when ``vnc_bind_local=True``).
            On failure: ``{"success": False, "error": str}``.

        Example::
            >>> mgr.launch_vm("my-linux", dry_run=True)
            {"success": True, "dry_run": True, "command": "qemu-system-x86_64 ..."}
        """
        from .qemu_arg_builder import qemu_version_warn
        qemu_version_warn()
        try:
            config = MachineConfig.load(name)
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}

        if display:
            config.display = display
        if vnc_bind_local is not None:
            config.vnc_bind_local = vnc_bind_local

        if dry_run:
            cmd = QemuArgBuilder(config).build()
            return {"success": True, "dry_run": True,
                    "command": " ".join(cmd),
                    "message": "Dry run — command not executed."}

        if os.environ.get("QEMU_TEST_NO_LAUNCH"):
            cmd = QemuArgBuilder(config).build()
            return {"success": True, "pid": 0,
                    "display": config.display or "none",
                    "name": name,
                    "message": f"VM '{name}' launch skipped (QEMU_TEST_NO_LAUNCH)."}

        if self._is_running(name):
            result: Dict[str, Any] = {
                "success": False, "already_running": True,
                "error": f"VM '{name}' is already running.",
            }
            if config.display == "vnc":
                result["display"]  = "vnc"
                result["vnc_port"] = config.vnc_port or 5900
            return result

        if config.tpm and not config.machine_arch == "aarch64":
            tpm_err = self._start_swtpm(config)
            if tpm_err:
                return {"success": False, "error": tpm_err}

        auto_detached_iso = self._maybe_auto_detach_iso(config)

        if config.display == "vnc" and not config.vnc_port:
            config.vnc_port = next_free_port(VNC_PORT_START, self._used_ports("vnc"))

        cmd     = QemuArgBuilder(config).build()
        log_path = os.path.join(config.get_vm_dir(), "launch.log")
        try:
            _kwargs: Dict[str, Any] = {
                "stdout": open(log_path, "a"),  # noqa: WPS515
                "stderr": subprocess.STDOUT,
            }
            if sys.platform == "win32":
                _kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                _kwargs["start_new_session"] = True
            proc = subprocess.Popen(cmd, **_kwargs)
        except FileNotFoundError:
            return {"success": False,
                    "error": f"{config.qemu_binary} not found. Check QEMU installation."}
        except Exception as e:
            return {"success": False, "error": str(e)}

        # QEMU fails fast on bad args, missing firmware, or permission issues
        # (e.g. no /dev/kvm access) — give it a brief moment to crash before
        # reporting success, so callers aren't told a dead process is running.
        time.sleep(_TIMEOUTS.get("launch_liveness_check", 0.3))
        if proc.poll() is not None:
            try:
                with open(log_path) as _lf:
                    tail = "".join(_lf.readlines()[-5:]).strip()
            except OSError:
                tail = ""
            return {
                "success": False,
                "error": (
                    f"QEMU exited immediately (code {proc.returncode}). "
                    f"{tail or 'See launch.log for details.'}"
                ),
            }

        self._procs[name] = proc
        self._state.set_running(name, proc.pid)

        if config.cpu_pinning:
            time.sleep(_TIMEOUTS["cpu_pinning_delay"])
            self._apply_cpu_pinning(proc.pid, config.cpu_pinning)

        # Unattended Windows FIRST boot: auto-press Enter past the Windows installer's
        # "Press any key to boot from CD or DVD" prompt (else it times out to the UEFI
        # shell and setup never starts). Fires once (sentinel file), via the HMP
        # monitor socket so it doesn't contend with QMP, and never touches a booted
        # desktop (only the very first launch).
        if config.unattended:
            _sentinel = os.path.join(config.get_vm_dir(), ".unattended_booted")
            if not os.path.exists(_sentinel):
                try:
                    open(_sentinel, "w").close()
                except OSError:
                    pass  # best-effort boot sentinel — failure only risks re-triggering unattended, not correctness
                import socket as _sock
                import threading as _thr
                _mon  = config.get_monitor_socket()
                _secs = _TIMEOUTS.get("unattended_boot_keys", 25)

                # Send Enter over the HMP monitor for a fixed window. The window is
                # host-timing-tuned and does DOUBLE DUTY, which is why it can't be a
                # naive "clear the prompt and stop":
                #   1. clears the installer's "Press any key to boot from CD" prompt
                #      (cdboot, appears early), AND
                #   2. clicks "Next" past Win11 25H2 Setup's "Select language" screen,
                #      which the modern setup still shows even with an answer file.
                # It must also STOP before Enter lands on later controls (e.g. Setup's
                # "Support" link → a blocking "can't open link" dialog, or a Cancel
                # button). ~25s spans both (1) and (2) on this hardware. An adaptive
                # stop keyed on CD read (boot.wim) was tried and REVERTED: that signal
                # fires before the language screen, so it stopped too early and hung
                # at "Select language". A truly host-independent version needs screen-
                # state detection or an answer file that fully skips the language page.
                def _spam_boot_keys() -> None:
                    """Repeatedly send keypresses over the monitor socket to dismiss boot menus."""
                    conn = None
                    for _ in range(30):
                        try:
                            conn = _sock.socket(_sock.AF_UNIX)
                            conn.connect(_mon)
                            break
                        except Exception:
                            conn = None
                            time.sleep(0.5)
                    if conn is None:
                        return
                    try:
                        time.sleep(0.2)
                        try:
                            conn.recv(65536)
                        except Exception:
                            pass  # draining the monitor socket during keypress injection — recv errors are non-fatal
                        _end = time.time() + _secs
                        while time.time() < _end:
                            try:
                                conn.sendall(b"sendkey ret\n")
                                time.sleep(0.3)
                                try:
                                    conn.recv(65536)
                                except Exception:
                                    pass  # draining the monitor socket after 'sendkey ret' — recv errors are non-fatal
                            except Exception:
                                break
                    finally:
                        try:
                            conn.close()
                        except Exception:
                            pass  # closing the monitor socket in finally — a close error can't affect teardown

                _thr.Thread(target=_spam_boot_keys, daemon=True).start()

        result = {
            "success": True, "name": name, "pid": proc.pid,
            "display": config.display,
            "message": f"VM '{name}' launched (PID {proc.pid}).",
        }

        if config.display == "vnc":
            result["vnc_port"] = config.vnc_port

        if config.display == "vnc" and config.vnc_bind_local:
            import secrets
            from datetime import datetime, timedelta, timezone
            vnc_password = secrets.token_urlsafe(8)
            result["vnc_port"] = config.vnc_port
            _qmp = None
            for _attempt in range(15):
                try:
                    _qmp = QMPClient(config.get_qmp_socket())
                    _qmp.connect(timeout=2)
                    break
                except Exception:
                    _qmp = None
                    time.sleep(0.5)
            if _qmp is not None:
                try:
                    _qmp.execute("set_password", {
                        "protocol": "vnc", "password": vnc_password, "connected": "keep",
                    })
                    try:
                        _expire = int(
                            (datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp()
                        )
                        _qmp.execute("expire_password",
                                     {"protocol": "vnc", "time": str(_expire)})
                    except Exception:
                        pass  # VNC password expiry is best-effort — a failure just leaves the password non-expiring
                    result["vnc_password"] = vnc_password
                finally:
                    _qmp.close()
            else:
                result["vnc_password"] = None

        if auto_detached_iso:
            result["note"] = (
                "ISO detached automatically — disk has an installed OS. "
                "VM will boot from disk."
            )

        if config.stealth:
            sentinel = os.path.join(config.get_vm_dir(), ".stealth_done")
            if not os.path.exists(sentinel):
                gr = self.generate_guest_setup(name)
                if gr.get("success"):
                    port = self._start_setup_server(name, gr["path"])
                    result["setup_cmd"]     = gr["cmd_template"].format(port=port)
                    result["setup_pending"] = True

        if config.iso_path:
            relaunch_flag = os.path.join(config.get_vm_dir(), ".relaunch_after_install")
            open(relaunch_flag, "w").close()  # noqa: WPS515
            watcher_script = (
                f"import sys, os, time, psutil\n"
                f"sys.path.insert(0, {repr(os.path.dirname(os.path.dirname(__file__)))})\n"
                f"pid, flag, name = {proc.pid}, {repr(relaunch_flag)}, {repr(name)}\n"
                f"try:\n"
                f"    p = psutil.Process(pid)\n"
                f"    while True:\n"
                f"        try:\n"
                f"            if not p.is_running() or p.status() == 'zombie':\n"
                f"                break\n"
                f"        except Exception:\n"
                f"            break\n"
                f"        time.sleep(0.5)\n"
                f"except Exception: pass\n"
                f"if os.path.exists(flag):\n"
                f"    os.unlink(flag)\n"
                f"    time.sleep(2)\n"
                f"    from executor.api.qemu_manager import QemuManager\n"
                f"    mgr = QemuManager()\n"
                f"    mgr.launch_vm(name)\n"
                f"    try:\n"
                f"        from executor.api.qemu_config import MachineConfig\n"
                f"        cfg = MachineConfig.load(name)\n"
                f"        if cfg.stealth:\n"
                f"            stealth_done = os.path.join("
                f"os.path.expanduser('~/.qemu_vms'), name, '.stealth_done')\n"
                f"            while not os.path.exists(stealth_done):\n"
                f"                time.sleep(5)\n"
                f"    except Exception: pass\n"
            )
            subprocess.Popen(
                [sys.executable, "-c", watcher_script],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        return result

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------

    def stop_vm(self, name: str, force: bool = False) -> Dict[str, Any]:
        """Try graceful QMP system_powerdown, then terminate/kill the process.

        Args:
            name:  VM name.
            force: Skip graceful shutdown attempt; send SIGKILL immediately.

        Returns:
            ``{"success": True, "name": str, "message": str}`` or error dict.

        Example::
            >>> mgr.stop_vm("my-linux")
            {"success": True, "name": "my-linux", "message": "VM 'my-linux' stopped."}
        """
        import traceback as _tb
        import datetime as _dt
        _log_dir = os.path.expanduser(f"~/.qemu_vms/{name}")
        if os.path.isdir(_log_dir):
            with open(os.path.join(_log_dir, "stop_vm.log"), "a") as _lf:
                _lf.write(
                    f"\n--- stop_vm(name={name!r}, force={force}) "
                    f"at {_dt.datetime.now()} ---\n"
                )
                _tb.print_stack(file=_lf)

        # Cancel auto-relaunch before anything else — intentional stop must win.
        relaunch_flag = os.path.join(
            os.path.expanduser(f"~/.qemu_vms/{name}"), ".relaunch_after_install"
        )
        if os.path.exists(relaunch_flag):
            os.unlink(relaunch_flag)

        if not self._is_running(name):
            pid = self._find_qemu_pid(name)
            if not pid:
                self._state.set_stopped(name)
                return {"success": False, "error": f"VM '{name}' is not running."}
            try:
                p = psutil.Process(pid)
                self._procs[name] = _PsutilProcWrapper(p)
                self._state.set_running(name, pid)
            except psutil.NoSuchProcess:
                self._state.set_stopped(name)
                return {"success": False, "error": f"VM '{name}' is not running."}

        if not force:
            try:
                cfg = MachineConfig.load(name)
                qmp = QMPClient(cfg.get_qmp_socket())
                qmp.connect()
                qmp.execute("system_powerdown")
                qmp.close()
                for _ in range(_TIMEOUTS["stop_graceful"]):
                    if not self._is_running(name):
                        break
                    time.sleep(1)
            except Exception:
                pass  # graceful QMP shutdown failed — falls through to the forceful kill below

        proc = self._procs.get(name)
        if proc:
            try:
                if hasattr(proc, "terminate"):
                    proc.terminate()
                    time.sleep(2)
                    if hasattr(proc, "poll") and proc.poll() is None:
                        proc.kill()
                else:
                    proc.terminate()
            except Exception:
                pass  # terminate/kill raced an already-exited process — nothing left to do

        self._procs.pop(name, None)
        self._state.set_stopped(name)
        self._stop_swtpm(name)
        return {"success": True, "name": name, "message": f"VM '{name}' stopped."}

    def stop_all(self) -> Dict[str, Any]:
        """Stop every currently tracked running VM.

        Returns:
            Dict keyed by VM name, each value the result of ``stop_vm()``.

        Example::
            >>> mgr.stop_all()
            {"vm1": {"success": True, ...}, "vm2": {"success": True, ...}}
        """
        return {name: self.stop_vm(name) for name in list(self._procs.keys())}

    # ------------------------------------------------------------------
    # Private runtime helpers
    # ------------------------------------------------------------------

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

    def _maybe_auto_detach_iso(self, config: MachineConfig) -> bool:
        """Detach ISO if disk already has a substantial OS install (> 2 GB data).

        Persists the config change if detached.

        Args:
            config: MachineConfig that may have ``iso_path`` set.

        Returns:
            ``True`` if the ISO was detached, ``False`` otherwise.
        """
        if not config.iso_path:
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
        config.iso_path = None
        config.save()
        return True

    def _find_qemu_pid(self, name: str) -> Optional[int]:
        """Find an orphaned QEMU process by VM name (survives session restarts).

        Args:
            name: VM name as passed to QEMU via ``-name <name>,process=<name>``.

        Returns:
            PID as ``int``, or ``None`` if not found.
        """
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = " ".join(proc.info["cmdline"] or [])
                if f"process={name}" in cmdline and "qemu" in cmdline.lower():
                    return proc.info["pid"]
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass  # process vanished mid-scan (psutil race) — skip it and keep searching
        return None

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

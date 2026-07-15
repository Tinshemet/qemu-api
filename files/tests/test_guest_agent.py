#!/usr/bin/env python3
"""
test_guest_agent.py — Guest Agent unit tests (no real VM required).

Exercises the QGA protocol client and the run_guest_command / guest_ping manager
methods against a mock qemu-guest-agent socket server that speaks the real wire
protocol (guest-sync-delimited framing, guest-exec / guest-exec-status /
guest-ping). This fills a gap the QMP layer never had — the guest-agent logic is
fully testable without launching QEMU.

Run:  PYTHONPATH=files python3 files/tests/test_guest_agent.py
"""
import base64
import json
import os
import shutil
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from executor.api.qga_client import QGAClient
from executor.api.qemu_config import MachineConfig
from executor.api.qemu_manager import QemuManager


# ── Mock qemu-guest-agent server ────────────────────────────────────────────────
class MockQGA:
    """A minimal AF_UNIX server speaking the qemu-guest-agent protocol.

    Configurable to simulate the failure modes the client must handle:
      answer_sync=False  → never completes the guest-sync-delimited handshake
      never_exits=True   → guest-exec-status always reports exited=False
      exit_code / out / err → what a finished guest-exec reports
    """
    def __init__(self, path, *, answer_sync=True, never_exits=False,
                 exit_code=0, out="", err=""):
        self.path        = path
        self.answer_sync = answer_sync
        self.never_exits = never_exits
        self.exit_code   = exit_code
        self.out         = out
        self.err         = err
        self._srv        = None
        self._thread     = None

    def start(self):
        if os.path.exists(self.path):
            os.unlink(self.path)
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.path)
        self._srv.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self):
        try:
            self._srv.close()
        except Exception:
            pass
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _serve(self):
        while True:
            try:
                conn, _ = self._srv.accept()
            except Exception:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        conn.settimeout(5)
        buf = b""
        # First message is the 0xFF-prefixed guest-sync-delimited command.
        try:
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    conn.close(); return
                buf += chunk
        except Exception:
            conn.close(); return
        line, _, buf = buf.partition(b"\n")
        if not self.answer_sync:
            # Simulate a dead agent: accept the socket but never reply.
            time.sleep(30); conn.close(); return
        sync = json.loads(line.lstrip(b"\xff").decode())
        sync_id = sync["arguments"]["id"]
        conn.sendall(b"\xff" + json.dumps({"return": sync_id}).encode() + b"\n")
        # Then serve newline-delimited JSON commands.
        while True:
            try:
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        conn.close(); return
                    buf += chunk
            except Exception:
                conn.close(); return
            line, _, buf = buf.partition(b"\n")
            if not line.strip():
                continue
            msg = json.loads(line.decode())
            conn.sendall((json.dumps(self._reply(msg)) + "\n").encode())

    def _reply(self, msg):
        cmd = msg.get("execute")
        if cmd == "guest-ping":
            return {"return": {}}
        if cmd == "guest-exec":
            return {"return": {"pid": 4242}}
        if cmd == "guest-exec-status":
            if self.never_exits:
                return {"return": {"exited": False}}
            return {"return": {
                "exited":   True,
                "exitcode": self.exit_code,
                "out-data": base64.b64encode(self.out.encode()).decode(),
                "err-data": base64.b64encode(self.err.encode()).decode(),
            }}
        return {"error": {"class": "GenericError", "desc": f"unknown command {cmd}"}}


# ── Test harness ────────────────────────────────────────────────────────────────
_PASS = 0
_FAIL = 0

def check(label, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  \033[32mok\033[0m   {label}")
    else:
        _FAIL += 1
        print(f"  \033[31mFAIL\033[0m {label}")


def make_test_vm(name, *, guest_agent=True, stealth=False):
    """Create a saved VM config; return it. Caller cleans up the dir."""
    cfg = MachineConfig(name=name, os_type="linux",
                        guest_agent=guest_agent, stealth=stealth)
    cfg.save()
    return cfg


def cleanup_vm(name):
    d = os.path.expanduser(f"~/.qemu_vms/{name}")
    if os.path.isdir(d):
        shutil.rmtree(d)


def main():
    print("QGA client — protocol against mock server")
    sockp = "/tmp/_ga_client_test.sock"
    srv = MockQGA(sockp, out="hello\n", exit_code=0)
    srv.start()
    try:
        c = QGAClient(sockp)
        c.connect()
        check("connect completes guest-sync-delimited handshake", True)
        check("guest-ping returns {}", "return" in c.execute("guest-ping"))
        r = c.execute("guest-exec", {"path": "/bin/sh", "arg": ["-c", "echo hi"], "capture-output": True})
        check("guest-exec returns a pid", r.get("return", {}).get("pid") == 4242)
        c.close()
    finally:
        srv.stop()

    # sync id mismatch / dead agent
    srv2 = MockQGA(sockp, answer_sync=False)
    srv2.start()
    try:
        c = QGAClient(sockp)
        raised = False
        try:
            c.connect(timeout=1)
        except (ConnectionError, socket.timeout):
            raised = True
        check("connect raises when agent never answers sync", raised)
        c.close()
    finally:
        srv2.stop()

    print("\nrun_guest_command — manager method")
    mgr = QemuManager()
    orig_running = mgr._is_running

    # happy path
    name = "_ga_test"
    cleanup_vm(name)
    cfg = make_test_vm(name)
    srv = MockQGA(cfg.get_qga_socket(), out="up 3 min\n", err="", exit_code=0)
    srv.start()
    mgr._is_running = lambda n: True
    try:
        res = mgr.run_guest_command(name, "uptime")
        check("success True", res.get("success") is True)
        check("exit_code 0", res.get("exit_code") == 0)
        check("stdout decoded", res.get("stdout") == "up 3 min\n")
    finally:
        srv.stop()

    # non-zero exit + stderr
    srv = MockQGA(cfg.get_qga_socket(), out="", err="nope\n", exit_code=2)
    srv.start()
    try:
        res = mgr.run_guest_command(name, "false")
        check("non-zero exit_code surfaced", res.get("exit_code") == 2)
        check("stderr decoded", res.get("stderr") == "nope\n")
    finally:
        srv.stop()

    # timeout (agent never reports exited)
    srv = MockQGA(cfg.get_qga_socket(), never_exits=True)
    srv.start()
    try:
        res = mgr.run_guest_command(name, "sleep 999", timeout=1)
        check("timeout returns error", res.get("success") is False and "timed out" in res.get("error", ""))
    finally:
        srv.stop()

    # agent unreachable (no server listening; socket file absent)
    if os.path.exists(cfg.get_qga_socket()):
        os.unlink(cfg.get_qga_socket())
    res = mgr.run_guest_command(name, "uptime")
    check("missing channel socket → distinct error", res.get("success") is False and "socket not found" in res.get("error", ""))

    # guest_ping true / false
    srv = MockQGA(cfg.get_qga_socket())
    srv.start()
    try:
        check("guest_ping alive True", mgr.guest_ping(name).get("alive") is True)
    finally:
        srv.stop()
    check("guest_ping alive False when channel down", mgr.guest_ping(name).get("alive") is False)

    # VM not running
    mgr._is_running = lambda n: False
    check("not-running → error", mgr.run_guest_command(name, "uptime").get("error", "").endswith("is not running."))
    mgr._is_running = lambda n: True

    # guest_agent disabled
    cleanup_vm(name); make_test_vm(name, guest_agent=False)
    check("agent-disabled → error", "not enabled" in mgr.run_guest_command(name, "uptime").get("error", ""))

    # stealth with no PSK provisioned yet (make_test_vm builds a config
    # directly, bypassing create_vm's keygen) → distinct error, not a channel
    # timeout. Full stealth serial-agent coverage lives in test_serial_agent.py.
    cleanup_vm(name); make_test_vm(name, guest_agent=True, stealth=True)
    check("stealth, no PSK → distinct error", "PSK" in mgr.run_guest_command(name, "uptime").get("error", ""))

    mgr._is_running = orig_running
    cleanup_vm(name)

    print("\ngenerate_guest_agent_setup")
    cleanup_vm(name); make_test_vm(name)
    r = mgr.generate_guest_agent_setup(name)
    check("writes script + cmd_template", r.get("success") and os.path.exists(r["path"]) and "{port}" in r["cmd_template"])
    cleanup_vm(name)

    print(f"\n{'='*48}\n  {_PASS} passed, {_FAIL} failed\n{'='*48}")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

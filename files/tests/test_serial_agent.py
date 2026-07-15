#!/usr/bin/env python3
"""
test_serial_agent.py — Stealth serial-agent unit tests (no real VM required).

Exercises the SerialAgentClient protocol client and the run_guest_command /
guest_ping manager methods, for stealth VMs, against a mock guest-side daemon
socket server that speaks the real wire protocol (PSK challenge-response
handshake, then exec / exec-status / ping). Sibling of test_guest_agent.py —
same mock-server-over-AF_UNIX approach, standing in for the Unix-socket end of
the chardev (the mock never needs to touch a real tty; the host only ever
talks to that socket end regardless of what's on the guest side of the wire).

Run:  PYTHONPATH=files python3 files/tests/test_serial_agent.py
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from executor.api.serial_agent_client import SerialAgentClient
from executor.api.qemu_config import MachineConfig
from executor.api.qemu_manager import QemuManager

_TEST_PSK = secrets.token_hex(32)


# ── Mock guest-side serial-agent daemon ─────────────────────────────────────────
class MockSerialAgent:
    """A minimal AF_UNIX server speaking the stealth serial-agent protocol.

    Configurable to simulate the failure modes the client must handle:
      psk                 → the PSK this mock verifies incoming responses against
      answer_auth=False   → never replies to the handshake at all
      never_exits=True    → exec-status always reports exited=False
      exit_code/out/err   → what a finished exec reports
    """
    def __init__(self, path, *, psk=_TEST_PSK, answer_auth=True, never_exits=False,
                 exit_code=0, out="", err=""):
        self.path         = path
        self.psk          = psk
        self.answer_auth  = answer_auth
        self.never_exits  = never_exits
        self.exit_code    = exit_code
        self.out          = out
        self.err          = err
        self._srv         = None
        self._thread      = None

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

    def _recv_line(self, conn, buf):
        while b"\n" not in buf[0]:
            chunk = conn.recv(65536)
            if not chunk:
                raise ConnectionError
            buf[0] += chunk
        line, _, buf[0] = buf[0].partition(b"\n")
        return json.loads(line.decode())

    def _handle(self, conn):
        conn.settimeout(5)
        buf = [b""]
        try:
            hello = self._recv_line(conn, buf)
        except Exception:
            conn.close(); return
        if "hello" not in hello:
            conn.close(); return
        if not self.answer_auth:
            # Simulate a dead/hung daemon: accept the connection, never reply.
            time.sleep(30); conn.close(); return
        nonce = secrets.token_bytes(32)
        conn.sendall((json.dumps({"challenge": nonce.hex()}) + "\n").encode())
        try:
            resp = self._recv_line(conn, buf)
        except Exception:
            conn.close(); return
        expected = hmac.new(self.psk.encode(), nonce, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(resp.get("response", ""), expected):
            conn.sendall((json.dumps({"auth": "fail"}) + "\n").encode())
            conn.close(); return
        conn.sendall((json.dumps({"auth": "ok"}) + "\n").encode())

        while True:
            try:
                msg = self._recv_line(conn, buf)
            except Exception:
                conn.close(); return
            conn.sendall((json.dumps(self._reply(msg)) + "\n").encode())

    def _reply(self, msg):
        cmd = msg.get("cmd")
        if cmd == "ping":
            return {"pong": True}
        if cmd == "exec":
            return {"pid": 4242}
        if cmd == "exec-status":
            if self.never_exits:
                return {"exited": False}
            return {
                "exited":    True,
                "exit_code": self.exit_code,
                "stdout":    base64.b64encode(self.out.encode()).decode(),
                "stderr":    base64.b64encode(self.err.encode()).decode(),
            }
        return {"error": f"unknown command {cmd}"}


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


def make_test_vm(name, *, guest_agent=True, psk=_TEST_PSK):
    """Create a saved stealth VM config; return it. Caller cleans up the dir."""
    cfg = MachineConfig(name=name, os_type="linux", stealth=True,
                        guest_agent=guest_agent, guest_agent_psk=psk)
    cfg.save()
    return cfg


def cleanup_vm(name):
    d = os.path.expanduser(f"~/.qemu_vms/{name}")
    if os.path.isdir(d):
        shutil.rmtree(d)


def main():
    print("SerialAgentClient — protocol against mock server")
    sockp = "/tmp/_serial_agent_client_test.sock"
    srv = MockSerialAgent(sockp, out="hello\n", exit_code=0)
    srv.start()
    try:
        c = SerialAgentClient(sockp, _TEST_PSK)
        c.connect()
        check("connect completes PSK handshake", True)
        check("ping returns True", c.ping() is True)
        r = c.run_exec("echo hi")
        check("run_exec returns a pid", r.get("pid") == 4242)
        c.close()
    finally:
        srv.stop()

    # wrong PSK — handshake must fail cleanly, not hang or silently succeed
    srv2 = MockSerialAgent(sockp, psk=_TEST_PSK)
    srv2.start()
    try:
        c = SerialAgentClient(sockp, "the-wrong-psk-entirely")
        raised = False
        try:
            c.connect(timeout=2)
        except ConnectionError:
            raised = True
        check("connect raises on wrong PSK", raised)
        c.close()
    finally:
        srv2.stop()

    # daemon never answers the handshake at all (hung/dead)
    srv3 = MockSerialAgent(sockp, answer_auth=False)
    srv3.start()
    try:
        c = SerialAgentClient(sockp, _TEST_PSK)
        raised = False
        try:
            c.connect(timeout=1)
        except (socket.timeout, ConnectionError):
            raised = True
        check("connect raises when daemon never answers handshake", raised)
        c.close()
    finally:
        srv3.stop()

    print("\nrun_guest_command — manager method (stealth transport)")
    mgr = QemuManager()
    orig_running = mgr._is_running

    name = "_sa_test"
    cleanup_vm(name)
    cfg = make_test_vm(name)
    srv = MockSerialAgent(cfg.get_serial_agent_socket(), out="up 3 min\n", err="", exit_code=0)
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
    srv = MockSerialAgent(cfg.get_serial_agent_socket(), out="", err="nope\n", exit_code=2)
    srv.start()
    try:
        res = mgr.run_guest_command(name, "false")
        check("non-zero exit_code surfaced", res.get("exit_code") == 2)
        check("stderr decoded", res.get("stderr") == "nope\n")
    finally:
        srv.stop()

    # timeout (daemon never reports exited)
    srv = MockSerialAgent(cfg.get_serial_agent_socket(), never_exits=True)
    srv.start()
    try:
        res = mgr.run_guest_command(name, "sleep 999", timeout=1)
        check("timeout returns error", res.get("success") is False and "timed out" in res.get("error", ""))
    finally:
        srv.stop()

    # agent unreachable (no server listening; socket file absent)
    if os.path.exists(cfg.get_serial_agent_socket()):
        os.unlink(cfg.get_serial_agent_socket())
    res = mgr.run_guest_command(name, "uptime")
    check("missing channel socket → distinct error", res.get("success") is False and "socket not found" in res.get("error", ""))

    # guest_ping true / false
    srv = MockSerialAgent(cfg.get_serial_agent_socket())
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

    # PSK missing (e.g. a clone that hasn't re-run generate_guest_agent_setup)
    cleanup_vm(name); make_test_vm(name, psk="")
    res = mgr.run_guest_command(name, "uptime")
    check("missing PSK → distinct error", res.get("success") is False and "PSK" in res.get("error", ""))
    check("guest_ping alive False when PSK missing", mgr.guest_ping(name).get("alive") is False)

    mgr._is_running = orig_running
    cleanup_vm(name)

    print("\ngenerate_guest_agent_setup — stealth branch")
    cleanup_vm(name); make_test_vm(name, psk="")
    r = mgr.generate_guest_agent_setup(name)
    check("writes script + cmd_template", r.get("success") and os.path.exists(r["path"]) and "{port}" in r["cmd_template"])
    check("lazily provisions a PSK when missing", MachineConfig.load(name).guest_agent_psk != "")
    script = open(r["path"]).read()
    check("embeds the PSK, not the placeholder", "__PSK_PLACEHOLDER__" not in script
          and MachineConfig.load(name).guest_agent_psk in script)
    check("script references the serial-agent systemd unit", "sysdiag-agent" in script)
    cleanup_vm(name)

    print(f"\n{'='*48}\n  {_PASS} passed, {_FAIL} failed\n{'='*48}")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

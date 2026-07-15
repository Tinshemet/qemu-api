"""
serial_agent_client.py — Stealth Serial-Agent Communication Layer

Low-level socket client for the PSK-authenticated JSON-line protocol spoken
over a stealth VM's second plain UART (COM2; see
qemu_arg_builder._serial_agent). Sibling of qga_client.QGAClient — same
newline-delimited JSON wire style — but:

  * No virtio-serial 0xFF sync handshake (there's no shared bus to flush on a
    private point-to-point UART). Instead connect() runs a PSK challenge-
    response handshake so a compromised/malicious guest can't spoof replies
    back at the host. This is deliberately ONE-WAY: the host proves it knows
    the PSK, the guest doesn't prove anything back — the host already fully
    controls the hypervisor, so guest-authenticating-host would be theater.
    The PSK itself is never sent over the wire in either direction, only an
    HMAC of a nonce the guest generates.
  * Command shape (``{"cmd": ...}``) differs from QGA's (``{"execute": ...}``)
    since the guest-side daemon is our own code, not the real qemu-guest-agent
    — but exposes the SAME run_exec()/exec_status()/ping() surface as
    QGAClient, so _vm_guest.py can drive either transport identically without
    branching on which one it holds.
  * A raw UART carries one in-flight command at a time — no multiplexing.
    The guest daemon rejects a second exec while one is outstanding with
    {"error": "busy"}; this client doesn't hide that, it's surfaced like any
    other {"error"} response.

This module imports nothing from the manager/tool layers — the dependency edge
points inward only, exactly like qga_client / qmp_client.
"""

import base64
import hashlib
import hmac
import json
import os
import socket
from typing import Optional

_CFG      = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_BUFFERS  = _CFG["buffers"]
_TIMEOUTS = _CFG["timeouts"]


# Decodes a base64 exec-status output field to text; empty string when absent.
# In: Optional[str] → Out: str
def _b64(data: Optional[str]) -> str:
    """Base64-decode a stdout/stderr field to a UTF-8 string."""
    if not data:
        return ""
    try:
        return base64.b64decode(data).decode("utf-8", errors="replace")
    except Exception:
        return ""


class SerialAgentClient:
    def __init__(self, socket_path: str, psk: str):
        self.socket_path = socket_path
        self.psk = psk
        self.sock: Optional[socket.socket] = None

    # Opens the serial-agent socket and completes the PSK handshake.
    # Supports Unix domain sockets (Linux/macOS) and TCP via "tcp:host:port" (Windows).
    # In: int timeout → Out: nothing
    def connect(self, timeout: int = _TIMEOUTS.get("serial_agent_connect", 5)) -> None:
        """Open the serial-agent socket and complete the PSK challenge-response handshake.

        Raises:
            OSError/socket.timeout: the socket can't be opened (VM not running,
                channel not wired) — caller distinguishes these.
            ConnectionError: the daemon answered but the PSK didn't match, or
                closed the channel before completing the handshake (no
                sysdiag-agent.service running in the guest).
        """
        if self.socket_path.startswith("tcp:"):
            host, port = self.socket_path[4:].rsplit(":", 1)
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect((host, int(port)))
        else:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect(self.socket_path)
        self._handshake()

    # Performs the PSK challenge-response to prove the host knows the secret,
    # without ever sending the secret itself. In: nothing → Out: nothing (raises on failure)
    def _handshake(self) -> None:
        """Prove knowledge of the PSK via an HMAC-SHA256 challenge-response."""
        self._send({"hello": 1})
        msg = self._recv()
        nonce_hex = msg.get("challenge")
        if not nonce_hex:
            raise ConnectionError("serial agent did not send a challenge — unexpected greeting")
        digest = hmac.new(self.psk.encode(), bytes.fromhex(nonce_hex), hashlib.sha256).hexdigest()
        self._send({"response": digest})
        reply = self._recv()
        if reply.get("auth") != "ok":
            raise ConnectionError("serial agent rejected the PSK (auth failed)")

    # Serializes a dict to JSON and sends it over the socket.
    # In: dict → Out: nothing
    def _send(self, data: dict) -> None:
        """Send one JSON line over the socket."""
        self.sock.sendall((json.dumps(data) + "\n").encode())

    # Reads bytes until a complete newline-delimited JSON object is assembled.
    # In: nothing → Out: dict
    def _recv(self) -> dict:
        """Read and parse one JSON line response."""
        buf = b""
        while True:
            if b"\n" not in buf:
                chunk = self.sock.recv(_BUFFERS.get("serial_agent", 65536))
                if not chunk:
                    raise ConnectionError(
                        "serial agent socket closed by peer while waiting for response"
                    )
                buf += chunk
                continue
            line, _, buf = buf.partition(b"\n")
            if not line.strip():
                continue
            return json.loads(line.decode())

    # ------------------------------------------------------------------
    # Transport-agnostic surface — mirrors QGAClient's so _vm_guest.py can
    # drive either transport through the same three calls.
    # ------------------------------------------------------------------

    # Starts a command in the guest; normalized {"pid": int} or {"error": str}.
    # In: str command, Optional[List[str]] args, bool shell → Out: dict
    def run_exec(self, command: str, args: Optional[list] = None, shell: bool = True) -> dict:
        """Start a command via the guest daemon; normalized {"pid"} / {"error"} result."""
        if shell:
            payload = {"cmd": "exec", "shell": True, "command": command}
        else:
            payload = {"cmd": "exec", "shell": False, "command": command, "args": list(args or [])}
        self._send(payload)
        resp = self._recv()
        if "error" in resp:
            return {"error": f"exec failed: {resp['error']}"}
        return {"pid": resp["pid"]}

    # Polls one exec's status; normalized {"exited", "exit_code", "stdout",
    # "stderr", "truncated"} / {"error"} result (stdout/stderr already
    # base64-decoded). No output-size cap in v1 — "truncated" is always False.
    # In: int pid → Out: dict
    def exec_status(self, pid: int) -> dict:
        """Poll one exec's status; normalized result, decoded output."""
        self._send({"cmd": "exec-status", "pid": pid})
        resp = self._recv()
        if "error" in resp:
            return {"error": f"exec-status failed: {resp['error']}"}
        return {
            "exited":    bool(resp.get("exited")),
            "exit_code": resp.get("exit_code"),
            "stdout":    _b64(resp.get("stdout")),
            "stderr":    _b64(resp.get("stderr")),
            "truncated": False,
        }

    # Sends a ping; True if the daemon answered, False on any protocol error.
    # In: nothing → Out: bool
    def ping(self) -> bool:
        """Return whether the guest daemon answered a ping."""
        self._send({"cmd": "ping"})
        resp = self._recv()
        return bool(resp.get("pong"))

    # Closes the socket connection.
    # In: nothing → Out: nothing
    def close(self) -> None:
        """Close the serial-agent socket if open."""
        if self.sock:
            self.sock.close()
            self.sock = None

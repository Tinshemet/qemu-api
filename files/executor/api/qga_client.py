"""
qga_client.py — QEMU Guest Agent (QGA) Communication Layer

Low-level socket client for the qemu-guest-agent JSON protocol, spoken over
the VM's dedicated virtio-serial channel (see qemu_arg_builder._qga). Sibling
of qmp_client.QMPClient and deliberately shaped the same way — the wire format
is identical newline-delimited JSON — with two differences:

  * QGA has NO greeting and NO capabilities handshake. Instead, connect()
    performs a `guest-sync-delimited` round-trip: the client sends a 0xFF byte
    (which makes the agent flush any partial input and reset its JSON parser),
    then a sync command carrying a random id, then discards every byte up to
    and including the agent's 0xFF reply delimiter before reading the JSON
    response and confirming the echoed id. This guarantees the channel is
    clean and the agent is actually answering before any real command is sent.
  * The receive buffer is larger — guest-exec output comes back base64-encoded
    in a single JSON line and can be sizeable.

This module imports nothing from the manager/tool layers — the dependency edge
points inward only, exactly like qmp_client.
"""

import base64
import json
import os
import random
import socket
from typing import Optional

_CFG     = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_BUFFERS = _CFG["buffers"]
_TIMEOUTS = _CFG["timeouts"]


# Decodes a base64 QGA output field to text; empty string when absent.
# In: Optional[str] → Out: str
def _b64(data: Optional[str]) -> str:
    """Base64-decode a QGA out-data/err-data field to a UTF-8 string."""
    if not data:
        return ""
    try:
        return base64.b64decode(data).decode("utf-8", errors="replace")
    except Exception:
        return ""


class QGAClient:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.sock: Optional[socket.socket] = None

    # Opens the guest-agent socket and synchronises the protocol.
    # Supports Unix domain sockets (Linux/macOS) and TCP via "tcp:host:port" (Windows).
    # In: int timeout → Out: nothing
    def connect(self, timeout: int = _TIMEOUTS.get("qga_connect", 5)) -> None:
        """Open the QGA socket and complete a guest-sync-delimited handshake.

        Raises:
            OSError/socket.timeout: the socket can't be opened (VM not running,
                channel not wired) — caller distinguishes these.
            ConnectionError: the agent never answered the sync (channel open but
                no ``qemu-ga`` daemon running in the guest).
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
        self._sync()

    # Performs the guest-sync-delimited exchange to flush the channel and prove
    # the agent is alive. In: nothing → Out: nothing (raises on mismatch/timeout)
    def _sync(self) -> None:
        """Send guest-sync-delimited and consume up to the 0xFF-delimited reply."""
        sync_id = random.randint(1, 2**31 - 1)
        # 0xFF resets the agent's JSON parser, discarding any partial buffered
        # input, before we send the sync command.
        payload = b"\xff" + json.dumps(
            {"execute": "guest-sync-delimited", "arguments": {"id": sync_id}}
        ).encode() + b"\n"
        self.sock.sendall(payload)
        # The agent replies with a 0xFF delimiter followed by the JSON response;
        # discard everything up to and including that 0xFF, then read one line.
        buf = b""
        while b"\xff" not in buf:
            chunk = self.sock.recv(_BUFFERS.get("qga", 65536))
            if not chunk:
                raise ConnectionError("guest agent closed the channel during sync")
            buf += chunk
        buf = buf.partition(b"\xff")[2]
        while b"\n" not in buf:
            chunk = self.sock.recv(_BUFFERS.get("qga", 65536))
            if not chunk:
                raise ConnectionError("guest agent closed the channel during sync")
            buf += chunk
        line = buf.partition(b"\n")[0]
        msg  = json.loads(line.decode())
        if msg.get("return") != sync_id:
            raise ConnectionError(
                f"guest agent sync id mismatch (sent {sync_id}, got {msg.get('return')!r})"
            )

    # Serializes a dict to JSON and sends it over the socket.
    # In: dict → Out: nothing
    def _send(self, data: dict) -> None:
        """Send one JSON QGA command over the socket."""
        self.sock.sendall((json.dumps(data) + "\n").encode())

    # Reads bytes until a complete newline-delimited JSON object is assembled.
    # A single guest-exec-status reply can be large (base64 stdout/stderr), so
    # accumulate across recv() calls and frame on "\n". In: nothing → Out: dict
    def _recv(self) -> dict:
        """Read and parse one JSON QGA response."""
        buf = b""
        while True:
            if b"\n" not in buf:
                chunk = self.sock.recv(_BUFFERS.get("qga", 65536))
                if not chunk:
                    raise ConnectionError(
                        "QGA socket closed by peer while waiting for response"
                    )
                buf += chunk
                continue
            line, _, buf = buf.partition(b"\n")
            if not line.strip():
                continue
            return json.loads(line.decode())

    # Sends a QGA command with optional args and returns the response dict.
    # In: str cmd, dict args → Out: dict
    def execute(self, cmd: str, args: dict = None) -> dict:
        """Run a QGA command and return its response dict."""
        payload = {"execute": cmd}
        if args:
            payload["arguments"] = args
        self._send(payload)
        return self._recv()

    # ------------------------------------------------------------------
    # Transport-agnostic surface — mirrored by SerialAgentClient so
    # _vm_guest.py can drive either transport through the same three calls
    # without branching on which one it holds.
    # ------------------------------------------------------------------

    # Starts a guest-exec and returns {"pid": int} or {"error": str}.
    # In: str command, Optional[List[str]] args, bool shell → Out: dict
    def run_exec(self, command: str, args: Optional[list] = None, shell: bool = True) -> dict:
        """Start a command via guest-exec; normalized {"pid"} / {"error"} result."""
        if shell:
            exec_args = {"path": "/bin/sh", "arg": ["-c", command], "capture-output": True}
        else:
            exec_args = {"path": command, "arg": list(args or []), "capture-output": True}
        resp = self.execute("guest-exec", exec_args)
        if "error" in resp:
            return {"error": f"guest-exec failed: {resp['error'].get('desc', resp['error'])}"}
        return {"pid": resp["return"]["pid"]}

    # Polls guest-exec-status; normalized {"exited", "exit_code", "stdout",
    # "stderr", "truncated"} / {"error"} result (stdout/stderr already
    # base64-decoded, unlike the raw QGA response).
    # In: int pid → Out: dict
    def exec_status(self, pid: int) -> dict:
        """Poll one guest-exec's status; normalized result, decoded output."""
        status = self.execute("guest-exec-status", {"pid": pid})
        if "error" in status:
            return {"error": f"guest-exec-status failed: {status['error'].get('desc', status['error'])}"}
        ret = status["return"]
        return {
            "exited":    bool(ret.get("exited")),
            "exit_code": ret.get("exitcode", ret.get("signal")),
            "stdout":    _b64(ret.get("out-data")),
            "stderr":    _b64(ret.get("err-data")),
            "truncated": bool(ret.get("out-truncated") or ret.get("err-truncated")),
        }

    # Sends guest-ping; True if the agent answered, False on any protocol error.
    # In: nothing → Out: bool
    def ping(self) -> bool:
        """Return whether the agent answered guest-ping."""
        return "return" in self.execute("guest-ping")

    # Closes the socket connection.
    # In: nothing → Out: nothing
    def close(self) -> None:
        """Close the QGA socket if open."""
        if self.sock:
            self.sock.close()
            self.sock = None

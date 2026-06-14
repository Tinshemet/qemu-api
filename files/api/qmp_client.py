"""
qmp_client.py — QEMU Machine Protocol (QMP) Communication Layer

Handles low-level socket communication with a running QEMU process
via its QMP Unix socket.
"""

import json
import os
import socket
from typing import Optional

_CFG     = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_BUFFERS = _CFG["buffers"]


class QMPClient:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.sock: Optional[socket.socket] = None

    # Connects to the QEMU QMP socket, reads the greeting, and activates capabilities.
    # Supports Unix domain sockets (Linux/macOS) and TCP via "tcp:host:port" (Windows).
    # In: int timeout → Out: nothing
    def connect(self, timeout: int = _CFG["timeouts"]["qmp_connect"]):
        if self.socket_path.startswith("tcp:"):
            host, port = self.socket_path[4:].rsplit(":", 1)
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect((host, int(port)))
        else:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect(self.socket_path)
        self._recv()
        self._send({"execute": "qmp_capabilities"})
        self._recv()

    # Serializes a dict to JSON and sends it over the socket.
    # In: dict → Out: nothing
    def _send(self, data: dict):
        self.sock.sendall((json.dumps(data) + "\n").encode())

    # Reads bytes from the socket until a complete JSON object is assembled.
    # In: nothing → Out: dict
    def _recv(self) -> dict:
        buf = b""
        while True:
            buf += self.sock.recv(_BUFFERS["qmp"])
            try:
                return json.loads(buf.decode())
            except json.JSONDecodeError:
                continue

    # Sends a QMP command with optional args and returns the response dict.
    # In: str cmd, dict args → Out: dict
    def execute(self, cmd: str, args: dict = None) -> dict:
        payload = {"execute": cmd}
        if args:
            payload["arguments"] = args
        self._send(payload)
        return self._recv()

    # Closes the socket connection.
    # In: nothing → Out: nothing
    def close(self):
        if self.sock:
            self.sock.close()

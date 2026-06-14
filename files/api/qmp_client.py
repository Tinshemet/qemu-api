"""
qmp_client.py — QEMU Machine Protocol (QMP) Communication Layer

Handles low-level socket communication with a running QEMU process
via its QMP Unix socket.
"""

import json
import socket
from typing import Optional


class QMPClient:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.sock: Optional[socket.socket] = None

    def connect(self, timeout: int = 5):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(self.socket_path)
        self._recv()
        self._send({"execute": "qmp_capabilities"})
        self._recv()

    def _send(self, data: dict):
        self.sock.sendall((json.dumps(data) + "\n").encode())

    def _recv(self) -> dict:
        buf = b""
        while True:
            buf += self.sock.recv(4096)
            try:
                return json.loads(buf.decode())
            except json.JSONDecodeError:
                continue

    def execute(self, cmd: str, args: dict = None) -> dict:
        payload = {"execute": cmd}
        if args:
            payload["arguments"] = args
        self._send(payload)
        return self._recv()

    def close(self):
        if self.sock:
            self.sock.close()

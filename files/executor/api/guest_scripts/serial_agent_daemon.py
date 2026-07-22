#!/usr/bin/env python3
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

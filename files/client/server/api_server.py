"""
api_server.py — Client Machine HTTP Service

Runs on the client machine. Exposes execute_tool over HTTP so the AI provider
can drive QEMU/libvirt remotely. Every request must carry a valid Bearer token.

Preflight runs here (with the real QemuManager) before every execute_tool call,
so validation uses actual VM/disk state — not the empty state on the AI provider.
Preflight responses are returned as structured dicts so the AI provider's existing
clarify/error handlers pick them up without any changes on that side.

Start with:
    qemu-api serve [--host 0.0.0.0] [--port 8080] [--cert cert.pem --key key.pem]
Or directly:
    uvicorn server.api_server:app --host 0.0.0.0 --port 8080

Environment variables (client machine):
    API_TOKEN   shared secret — must match the AI provider's API_TOKEN
                server refuses to start if this is not set
                alternatively, write the token to ~/.qemu-api.token (preferred)
"""

import hashlib
import json
import os
import pathlib
import sys

from fastapi import FastAPI, HTTPException, Depends, Body, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Any, Dict, Iterator

# ── Config ────────────────────────────────────────────────────────────────────
_CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "connection_config.json")
_CFG      = json.load(open(_CFG_PATH))
_ALLOWED_TOOLS: set = set(_CFG.get("allowed_remote_tools", []))

# ── Token bootstrap ───────────────────────────────────────────────────────────
# Precedence: env var → ~/.qemu-api.token file → refuse to start.
_TOKEN_FILE = pathlib.Path.home() / ".qemu-api.token"

def _load_token() -> str:
    t = os.environ.get("API_TOKEN", "").strip()
    if t:
        return t
    if _TOKEN_FILE.exists():
        t = _TOKEN_FILE.read_text().strip()
        if t:
            return t
    return ""

_TOKEN = _load_token()
if not _TOKEN:
    print(
        "[qemu-api] ERROR: API_TOKEN not set and ~/.qemu-api.token not found.\n"
        "  Option A:  export API_TOKEN=<your-secret>\n"
        "  Option B:  echo '<your-secret>' > ~/.qemu-api.token && chmod 600 ~/.qemu-api.token\n"
        "  Set the same value on the AI provider before connecting."
    )
    sys.exit(1)

app   = FastAPI(title="qemu-api executor", version="1.0")
_auth = HTTPBearer()


def _require_token(creds: HTTPAuthorizationCredentials = Depends(_auth)):
    if creds.credentials != _TOKEN:
        raise HTTPException(status_code=401, detail="Invalid API token.")


class ExecuteRequest(BaseModel):
    tool_name: str
    args:      Dict[str, Any] = {}
    verbose:   bool           = False


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/rotate-token", dependencies=[Depends(_require_token)])
def rotate_token(new_token: str = Body(..., embed=True)):
    """Replace the in-memory token and persist it to ~/.qemu-api.token."""
    global _TOKEN
    if len(new_token) < 16:
        raise HTTPException(status_code=400, detail="New token must be at least 16 characters.")
    _TOKEN = new_token
    os.environ["API_TOKEN"] = new_token
    _TOKEN_FILE.write_text(new_token)
    _TOKEN_FILE.chmod(0o600)
    return {"ok": True, "message": "Token rotated. Update API_TOKEN on the AI provider too."}


@app.post("/execute", dependencies=[Depends(_require_token)])
def execute(req: ExecuteRequest):
    from client.executioner.tool_executor import execute_tool, manager
    from shared.preflight.validator       import _preflight_check

    # ── Tool allowlist ────────────────────────────────────────────────────────
    if _ALLOWED_TOOLS and req.tool_name not in _ALLOWED_TOOLS:
        raise HTTPException(
            status_code=403,
            detail=f"Tool '{req.tool_name}' is not in the remote allowlist. "
                   f"Add it to executor.allowed_remote_tools in config.json if intentional.",
        )

    # ── Server-side preflight (authoritative — uses real VM/disk state) ──────
    pf     = _preflight_check(req.tool_name, req.args, manager, req.verbose)
    action = pf.get("action", "ok")
    args   = req.args

    if action == "abort":
        return {
            "ok": True,
            "result": {
                "success":    False,
                "preflight":  True,
                "error":      pf.get("reason", "Pre-flight check failed."),
                "correction": pf.get("correction", ""),
            },
        }

    if action == "auto_fix":
        args = pf.get("fixed_args", args)

    if action == "ask_user":
        fix_field = pf.get("fix_field")
        question  = pf.get("question", "Please confirm.")
        options   = pf.get("options", [])
        return {
            "ok": True,
            "result": {
                "success":             False,
                "preflight":           True,
                "clarify":             True,
                "question":            question,
                "options":             options,
                "needs_clarification": fix_field,
                "missing": (
                    [{"field": fix_field, "question": question, "options": options}]
                    if fix_field else []
                ),
                "error":  pf.get("reason", "Pre-flight requires clarification."),
                "hint":   pf.get("correction", ""),
            },
        }

    # ── Remote display override ───────────────────────────────────────────────
    _LOCAL_ONLY = {"sdl", "gtk"}
    if req.tool_name == "launch_vm":
        args = dict(args)
        if args.get("display", "sdl") in _LOCAL_ONLY or "display" not in args:
            args["display"] = "vnc"
        args["vnc_bind_local"] = True

    # ── Execute ───────────────────────────────────────────────────────────────
    try:
        result = execute_tool(req.tool_name, args, req.verbose)
        if action == "auto_fix" and isinstance(result, dict):
            result["_preflight_auto_fixed"] = pf.get("correction", "Pre-flight corrected args.")
        return {"ok": True, "result": result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Ship-image delivery ───────────────────────────────────────────────────────

_CHUNK = 4 * 1024 * 1024  # 4 MB stream chunks


def _disk_path(vm_name: str) -> pathlib.Path:
    vm_dir = pathlib.Path.home() / ".qemu_vms" / vm_name
    if not vm_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"VM '{vm_name}' not found.")
    candidates = sorted(vm_dir.glob("*.qcow2"))
    if not candidates:
        raise HTTPException(status_code=404, detail=f"No qcow2 disk found for '{vm_name}'.")
    return candidates[0]


@app.get("/images/{vm_name}/sha256", dependencies=[Depends(_require_token)])
def image_sha256(vm_name: str) -> Dict[str, Any]:
    """Return the SHA-256 checksum of the VM's primary disk (for integrity verification)."""
    path = _disk_path(vm_name)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return {"vm_name": vm_name, "disk": path.name, "sha256": h.hexdigest(), "size_bytes": path.stat().st_size}


@app.get("/images/{vm_name}", dependencies=[Depends(_require_token)])
def image_download(vm_name: str, request: Request) -> StreamingResponse:
    """
    Stream the VM's primary qcow2 disk to the AI provider machine.
    Supports HTTP Range for resumable downloads.
    Response headers include X-SHA256 and X-Disk-Size for integrity checking.
    """
    path      = _disk_path(vm_name)
    total     = path.stat().st_size

    # Compute SHA256 (cheap enough for typical VM disk sizes on a LAN)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    checksum = h.hexdigest()

    range_header = request.headers.get("range")
    start, end = 0, total - 1

    if range_header:
        try:
            unit, rng = range_header.split("=")
            s, e = rng.split("-")
            start = int(s)
            end   = int(e) if e else total - 1
        except Exception:
            raise HTTPException(status_code=416, detail="Invalid Range header.")
        if start >= total or end >= total or start > end:
            raise HTTPException(status_code=416, detail="Range not satisfiable.")

    length = end - start + 1

    def _stream(path: pathlib.Path, start: int, length: int) -> Iterator[bytes]:
        remaining = length
        with open(path, "rb") as f:
            f.seek(start)
            while remaining > 0:
                data = f.read(min(_CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    status = 206 if range_header else 200
    headers = {
        "Content-Length":      str(length),
        "Content-Range":       f"bytes {start}-{end}/{total}" if range_header else f"bytes 0-{end}/{total}",
        "Accept-Ranges":       "bytes",
        "X-SHA256":            checksum,
        "X-Disk-Size":         str(total),
        "X-VM-Name":           vm_name,
        "Content-Disposition": f'attachment; filename="{path.name}"',
    }
    return StreamingResponse(
        _stream(path, start, length),
        status_code=status,
        media_type="application/octet-stream",
        headers=headers,
    )

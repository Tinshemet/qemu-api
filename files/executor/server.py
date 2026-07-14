"""
server.py — gorgon Executor Server

Lightweight FastAPI service that receives validated tool calls from the
orchestrator (AI server) over HTTP and executes them against the local
QEMU engine. No AI, no NLU — receive, validate, execute, log.

Start with:
    uvicorn executor.server:app --host 0.0.0.0 --port 8001

Environment variables:
    EXECUTOR_TOKEN   shared secret between orchestrator and executor.
                     Alternatively write the token to ~/.gorgon-executor.token
                     or set "token" in executor/config.json.

Endpoints:
    GET  /health            — liveness check, no auth required
    GET  /status            — running VM count + executor health, auth required
    GET  /tools             — list supported tool names, auth required
    POST /execute           — run a tool call, auth required
"""

import hashlib
import json
import os
import pathlib
import subprocess
import time
from typing import Any, Dict, Iterator, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────
_CFG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
with open(_CFG_PATH) as _f:
    _CFG = json.load(_f)

_TOKEN_FILE = pathlib.Path.home() / ".gorgon-executor.token"
_EVENT_LOG  = pathlib.Path.home() / ".gorgon-executor-events.jsonl"


def _load_token() -> str:
    """Load executor token: env var → token file → config file."""
    t = os.environ.get("EXECUTOR_TOKEN", "").strip()
    if t:
        return t
    if _TOKEN_FILE.exists():
        t = _TOKEN_FILE.read_text().strip()
        if t:
            return t
    return _CFG.get("token", "")


_TOKEN = _load_token()
if not _TOKEN:
    print(
        "[executor] WARNING: No token configured — remote connections will be refused.\n"
        "  Set EXECUTOR_TOKEN, write to ~/.gorgon-executor.token, "
        "or set 'token' in executor/config.json."
    )

# ── Known tools (derived from tool_executor.py's dispatch table) ──────────────
_KNOWN_TOOLS: set = {
    "revert", "clarify",
    "check_system", "scan_isos",
    "list_vms", "list_profiles", "check_profile_compatibility",
    "create_profile", "delete_profile",
    "create_vm", "clone_vm", "delete_vm",
    "launch_vm", "stop_vm", "vm_status", "monitor_vm",
    "show_config", "update_config",
    "resize_disk",
    "snapshot_create", "snapshot_list", "snapshot_restore", "snapshot_delete",
    "set_resource_limits",
    "create_network", "delete_network", "list_networks", "add_vm_to_network",
    "open_display", "open_shell",
    "check_disk", "get_vm_logs", "print_command",
    "fingerprint_vm", "send_monitor_cmd",
    "setup_done", "generate_guest_setup",
}

app   = FastAPI(title="gorgon executor", version="1.0")
_auth = HTTPBearer(auto_error=False)
_LOCALHOST = {"127.0.0.1", "::1", "localhost"}


# ── Auth ──────────────────────────────────────────────────────────────────────
def _require_token(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_auth),
) -> None:
    """Allow localhost freely; require valid Bearer token for remote callers."""
    if request.client and request.client.host in _LOCALHOST:
        return
    if not _TOKEN:
        raise HTTPException(status_code=401, detail="No executor token configured.")
    if creds is None or creds.credentials != _TOKEN:
        raise HTTPException(status_code=401, detail="Invalid executor token.")


# ── Local event log ───────────────────────────────────────────────────────────
def _log_event(tool_name: str, args: dict, result: Any, duration_ms: float) -> None:
    """Append one record to the executor's local audit log.

    Separate from the orchestrator's event log — when running on a different
    machine the orchestrator's log is not visible to the executor operator.

    Args:
        tool_name:   Tool that was called.
        args:        Arguments dict (vm name extracted for quick filtering).
        result:      Raw result returned by execute_tool.
        duration_ms: Wall time in milliseconds.

    Example log line::

        {"ts": 1720000000.0, "tool": "launch_vm", "vm": "win11",
         "success": true, "duration_ms": 312.4}
    """
    record = {
        "ts":          time.time(),
        "tool":        tool_name,
        "vm":          args.get("name", ""),
        "success":     result.get("success", False) if isinstance(result, dict) else bool(result),
        "duration_ms": round(duration_ms, 1),
    }
    try:
        with _EVENT_LOG.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass  # never let a logging failure break the response


# ── Request model ─────────────────────────────────────────────────────────────
class ExecuteRequest(BaseModel):
    tool_name: str
    args:      Dict[str, Any] = {}
    verbose:   bool           = False


from shared.executioner.tool_executor import dispatch_tool as _dispatch_tool  # noqa: E402


# ── Routes ────────────────────────────────────────────────────────────────────
_VM_BASE = pathlib.Path.home() / ".qemu_vms"
_CHUNK   = _CFG.get("io_chunk_bytes", 4 * 1024 * 1024)


def _disk_path(vm_name: str) -> pathlib.Path:
    """Return the path to a VM's first qcow2 disk, or raise 404 if absent."""
    vm_dir = _VM_BASE / vm_name
    if not vm_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"VM '{vm_name}' not found.")
    candidates = sorted(vm_dir.glob("*.qcow2"))
    if not candidates:
        raise HTTPException(status_code=404, detail=f"No qcow2 disk for '{vm_name}'.")
    return candidates[0]


@app.get("/vms/{vm_name}/disk/sha256", dependencies=[Depends(_require_token)])
def vm_disk_sha256(vm_name: str) -> Dict[str, Any]:
    """Return SHA-256 and size of the VM's primary disk."""
    path = _disk_path(vm_name)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return {"vm_name": vm_name, "disk": path.name,
            "sha256": h.hexdigest(), "size_bytes": path.stat().st_size}


@app.get("/vms/{vm_name}/disk", dependencies=[Depends(_require_token)])
def vm_disk(vm_name: str, request: Request) -> StreamingResponse:
    """Stream the VM's primary qcow2 disk with SHA256 header and Range support."""
    path  = _disk_path(vm_name)
    total = path.stat().st_size
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    checksum     = h.hexdigest()
    range_header = request.headers.get("range")
    start, end   = 0, total - 1
    if range_header:
        try:
            _, rng = range_header.split("=")
            s, e   = rng.split("-")
            start  = int(s)
            end    = int(e) if e else total - 1
        except Exception:
            raise HTTPException(status_code=416, detail="Invalid Range header.")
        if start >= total or end >= total or start > end:
            raise HTTPException(status_code=416, detail="Range not satisfiable.")
    length = end - start + 1

    def _stream() -> Iterator[bytes]:
        """Yield a byte range of a file in chunks for HTTP streaming."""
        remaining = length
        with open(path, "rb") as f:
            f.seek(start)
            while remaining > 0:
                data = f.read(min(_CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data

    return StreamingResponse(
        _stream(),
        status_code=206 if range_header else 200,
        media_type="application/octet-stream",
        headers={
            "Content-Length":      str(length),
            "Content-Range":       f"bytes {start}-{end}/{total}",
            "Accept-Ranges":       "bytes",
            "X-SHA256":            checksum,
            "X-Disk-Size":         str(total),
            "Content-Disposition": f'attachment; filename="{path.name}"',
        },
    )


@app.get("/vms/{vm_name}/bundle", dependencies=[Depends(_require_token)])
def vm_bundle(vm_name: str) -> StreamingResponse:
    """Stream the entire VM folder as a gzipped tar archive."""
    vm_dir = _VM_BASE / vm_name
    if not vm_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"VM '{vm_name}' not found.")

    def _tar_stream() -> Iterator[bytes]:
        """Yield a tar archive of a VM directory as a byte stream."""
        proc = subprocess.Popen(
            ["tar", "czf", "-", "-C", str(vm_dir.parent), vm_name],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            for chunk in iter(lambda: proc.stdout.read(65536), b""):
                yield chunk
        finally:
            proc.stdout.close()
            proc.wait()

    return StreamingResponse(
        _tar_stream(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{vm_name}.tar.gz"'},
    )


@app.get("/profiles", dependencies=[Depends(_require_token)])
def get_profiles() -> Dict[str, Any]:
    """Return all profiles, profile list, and OVMF info for orchestrator sync.

    Returns:
        ``{"profiles": {...}, "profiles_list": [...], "ovmf": {...}}``
    """
    from executor.api.qemu_config import get_all_profiles, list_profiles, OVMF
    return {
        "profiles":      get_all_profiles(),
        "profiles_list": list_profiles(),
        "ovmf":          OVMF,
    }


@app.get("/capabilities", dependencies=[Depends(_require_token)])
def get_capabilities() -> Dict[str, Any]:
    """Return system capabilities (KVM, QEMU version, disk space, etc.).

    Returns:
        Capabilities dict from ``check_system_capabilities()``.
    """
    from executor.api.qemu_config import check_system_capabilities
    return check_system_capabilities()


@app.get("/health")
def health() -> Dict[str, str]:
    """Liveness check — no auth required so load balancers can probe it.

    Returns:
        ``{"status": "ok", "version": "1.0"}``
    """
    return {"status": "ok", "version": "1.0"}


@app.get("/tools", dependencies=[Depends(_require_token)])
def tools() -> Dict[str, Any]:
    """Return the sorted list of tool names this executor supports.

    The orchestrator can call this on startup to verify the executor
    supports all tools it intends to dispatch.

    Returns:
        ``{"tools": ["check_system", "clone_vm", ...]}``
    """
    return {"tools": sorted(_KNOWN_TOOLS)}


@app.get("/commands", dependencies=[Depends(_require_token)])
def commands() -> Dict[str, Any]:
    """Return the authored command catalog that drives both help surfaces.

    The single source of truth for user-facing commands (terminal + AI-chat CLI).
    Callers filter it against the allowed-tools list before rendering.

    Returns:
        ``{"commands": [...], "category_order": [...]}``
    """
    from executor.command_catalog import COMMAND_CATALOG, CATEGORY_ORDER
    return {"commands": COMMAND_CATALOG, "category_order": CATEGORY_ORDER}


@app.get("/status", dependencies=[Depends(_require_token)])
def status() -> Dict[str, Any]:
    """Return executor health and a live VM count.

    Returns:
        ``{"status": "ok", "vms_total": 3, "vms_running": 1}``
        or ``{"status": "degraded", "error": "..."}`` on failure.

    Example::

        GET /status
        → {"status": "ok", "vms_total": 3, "vms_running": 1}
    """
    try:
        vms     = _dispatch_tool("list_vms", {}, verbose=True)
        vms     = vms if isinstance(vms, list) else []
        running = [v for v in vms if v.get("status") == "running"]
        return {
            "status":      "ok",
            "vms_total":   len(vms),
            "vms_running": len(running),
        }
    except Exception as exc:
        return {"status": "degraded", "error": str(exc)}


@app.post("/execute", dependencies=[Depends(_require_token)])
def execute(req: ExecuteRequest) -> Any:
    """Execute a validated tool call and return the result.

    Validates the tool name against the known-tools set, executes it,
    logs the outcome locally, and always returns a JSON body — never a
    bare HTTP 500 — so the orchestrator can parse the error.

    Args:
        req: Tool name, args dict, and optional verbose flag.

    Returns:
        Tool result dict, always containing ``"success": bool``.

    Example::

        POST /execute
        {"tool_name": "create_vm", "args": {"name": "win11", "os_type": "windows"}}
        → {"success": true, "name": "win11", "vm_dir": "~/.qemu_vms/win11"}

        POST /execute
        {"tool_name": "unknown_tool", "args": {}}
        → {"success": false, "error": "Unknown tool: 'unknown_tool'"}
    """
    if req.tool_name not in _KNOWN_TOOLS:
        return {"success": False, "error": f"Unknown tool: '{req.tool_name}'"}

    t0 = time.monotonic()
    try:
        result = _dispatch_tool(req.tool_name, req.args, req.verbose)
    except Exception as exc:
        result = {"success": False, "error": f"Executor error: {exc}"}
    _log_event(req.tool_name, req.args, result, (time.monotonic() - t0) * 1000)
    return result

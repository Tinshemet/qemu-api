"""
api_server.py — qemu-api Server HTTP Service

Runs on the server machine alongside Ollama and the QEMU engine.
Exposes /chat (AI loop), /execute (direct tool call), /health, /images,
and /rotate-token. Every request except /health requires a Bearer token.

Start with:
    uvicorn server.http.api_server:app --host 0.0.0.0 --port 8080

Environment variables:
    API_TOKEN   shared secret — server refuses to start if not set
                alternatively write the token to ~/.qemu-api.token
"""

import hashlib
import json
import os
import pathlib
import sys
import uuid

from fastapi import FastAPI, HTTPException, Depends, Body, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import Any, Dict, Iterator, List, Optional

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
        "[qemu-api] WARNING: No API token configured — remote connections will be refused.\n"
        "  Localhost connections are always allowed without a token.\n"
        "  To enable remote access set API_TOKEN or write to ~/.qemu-api.token"
    )

app   = FastAPI(title="qemu-api executor", version="1.0")
_auth = HTTPBearer(auto_error=False)

_LOCALHOST = {"127.0.0.1", "::1", "localhost"}


def _require_token(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_auth),
):
    if request.client and request.client.host in _LOCALHOST:
        return  # localhost always trusted
    if not _TOKEN:
        raise HTTPException(status_code=401, detail="No API token configured on server.")
    if creds is None or creds.credentials != _TOKEN:
        raise HTTPException(status_code=401, detail="Invalid API token.")


class ExecuteRequest(BaseModel):
    tool_name: str
    args:      Dict[str, Any] = {}
    verbose:   bool           = False


class ChatRequest(BaseModel):
    message:      str
    session_id:   Optional[str] = None
    auto_confirm: bool          = False
    verbose:      bool          = False


# ── In-memory session store ───────────────────────────────────────────────────
# Each session: {"messages": [...], "pending_tool": {"tool_name": str, "args": dict} | None}
_sessions: Dict[str, Dict[str, Any]] = {}


def _get_session(sid: str) -> Dict[str, Any]:
    return _sessions.get(sid, {"messages": [], "pending_tool": None, "critical_step2": False})


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/info", dependencies=[Depends(_require_token)])
def info():
    """Return server-side runtime info for the client banner."""
    from server.ai.ollama_client import OLLAMA_URL, OLLAMA_MODEL
    from shared.api.qemu_config  import OVMF
    return {
        "ollama_model":   OLLAMA_MODEL,
        "ollama_url":     OLLAMA_URL,
        "ovmf_available": OVMF.get("available", False),
        "ovmf_code":      OVMF.get("code") or "",
    }


@app.post("/chat", dependencies=[Depends(_require_token)])
def chat(req: ChatRequest):
    """
    Process one AI chat turn server-side and return the response.

    The full agentic tool loop runs here (Ollama + tool execution).
    Session state (conversation history) is kept in-memory on the server,
    keyed by session_id.

    When a dangerous operation requires confirmation, the response contains
    needs_input with the question.  The client shows the dialog and re-sends
    the user's reply with auto_confirm=True once confirmed.
    """
    from server.ai.cli import process_message
    from server.executor_client import execute_tool

    sid     = req.session_id or str(uuid.uuid4())
    session = _get_session(sid)
    messages      = list(session["messages"])
    pending_tool  = session.get("pending_tool")
    critical_step2 = session.get("critical_step2", False)

    # ── Fast-path: confirmed action — skip Ollama ─────────────────────────────
    if req.auto_confirm and pending_tool:
        tool_name = pending_tool["tool_name"]
        args      = pending_tool["args"]
        is_critical = pending_tool.get("critical", False)

        # Critical tools require a second confirmation: user must type the VM name.
        if is_critical and not critical_step2:
            expected = args.get("name", "")
            _sessions[sid] = {**session, "messages": messages,
                               "pending_tool": pending_tool, "critical_step2": True}
            return {
                "session_id":  sid,
                "text":        "",
                "tool_results": [],
                "needs_input": {
                    "type":      "confirm_critical",
                    "question":  f"Type '{expected}' to permanently confirm:",
                    "options":   [],
                    "tool_name": tool_name,
                    "proposed":  expected,
                },
            }

        # Critical step 2: validate the name the user typed.
        if is_critical and critical_step2:
            expected = args.get("name", "")
            typed    = req.message.strip()
            if typed.lower() != expected.lower():
                _sessions[sid] = {**session, "messages": messages,
                                   "pending_tool": pending_tool, "critical_step2": True}
                return {
                    "session_id":  sid,
                    "text":        f"Name didn't match — expected '{expected}'. Try again or type 'cancel'.",
                    "tool_results": [],
                    "needs_input": {
                        "type":      "confirm_critical",
                        "question":  f"Type '{expected}' to permanently confirm:",
                        "options":   [],
                        "tool_name": tool_name,
                        "proposed":  expected,
                    },
                }

        # Inject delete_disks=True for irreversible deletes so disks are cleaned up.
        if tool_name == "delete_vm":
            args = {**args, "delete_disks": True}

        try:
            result_data = execute_tool(tool_name, args, req.verbose)
        except Exception as exc:
            result_data = {"success": False, "error": str(exc)}

        # If executor requires clarification, ask the user then let AI re-plan.
        if result_data.get("clarify"):
            missing_fields = result_data.get("missing") or [{
                "field":    result_data.get("needs_clarification", ""),
                "question": result_data.get("question", "Please provide more detail."),
                "options":  result_data.get("options", []),
            }]
            mf = missing_fields[0]
            # Keep pending_tool so the user's answer goes through the normal AI path
            # with context about what was already confirmed.
            import json as _json
            _sessions[sid] = {
                "messages": messages + [
                    {"role": "assistant", "content": "",
                     "tool_calls": [{"function": {"name": tool_name, "arguments": args}}]},
                    {"role": "tool", "content": _json.dumps(result_data, default=str)},
                ],
                "pending_tool": None,
                "critical_step2": False,
            }
            return {
                "session_id":  sid,
                "text":        "",
                "tool_results": [],
                "needs_input": {
                    "type":      "clarify",
                    "question":  mf.get("question", "Please provide more detail."),
                    "options":   mf.get("options", []),
                    "field":     mf.get("field", ""),
                    "tool_name": tool_name,
                    "proposed":  None,
                },
            }

        ok_flag = result_data.get("success", True)
        label   = tool_name.replace("_", " ")
        text    = f"Done — {label} completed." if ok_flag else result_data.get("error", "Failed.")
        # Store a proper tool-call sequence so Ollama doesn't repeat the action next turn.
        import json as _json
        updated_messages = messages + [
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": tool_name, "arguments": args}}]},
            {"role": "tool",      "content": _json.dumps(result_data, default=str)},
            {"role": "assistant", "content": text},
        ]
        _sessions[sid] = {"messages": updated_messages, "pending_tool": None, "critical_step2": False}
        return {
            "session_id":  sid,
            "text":        text,
            "tool_results": [{"tool": tool_name, "args": args, "result": result_data}],
            "needs_input": None,
        }

    # ── Normal AI path ────────────────────────────────────────────────────────
    result = process_message(
        user_input   = req.message,
        messages     = messages,
        verbose      = req.verbose,
        auto_confirm = req.auto_confirm,
    )

    _sessions[sid] = {
        "messages":     result["messages"],
        "pending_tool": result.get("pending_tool"),
    }

    return {
        "session_id":  sid,
        "text":        result["text"],
        "tool_results": result["tool_results"],
        "needs_input": result["needs_input"],
    }


@app.get("/sessions", dependencies=[Depends(_require_token)])
def list_sessions():
    """List active session IDs (debug/admin)."""
    return {"sessions": list(_sessions.keys())}


@app.delete("/sessions/{session_id}", dependencies=[Depends(_require_token)])
def clear_session(session_id: str):
    """Delete a session's conversation history."""
    _sessions.pop(session_id, None)
    return {"ok": True, "session_id": session_id}


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
    from shared.executioner.tool_executor import execute_tool, manager
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

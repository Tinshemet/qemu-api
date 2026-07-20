"""
api_server.py — gorgon Server HTTP Service

Runs on the server machine alongside Ollama and the QEMU engine.
Exposes /chat (AI loop), /execute (direct tool call), /health, /images,
and /rotate-token. Every request except /health requires a Bearer token.

Start with:
    uvicorn server.http.api_server:app --host 0.0.0.0 --port 8080

Environment variables:
    API_TOKEN   shared secret — server refuses to start if not set
                alternatively write the token to ~/.gorgon.token
"""

import hashlib
import json
import os
import pathlib
import secrets
import uuid

from fastapi import FastAPI, HTTPException, Depends, Body, Request, Response, Cookie
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from typing import Any, Dict, Iterator, List, Optional

# ── Config ────────────────────────────────────────────────────────────────────
_CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "connection_config.json")
with open(_CFG_PATH) as _f:
    _CFG = json.load(_f)
_ALLOWED_TOOLS:       set  = set(_CFG.get("allowed_remote_tools", []))
_LOCAL_ONLY_DISPLAYS: set  = set(_CFG.get("local_only_displays", ["sdl", "gtk"]))
_MIN_TOKEN_LEN:       int  = _CFG.get("min_token_length", 16)
# Empty list = all allowed; non-empty = allowlist
_ALLOWED_VMS:         list = _CFG.get("client_allowed_vms",      [])
_ALLOWED_PROFILES:    list = _CFG.get("client_allowed_profiles", [])
_MAX_MESSAGE_LEN:     int  = _CFG.get("max_message_length", 32_768)
_MAX_SESSIONS:        int  = _CFG.get("max_sessions", 1_000)


def _filter_allowed(names: list, allowlist: list) -> list:
    """Return names visible to clients. Empty allowlist means all are visible."""
    if not allowlist:
        return names
    return [n for n in names if n in allowlist]


# ── Token bootstrap ───────────────────────────────────────────────────────────
# Precedence: env var → ~/.gorgon.token file → refuse to start.
_TOKEN_FILE = pathlib.Path.home() / ".gorgon.token"

def _load_token() -> str:
    """Load the API token from the environment variable or the token file."""
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
        "[gorgon] WARNING: No API token configured — remote connections will be refused.\n"
        "  Localhost connections are always allowed without a token.\n"
        "  To enable remote access set API_TOKEN or write to ~/.gorgon.token"
    )

app   = FastAPI(title="gorgon executor", version="1.0")
_auth = HTTPBearer(auto_error=False)

_LOCALHOST = {"127.0.0.1", "::1", "localhost"}
_SESSION_COOKIE_NAME = "gorgon_session"


def _active_agent_warnings() -> List[str]:
    """Advisory drift warnings for the active .grgn's tool references vs. the
    executor (missing / not-remotely-allowed). Never raises."""
    try:
        from orchestrator.ai import contract as _contract
        return _contract.agent_tool_issues(_ALLOWED_TOOLS)
    except Exception:
        return []


@app.on_event("startup")
async def _startup() -> None:
    """Sync profiles, OVMF info, and capabilities from the executor at startup,
    then log any drift between the active agent's tool references and the
    executor registry (advisory — surfaced after `gorgon agent load` restarts)."""
    from orchestrator.executor_client import sync as _sync
    _sync()
    for _msg in _active_agent_warnings():
        print(f"  ⚠ agent: {_msg}")


def _require_auth(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_auth),
    session_cookie: Optional[str] = Cookie(default=None, alias=_SESSION_COOKIE_NAME),
) -> None:
    """FastAPI dependency: require a valid API_TOKEN bearer OR a valid operator
    session (bearer token or cookie — one /login response serves both the CLI
    and a future browser client from the same session store).

    Localhost is trusted freely ONLY while no operator account exists yet —
    identical to the old behavior, so nothing breaks until an operator opts
    into the login system via `gorgon login`. The moment one exists, localhost
    is held to the same bar as anyone else — this is what actually closes the
    gap for the CLI's normal (localhost) traffic.
    """
    from orchestrator.auth import sessions as _op_sessions
    from orchestrator.auth import store as _op_store

    bootstrap_open = not _op_store.operators_exist()
    is_localhost   = bool(request.client and request.client.host in _LOCALHOST)
    if bootstrap_open and is_localhost:
        return

    # Operator session — Bearer token or cookie, either way.
    session_token = (creds.credentials if creds else None) or session_cookie
    if session_token and _op_sessions.validate_session(session_token):
        return

    # API_TOKEN bearer — unchanged machine-to-machine / AI-provider path.
    # Re-read fresh so env-var changes (token rotation, test setup) take
    # effect without a server restart.
    token = _load_token() or _TOKEN
    if token and creds is not None and secrets.compare_digest(creds.credentials, token):
        return

    if bootstrap_open and not is_localhost and not token:
        raise HTTPException(status_code=401, detail="No API token configured on server.")
    raise HTTPException(status_code=401, detail="Login required (run `gorgon login`) or provide a valid API token.")


def _require_operator_auth(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_auth),
    session_cookie: Optional[str] = Cookie(default=None, alias=_SESSION_COOKIE_NAME),
) -> None:
    """Stricter than _require_auth — applied only to /chat and /execute, the
    primary human-interactive surfaces.

    The plain API_TOKEN is shipped as the same default value
    (connection_config.json's "token") in both the server's and the client's
    config, so any interactive client (e.g. client/ui/chat_client.py) carries
    working "credentials" out of the box regardless of whether anyone has
    ever logged in — making operator login optional in practice, not
    mandatory, for exactly the surfaces this feature was built to gate.

    Once an operator account exists, ONLY a valid operator session (bearer or
    cookie) is accepted here — the shared token no longer suffices. Every
    other _require_auth-gated endpoint (rotate-token, sync, events, ...)
    keeps accepting the plain token unchanged; this is deliberately scoped to
    /chat and /execute only. Pre-bootstrap (no operators yet) behaves
    identically to _require_auth, including the plain-token fallback for
    non-localhost callers.
    """
    from orchestrator.auth import sessions as _op_sessions
    from orchestrator.auth import store as _op_store

    bootstrap_open = not _op_store.operators_exist()
    is_localhost   = bool(request.client and request.client.host in _LOCALHOST)
    if bootstrap_open:
        if is_localhost:
            return
        token = _load_token() or _TOKEN
        if token and creds is not None and secrets.compare_digest(creds.credentials, token):
            return
        raise HTTPException(
            status_code=401,
            detail="No API token configured on server." if not token else "Invalid API token.",
        )

    session_token = (creds.credentials if creds else None) or session_cookie
    if session_token and _op_sessions.validate_session(session_token):
        return
    raise HTTPException(
        status_code=401,
        detail="Login required (run `gorgon login`) — the shared API token alone no "
               "longer authorizes this endpoint once an operator account exists.",
    )


def _current_operator(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_auth),
    session_cookie: Optional[str] = Cookie(default=None, alias=_SESSION_COOKIE_NAME),
) -> Optional[str]:
    """The authenticated operator's username, or None (pre-bootstrap, or a
    shared-token/localhost caller with no operator session). Resolved the same
    way _require_operator_auth validates — used by /chat so the forge wizard can
    re-verify the operator's password before forging."""
    from orchestrator.auth import sessions as _op_sessions, store as _op_store
    if not _op_store.operators_exist():
        return None
    token = (creds.credentials if creds else None) or session_cookie
    return _op_sessions.validate_session(token) if token else None


class ExecuteRequest(BaseModel):
    tool_name: str
    args:      Dict[str, Any] = {}
    verbose:   bool           = False
    log:       bool           = True


class ChatRequest(BaseModel):
    message:      str           = Field(..., max_length=_MAX_MESSAGE_LEN)
    session_id:   Optional[str] = None
    auto_confirm: bool          = False
    verbose:      bool          = False


# ── In-memory session store ───────────────────────────────────────────────────
# Each session: {"messages": [...], "pending_tool": {"tool_name": str, "args": dict} | None,
#                "last_active": float}
_sessions: Dict[str, Dict[str, Any]] = {}
_SESSION_TTL_SECONDS = _CFG.get("session_ttl_seconds", 3600)


def _evict_expired_sessions() -> None:
    """Remove sessions that have been inactive longer than _SESSION_TTL_SECONDS."""
    import time as _time
    cutoff = _time.time() - _SESSION_TTL_SECONDS
    # Sessions without last_active are treated as live (float('inf') > cutoff always).
    expired = [sid for sid, s in list(_sessions.items()) if s.get("last_active", float("inf")) < cutoff]
    for sid in expired:
        _sessions.pop(sid, None)


def _get_session(sid: str) -> Dict[str, Any]:
    """Return (and touch) the session for *sid*, creating it with eviction if missing."""
    import time as _time
    if sid not in _sessions:
        _evict_expired_sessions()
        if len(_sessions) >= _MAX_SESSIONS:
            # Drop the oldest session to stay under the cap.
            oldest = min(_sessions, key=lambda k: _sessions[k].get("last_active", 0))
            _sessions.pop(oldest, None)
        _sessions[sid] = {"messages": [], "pending_tool": None, "critical_step2": False, "last_active": _time.time()}
    _sessions[sid]["last_active"] = _time.time()
    return _sessions[sid]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health() -> Dict[str, Any]:
    """Liveness endpoint — return a simple ok status."""
    return {"status": "ok"}


@app.get("/info", dependencies=[Depends(_require_auth)])
def info() -> Dict[str, Any]:
    """Return server-side runtime info for the client banner."""
    from orchestrator.ai.ollama_client import OLLAMA_URL, OLLAMA_MODEL
    try:
        import executor.api.qemu_config as _qc
        ovmf = _qc.OVMF
    except ImportError:
        ovmf = {"available": False, "code": ""}
    return {
        "ollama_model":   OLLAMA_MODEL,
        "ollama_url":     OLLAMA_URL,
        "ovmf_available": ovmf.get("available", False),
        "ovmf_code":      ovmf.get("code") or "",
        "agent_warnings": _active_agent_warnings(),
    }


@app.get("/events", dependencies=[Depends(_require_auth)])
def get_events(limit: int = 100, since: str = "") -> Dict[str, Any]:
    """Return recent server events (tool calls, outcomes, durations)."""
    from orchestrator.event_log import read_events
    return {"events": read_events(limit=limit, since=since)}


@app.get("/sync", dependencies=[Depends(_require_auth)])
def sync() -> Dict[str, Any]:
    """Return server-authoritative config the client should apply at startup."""
    ai_cfg_path = pathlib.Path(__file__).parent.parent / "ai" / "config.json"
    try:
        ai_cfg = json.loads(ai_cfg_path.read_text())
    except Exception:
        ai_cfg = {}

    try:
        from orchestrator.executor_client import execute_tool as _exec
        raw = _exec("list_vms", {})
        vms = raw if isinstance(raw, list) else raw.get("vms", [])
    except Exception:
        vms = []

    try:
        from orchestrator.executor_client import execute_tool as _exec
        profiles = _exec("list_profiles", {})
        if not isinstance(profiles, list):
            profiles = []
    except Exception:
        profiles = []

    vm_names      = [v.get("name") for v in vms]
    profile_names = [p.get("name") if isinstance(p, dict) else p for p in profiles]

    try:
        from executor.command_catalog import COMMAND_CATALOG
        commands = COMMAND_CATALOG
    except Exception:
        commands = []

    return {
        "shortcut_commands":    ai_cfg.get("shortcut_commands", {}),
        "allowed_remote_tools": list(_ALLOWED_TOOLS),
        "commands":             commands,
        "vms":      [{"name": n, "status": next((v.get("status") for v in vms if v.get("name") == n), None)}
                     for n in _filter_allowed(vm_names, _ALLOWED_VMS)],
        "profiles": _filter_allowed(profile_names, _ALLOWED_PROFILES),
    }


@app.post("/chat", dependencies=[Depends(_require_operator_auth)])
def chat(req: ChatRequest, operator: Optional[str] = Depends(_current_operator)) -> Dict[str, Any]:
    """
    Process one AI chat turn server-side and return the response.

    The full agentic tool loop runs here (Ollama + tool execution).
    Session state (conversation history) is kept in-memory on the server,
    keyed by session_id.

    When a dangerous operation requires confirmation, the response contains
    needs_input with the question.  The client shows the dialog and re-sends
    the user's reply with auto_confirm=True once confirmed.
    """
    from orchestrator.ai.cli import process_message
    from orchestrator.executor_client import execute_tool
    from orchestrator.ai import forge_chat
    from orchestrator.ai import forge as _forge
    _FORGE_DIR = os.path.dirname(os.path.abspath(_forge.__file__))

    _evict_expired_sessions()
    sid     = req.session_id or str(uuid.uuid4())
    session = _get_session(sid)
    messages      = list(session["messages"])
    pending_tool  = session.get("pending_tool")
    critical_step2 = session.get("critical_step2", False)
    forge_wizard   = session.get("forge_wizard")

    # ── Forge wizard: deterministic multi-turn contract elicitation ───────────
    # High-impact, so it opens with operator re-auth. Runs entirely here, never
    # touching the AI loop, and its turns (incl. the password) are never appended
    # to `messages` — the transcript stays clean.
    if forge_wizard is not None or (
        not (req.auto_confirm and pending_tool)
        and forge_chat.looks_like_forge_intent(req.message)
    ):
        from orchestrator.auth import store as _op_store

        def _verify(pw: str) -> bool:
            return bool(operator) and _op_store.verify_password(operator, pw)

        if forge_wizard is None:
            state = forge_chat.start(needs_auth=_op_store.operators_exist())
            reply, needs_input = forge_chat.current_prompt(state)
        else:
            state, reply, needs_input, _result = forge_chat.advance(
                forge_wizard, req.message, verify_password=_verify, write_dir=_FORGE_DIR)

        _sessions[sid] = {**session, "messages": messages, "forge_wizard": state,
                          "pending_tool": None, "critical_step2": False}
        return {"session_id": sid, "text": reply,
                "tool_results": [], "needs_input": needs_input}

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
            _sessions[sid] = {
                "messages": messages + [
                    {"role": "assistant", "content": "",
                     "tool_calls": [{"function": {"name": tool_name, "arguments": args}}]},
                    {"role": "tool", "content": json.dumps(result_data, default=str)},
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
        updated_messages = messages + [
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": tool_name, "arguments": args}}]},
            {"role": "tool",      "content": json.dumps(result_data, default=str)},
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


@app.get("/sessions", dependencies=[Depends(_require_auth)])
def list_sessions() -> Dict[str, Any]:
    """List active session IDs (debug/admin)."""
    return {"sessions": list(_sessions.keys())}


@app.delete("/sessions/{session_id}", dependencies=[Depends(_require_auth)])
def clear_session(session_id: str) -> Dict[str, Any]:
    """Delete a session's conversation history."""
    _sessions.pop(session_id, None)
    return {"ok": True, "session_id": session_id}


@app.post("/rotate-token", dependencies=[Depends(_require_auth)])
def rotate_token(new_token: str = Body(..., embed=True)) -> Dict[str, Any]:
    """Replace the in-memory token and persist it to ~/.gorgon.token."""
    global _TOKEN
    if len(new_token) < _MIN_TOKEN_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"New token must be at least {_MIN_TOKEN_LEN} characters.",
        )
    _TOKEN = new_token
    os.environ["API_TOKEN"] = new_token
    # Create the file 0600 from the start — write_text()+chmod leaves a brief
    # world-readable window. chmod still covers a pre-existing looser file.
    _fd = os.open(str(_TOKEN_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(_fd, new_token.encode())
    finally:
        os.close(_fd)
    _TOKEN_FILE.chmod(0o600)
    return {"ok": True, "message": "Token rotated. Update API_TOKEN on the AI provider too."}


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateOperatorRequest(BaseModel):
    username: str
    password: str


@app.post("/login")
def login(body: LoginRequest, response: Response) -> Dict[str, Any]:
    """Authenticate an operator; return a session token and set it as a cookie.

    No auth dependency — this IS the entry point auth hangs off of. Rate
    limiting/lockout is out of scope for 1.1 (single-operator, localhost-first
    threat model); revisit alongside the 1.2 multi-tenant work.
    """
    from orchestrator.auth import sessions as _op_sessions
    from orchestrator.auth import store as _op_store

    if not _op_store.verify_password(body.username, body.password):
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    token = _op_sessions.create_session(body.username)
    response.set_cookie(key=_SESSION_COOKIE_NAME, value=token, httponly=True, samesite="lax")
    return {"success": True, "session_token": token, "username": body.username}


@app.post("/logout", dependencies=[Depends(_require_auth)])
def logout(
    response: Response,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_auth),
    session_cookie: Optional[str] = Cookie(default=None, alias=_SESSION_COOKIE_NAME),
) -> Dict[str, Any]:
    """Invalidate the caller's operator session (bearer token or cookie)."""
    from orchestrator.auth import sessions as _op_sessions
    token = (creds.credentials if creds else None) or session_cookie
    _op_sessions.invalidate_session(token)
    response.delete_cookie(_SESSION_COOKIE_NAME)
    return {"success": True}


@app.post("/operators", dependencies=[Depends(_require_auth)])
def create_operator_endpoint(body: CreateOperatorRequest) -> Dict[str, Any]:
    """Create a new operator account.

    Reachable pre-bootstrap from localhost with no credentials at all — the
    same "localhost trusted until an operator exists" rule _require_auth
    applies everywhere else covers creating that first account too.
    """
    from orchestrator.auth import store as _op_store
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    result = _op_store.create_operator(body.username, body.password)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.get("/operators", dependencies=[Depends(_require_auth)])
def list_operators_endpoint() -> Dict[str, Any]:
    """List all operator usernames."""
    from orchestrator.auth import store as _op_store
    return {"operators": _op_store.list_operators()}


@app.delete("/operators/{username}", dependencies=[Depends(_require_auth)])
def delete_operator_endpoint(username: str) -> Dict[str, Any]:
    """Delete an operator account by username."""
    from orchestrator.auth import store as _op_store
    result = _op_store.delete_operator(username)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error"))
    return result


@app.post("/custom-mode", dependencies=[Depends(_require_auth)])
def custom_mode(enabled: bool = Body(..., embed=True)) -> Dict[str, Any]:
    """Toggle custom-machine mode (skip product verification) for -cu.

    Note: this is a process-global toggle (matches orchestrator/ai/cli.py's own
    -cu handling in local mode) — it affects every client talking to this
    orchestrator, not just the caller.
    """
    from orchestrator.preflight.validator import set_custom_mode
    set_custom_mode(enabled)
    return {"ok": True, "custom_mode": enabled}


def _manager_proxy() -> object:
    """Return a QemuManager wrapper in local mode, or a thin executor_client proxy in remote mode.

    Both branches filter list_vms() by _ALLOWED_VMS. Without this, preflight's own VM-existence
    checks (launch_vm, resize_disk, etc. — anything calling manager.list_vms() directly) would see
    hidden VMs as real and skip its "not found" handling, a side channel that leaks a hidden VM's
    existence through preflight's response shape even though the tool call itself would still
    correctly deny it — before this fix, this was only guarded against in remote mode.
    """
    from orchestrator.executor_client import API_URL, execute_tool as _exec
    if not API_URL or API_URL == "local":
        from shared.executioner.tool_executor import manager as _real_manager
        class _LocalProxy:
            def list_vms(self, *a, **k) -> list:
                """Filter the real manager's list_vms() by _ALLOWED_VMS."""
                vms = _real_manager.list_vms(*a, **k)
                names = _filter_allowed([v["name"] for v in vms], _ALLOWED_VMS)
                return [v for v in vms if v["name"] in names]
            def __getattr__(self, attr: str):
                return getattr(_real_manager, attr)
        return _LocalProxy()
    class _Proxy:
        def scan_isos(self) -> dict:
            """Proxy scan_isos to the executor via the HTTP /execute path."""
            return _exec("scan_isos", {})
        def list_vms(self) -> dict:
            """Proxy list_vms to the executor via the HTTP /execute path."""
            return _exec("list_vms", {})
    return _Proxy()


@app.post("/execute", dependencies=[Depends(_require_operator_auth)])
def execute(req: ExecuteRequest) -> Any:
    """Dispatch a tool call via executor_client and return its result (or raise HTTP 4xx on access/preflight failure)."""
    from orchestrator.executor_client import execute_tool
    import orchestrator.preflight.validator as _pf
    manager = _manager_proxy()

    # Tool/VM allowlist enforcement lives solely in executor_client.execute_tool() below (the
    # same point /chat already relies on with no pre-check of its own) — a prior duplicate
    # pre-check here returned a differently-shaped response (HTTP 403, {"ok": False, ...}) with
    # leakier wording than the deeper check, and disagreed with /chat's behavior for the same
    # violation. One enforcement point, one consistent response shape.

    # ── Server-side preflight (authoritative — uses real VM/disk state) ──────
    pf     = _pf._preflight_check(req.tool_name, req.args, manager, req.verbose)
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
    if req.tool_name == "launch_vm":
        args = dict(args)
        if args.get("display", "sdl") in _LOCAL_ONLY_DISPLAYS or "display" not in args:
            args["display"] = "vnc"
        args["vnc_bind_local"] = True

    # ── Execute ───────────────────────────────────────────────────────────────
    try:
        result = execute_tool(req.tool_name, args, req.verbose, log=req.log)
        if action == "auto_fix" and isinstance(result, dict):
            result["_preflight_auto_fixed"] = pf.get("correction", "Pre-flight corrected args.")
        # Filter list_vms results to only show allowed VMs
        if req.tool_name == "list_vms" and _ALLOWED_VMS and isinstance(result, list):
            result = [v for v in result if v.get("name") in _ALLOWED_VMS]
        elif req.tool_name == "list_vms" and _ALLOWED_VMS and isinstance(result, dict) and "vms" in result:
            result["vms"] = [v for v in result["vms"] if v.get("name") in _ALLOWED_VMS]
        return {"ok": True, "result": result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Ship-image delivery ───────────────────────────────────────────────────────

_CHUNK = 4 * 1024 * 1024  # 4 MB stream chunks


def _executor_url() -> str:
    """Return the executor base URL, or empty string in local mode."""
    from orchestrator.executor_client import API_URL, _TOKEN as _EXEC_TOKEN, _VERIFY as _EXEC_VERIFY
    return API_URL if API_URL and API_URL != "local" else ""


def _exec_headers() -> dict:
    """Return the auth headers for calling the executor server."""
    from orchestrator.executor_client import _TOKEN as _EXEC_TOKEN
    return {"Authorization": f"Bearer {_EXEC_TOKEN}"}


def _disk_path(vm_name: str) -> pathlib.Path:
    """Return the path to the first qcow2 disk for *vm_name*, raising HTTP 404 if absent."""
    vm_dir = pathlib.Path.home() / ".qemu_vms" / vm_name
    if not vm_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"VM '{vm_name}' not found.")
    candidates = sorted(vm_dir.glob("*.qcow2"))
    if not candidates:
        raise HTTPException(status_code=404, detail=f"No qcow2 disk found for '{vm_name}'.")
    return candidates[0]


@app.get("/images/{vm_name}/sha256", dependencies=[Depends(_require_auth)])
def image_sha256(vm_name: str) -> Dict[str, Any]:
    """Return the SHA-256 checksum of the VM's primary disk."""
    exec_url = _executor_url()
    if exec_url:
        import requests as _req
        from orchestrator.executor_client import _VERIFY as _EV
        r = _req.get(f"{exec_url}/vms/{vm_name}/disk/sha256",
                     headers=_exec_headers(), timeout=30, verify=_EV)
        if not r.ok:
            raise HTTPException(status_code=r.status_code, detail=r.text)
        return r.json()
    path = _disk_path(vm_name)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return {"vm_name": vm_name, "disk": path.name, "sha256": h.hexdigest(),
            "size_bytes": path.stat().st_size}


@app.get("/images/{vm_name}", dependencies=[Depends(_require_auth)])
def image_download(vm_name: str, request: Request) -> StreamingResponse:
    """Stream the VM's primary qcow2 disk — proxied from executor in remote mode."""
    import requests as _req
    from orchestrator.executor_client import _VERIFY as _EV
    exec_url = _executor_url()
    if exec_url:
        upstream = _req.get(
            f"{exec_url}/vms/{vm_name}/disk",
            headers={**_exec_headers(), "Range": request.headers.get("range", "")},
            stream=True, timeout=300, verify=_EV,
        )
        if not upstream.ok:
            raise HTTPException(status_code=upstream.status_code, detail=upstream.text)
        return StreamingResponse(
            upstream.iter_content(chunk_size=_CHUNK),
            status_code=upstream.status_code,
            media_type="application/octet-stream",
            headers={k: v for k, v in upstream.headers.items()
                     if k in ("Content-Length", "Content-Range", "Accept-Ranges",
                               "X-SHA256", "X-Disk-Size", "Content-Disposition")},
        )
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

    def _stream(path: pathlib.Path, start: int, length: int) -> Iterator[bytes]:
        """Yield ``length`` bytes of a file starting at ``start`` in chunks."""
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
        _stream(path, start, length),
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


@app.get("/vms/{vm_name}/bundle", dependencies=[Depends(_require_auth)])
def vm_bundle(vm_name: str) -> StreamingResponse:
    """Stream the entire VM folder as a tar.gz — proxied from executor in remote mode."""
    import requests as _req, subprocess as _sp
    from orchestrator.executor_client import _VERIFY as _EV
    exec_url = _executor_url()
    if exec_url:
        upstream = _req.get(f"{exec_url}/vms/{vm_name}/bundle",
                            headers=_exec_headers(), stream=True, timeout=300, verify=_EV)
        if not upstream.ok:
            raise HTTPException(status_code=upstream.status_code, detail=upstream.text)
        return StreamingResponse(
            upstream.iter_content(chunk_size=65536),
            media_type="application/gzip",
            headers={"Content-Disposition": f'attachment; filename="{vm_name}.tar.gz"'},
        )
    vm_dir = pathlib.Path.home() / ".qemu_vms" / vm_name
    if not vm_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"VM '{vm_name}' not found.")

    def _tar_stream() -> Iterator[bytes]:
        """Yield a tar archive of the VM directory as a byte stream."""
        proc = _sp.Popen(
            ["tar", "czf", "-", "-C", str(vm_dir.parent), vm_name],
            stdout=_sp.PIPE, stderr=_sp.DEVNULL,
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

"""
orchestrator/auth/sessions.py — Operator session tokens.

Distinct from the unrelated chat-conversation `_sessions` dict in
orchestrator/http/api_server.py (that's per-conversation message history;
this is operator identity). File-backed rather than in-memory, on purpose:
orchestrator/ai/direct_cli.py's in-process dispatch path and the HTTP server
are separate processes that both need to see the same live session state —
the same reasoning behind executor/api/vm_state.py's VMState fix this same
session (every read re-loads the file rather than trusting a snapshot, since
this file is the one shared source of truth across whichever process asks).

A session token doubles as both a CLI bearer token and a browser cookie
value (see orchestrator/http/api_server.py's /login) — one mechanism, one
store, so the not-yet-built Web UI needs no separate auth work later.
"""
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

_GORGON_DIR      = Path.home() / ".gorgon"
SESSIONS_FILE    = _GORGON_DIR / "operator_sessions.json"
# This box's currently-logged-in operator, as distinct from SESSIONS_FILE (all
# valid sessions server-side) — conceptually separate even though both live on
# one host today. Shared by orchestrator/ai/direct_cli.py's cli_direct() gate
# and cli.py's chat_loop() gate so the two entry points can't drift.
CURRENT_SESSION_FILE = _GORGON_DIR / "current_session"

_CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "connection_config.json")
with open(_CFG_PATH) as _f:
    _CFG = json.load(_f)
_TTL_HOURS = _CFG.get("operator_session_ttl_hours", 12)


def _load() -> Dict[str, Dict[str, Any]]:
    """Load the session store, or an empty dict if it doesn't exist yet."""
    if not SESSIONS_FILE.exists():
        return {}
    try:
        return json.loads(SESSIONS_FILE.read_text())
    except Exception:
        return {}  # corrupt/unreadable store — treat as empty rather than crash


def _save(data: Dict[str, Dict[str, Any]]) -> None:
    """Persist the session store atomically at 0600."""
    # Derived from SESSIONS_FILE itself (see store.py's _save for the same
    # reasoning) so patching the file constant alone fully redirects this.
    SESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(SESSIONS_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(data, indent=2).encode())
    finally:
        os.close(fd)
    SESSIONS_FILE.chmod(0o600)


def create_session(username: str) -> str:
    """Create a new session for username; return the opaque session token."""
    data  = _load()
    token = secrets.token_urlsafe(32)
    now   = datetime.now(timezone.utc)
    data[token] = {
        "username": username,
        "created":  now.isoformat(),
        "expires":  (now + timedelta(hours=_TTL_HOURS)).isoformat(),
    }
    _save(data)
    return token


def validate_session(token: Optional[str]) -> Optional[str]:
    """Return the session's username if token is valid and unexpired, else None.

    Expires lazily on read: a hit past its expiry is dropped from the store
    here rather than requiring a separate sweep process.
    """
    if not token:
        return None
    data  = _load()
    entry = data.get(token)
    if not entry:
        return None
    # A malformed entry (missing/unparseable `expires`) must fail closed, not
    # 500 the request — drop it and treat the token as invalid.
    try:
        expires = datetime.fromisoformat(entry["expires"])
    except (KeyError, ValueError, TypeError):
        data.pop(token, None)
        _save(data)
        return None
    if datetime.now(timezone.utc) >= expires:
        data.pop(token, None)
        _save(data)
        return None
    # A session must not outlive its operator: if the account was deleted after
    # this token was issued, the token stops authorizing immediately rather than
    # coasting to its TTL. (store never imports sessions, so this local import
    # is cycle-free.)
    from orchestrator.auth import store as _store
    if entry.get("username") not in _store.list_operators():
        data.pop(token, None)
        _save(data)
        return None
    return entry["username"]


def invalidate_session(token: Optional[str]) -> None:
    """Remove a session token from the store; no-op if it's already absent."""
    if not token:
        return
    data = _load()
    if token in data:
        data.pop(token, None)
        _save(data)


def read_current_session() -> Optional[str]:
    """Return this box's cached login token, or None if never logged in."""
    if not CURRENT_SESSION_FILE.exists():
        return None
    token = CURRENT_SESSION_FILE.read_text().strip()
    return token or None


def write_current_session(token: str) -> None:
    """Cache token as this box's active login, at 0600."""
    CURRENT_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(CURRENT_SESSION_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    CURRENT_SESSION_FILE.chmod(0o600)


def clear_current_session() -> None:
    """Remove this box's cached login token, if any."""
    try:
        CURRENT_SESSION_FILE.unlink()
    except FileNotFoundError:
        pass


def current_username() -> Optional[str]:
    """Return the username of this box's active, still-valid login, or None."""
    return validate_session(read_current_session())

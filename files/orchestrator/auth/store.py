"""
orchestrator/auth/store.py — Operator account storage.

Backs the operator login layer that sits above the existing API_TOKEN
bearer secret (see orchestrator/http/api_server.py's _require_auth and
orchestrator/ai/direct_cli.py's cli_direct() gate). Single-operator in
practice for 1.1, but the on-disk schema carries `role`/`tenant_id` so the
1.2 multi-tenant milestone can extend this store instead of migrating it.

Passwords are hashed with stdlib hashlib.scrypt (no bcrypt/passlib dependency
anywhere else in this repo) — a per-user random salt, verified in constant
time via hmac.compare_digest. Never store or compare plaintext.
"""
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_GORGON_DIR     = Path.home() / ".gorgon"
OPERATORS_FILE  = _GORGON_DIR / "operators.json"

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN    = 32


def _load() -> Dict[str, Dict[str, Any]]:
    """Load the operator store, or an empty dict if it doesn't exist yet."""
    if not OPERATORS_FILE.exists():
        return {}
    try:
        return json.loads(OPERATORS_FILE.read_text())
    except Exception:
        return {}  # corrupt/unreadable store — treat as empty rather than crash


def _save(data: Dict[str, Dict[str, Any]]) -> None:
    """Persist the operator store atomically at 0600."""
    # Derived from OPERATORS_FILE itself (not the separate _GORGON_DIR
    # constant) so patching OPERATORS_FILE alone — as the test suite's
    # _isolated_auth_paths() does — fully redirects this, with no stray
    # directory created on the real host as a side effect.
    OPERATORS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Create the file 0600 from the start — write_text()+chmod leaves a brief
    # world-readable window (same reasoning as api_server.py's token file).
    fd = os.open(str(OPERATORS_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, json.dumps(data, indent=2).encode())
    finally:
        os.close(fd)
    OPERATORS_FILE.chmod(0o600)


def _hash_password(password: str, salt: bytes) -> str:
    """Return the hex-encoded scrypt hash of password+salt."""
    return hashlib.scrypt(
        password.encode(), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN,
    ).hex()


def operators_exist() -> bool:
    """True if at least one operator account has been created.

    The gate everything else hinges on: while this is False, the CLI and
    the HTTP API's localhost bypass both behave exactly as they did before
    this feature existed — pure backward compatibility until an operator
    opts in by running `gorgon login` for the first time.
    """
    return bool(_load())


def create_operator(username: str, password: str) -> Dict[str, Any]:
    """Create a new operator account.

    Returns:
        ``{"success": True}`` or ``{"success": False, "error": str}`` if the
        username already exists.
    """
    data = _load()
    if username in data:
        return {"success": False, "error": f"Operator '{username}' already exists."}
    salt = secrets.token_bytes(16)
    data[username] = {
        "password_hash": _hash_password(password, salt),
        "salt":          salt.hex(),
        "role":          "operator",
        "tenant_id":     None,
        "created":       datetime.now(timezone.utc).isoformat(),
    }
    _save(data)
    return {"success": True}


def verify_password(username: str, password: str) -> bool:
    """Return True if password matches the stored hash for username."""
    data  = _load()
    entry = data.get(username)
    if not entry:
        # Compute a throwaway scrypt anyway so a nonexistent username costs the
        # same wall-clock as a real one — otherwise the instant return leaks
        # which usernames exist (timing-based enumeration).
        _hash_password(password, secrets.token_bytes(16))
        return False
    salt     = bytes.fromhex(entry["salt"])
    expected = entry["password_hash"]
    actual   = _hash_password(password, salt)
    return hmac.compare_digest(actual, expected)


def list_operators() -> List[str]:
    """Return all operator usernames."""
    return list(_load().keys())


def delete_operator(username: str) -> Dict[str, Any]:
    """Delete an operator account by username.

    Refuses to remove the last remaining operator: because every auth gate
    keys off ``operators_exist()``, emptying the store would silently revert
    the CLI and the HTTP localhost bypass to bootstrap-open mode. Disabling
    auth must be an explicit, deliberate act — not a side effect of deleting
    the final account.
    """
    data = _load()
    if username not in data:
        return {"success": False, "reason": "not_found",
                "error": f"Operator '{username}' not found."}
    if len(data) == 1:
        return {"success": False, "reason": "last_operator",
                "error": (f"Cannot delete '{username}': it is the last operator. "
                          "Removing it would disable operator authentication and "
                          "revert to localhost-open mode. Create another operator first.")}
    del data[username]
    _save(data)
    return {"success": True}

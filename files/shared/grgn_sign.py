"""grgn_sign.py — confidentiality + tamper-evidence for .grgn agent files.

A .grgn holds a campaign contract (persona, toolkit, red lines, and the SAFEWORD
kill-switch) — sensitive, and until now plaintext JSON anyone could read or edit.
Two file formats, both keyed by a per-install secret (~/.gorgon.key, 0600):

  ENCRYPTED (forged contracts) — a Fernet token (AES-128-CBC + HMAC). Contents are
    hidden AND authenticated in one; only this install's key can read or produce
    it. Forged contracts are written this way, so the safeword never sits in
    cleartext on disk.

  PLAINTEXT + SIDECAR (the built-in doorman/conductor/lab templates) — readable
    JSON (their content is already public in the repo) with an HMAC sidecar
    (<name>.sig) for integrity only. Keeps the git-tracked templates clean and
    portable while still detecting tampering.

read() auto-detects the format. Fernet's key is derived from ~/.gorgon.key via
SHA-256 so the same secret drives both mechanisms.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
from typing import Any, Dict, Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken

_KEY_PATH   = os.path.expanduser("~/.gorgon.key")
_FERNET_PFX = b"gAAAAA"          # every Fernet token starts with this (base64 of v0x80 + ts)


def key_path() -> str:
    return _KEY_PATH


def _key() -> bytes:
    """The per-install secret — read from ~/.gorgon.key, generated (0600) on first
    use, like ~/.gorgon.token."""
    try:
        with open(_KEY_PATH, "rb") as f:
            k = f.read().strip()
        if k:
            return k
    except (FileNotFoundError, OSError):
        pass
    k = secrets.token_hex(32).encode("ascii")
    fd = os.open(_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(k)
    return k


def _fernet() -> Fernet:
    """A Fernet keyed by ~/.gorgon.key (SHA-256 → 32-byte urlsafe key)."""
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(_key()).digest()))


def _canonical(contract: Dict[str, Any]) -> bytes:
    return json.dumps(contract, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


# ── Encrypted format (forged contracts) ─────────────────────────────────────────

def write_encrypted(contract: Dict[str, Any], path: str) -> str:
    """Encrypt a contract to a Fernet token and write it as the .grgn (0600).
    Contents are hidden and authenticated — no sidecar needed."""
    token = _fernet().encrypt(_canonical(contract))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(token)
    return path


# ── Plaintext + HMAC sidecar (built-in templates) ───────────────────────────────

def sig_path(grgn_path: str) -> str:
    return grgn_path + ".sig"


def _signature(contract: Dict[str, Any]) -> str:
    return hmac.new(_key(), _canonical(contract), hashlib.sha256).hexdigest()


def sign_file(grgn_path: str) -> str:
    """Write/refresh the HMAC sidecar for a PLAINTEXT .grgn. Returns the sig path."""
    with open(grgn_path) as f:
        contract = json.load(f)
    sp = sig_path(grgn_path)
    fd = os.open(sp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(_signature(contract))
    return sp


# ── Unified read / status ───────────────────────────────────────────────────────

def read(path: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Load a .grgn of either format → (contract, status). status:
        'encrypted' — Fernet token, decrypted OK (confidential + authenticated)
        'signed'    — plaintext with a valid HMAC sidecar
        'unsigned'  — plaintext with no sidecar (trust-on-first-use)
        'tampered'  — Fernet decrypt failed, sidecar mismatch, or unparseable
        'missing'   — no such file
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except (FileNotFoundError, OSError):
        return None, "missing"
    stripped = raw.strip()
    if stripped.startswith(_FERNET_PFX):
        try:
            data = _fernet().decrypt(stripped)
            return json.loads(data.decode("utf-8")), "encrypted"
        except (InvalidToken, ValueError, Exception):
            return None, "tampered"                 # wrong key or corrupted
    # plaintext JSON (a built-in template)
    try:
        contract = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, "tampered"
    try:
        with open(sig_path(path)) as f:
            stored = f.read().strip()
    except (FileNotFoundError, OSError):
        return contract, "unsigned"
    return (contract, "signed") if hmac.compare_digest(_signature(contract), stored) \
        else (None, "tampered")


def status(path: str) -> str:
    """Just the integrity/format status of a .grgn (see read())."""
    return read(path)[1]


def ensure_integrity(path: str) -> str:
    """Trust-on-first-use for the active agent: an unsigned PLAINTEXT template gets
    an HMAC sidecar; encrypted/signed/tampered files are left as-is. Returns the
    resulting status."""
    st = status(path)
    if st == "unsigned":
        sign_file(path)
        return "signed"
    return st

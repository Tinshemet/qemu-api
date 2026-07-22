"""
orchestrator/http/context.py — shared configuration for the HTTP layer.

The connection_config.json load and the allowlists/limits derived from it live
here as the single source of truth, so api_server.py (routing + auth) and the
endpoint-body modules (chat_endpoint, execute_endpoint, image_delivery) all read
the same values without importing each other — which would be circular.
"""
import json
import os

_CFG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "connection_config.json")
with open(_CFG_PATH) as _f:
    _CFG = json.load(_f)

ALLOWED_TOOLS:       set  = set(_CFG.get("allowed_remote_tools", []))
LOCAL_ONLY_DISPLAYS: set  = set(_CFG.get("local_only_displays", ["sdl", "gtk"]))
MIN_TOKEN_LEN:       int  = _CFG.get("min_token_length", 16)
# Empty list = all allowed; non-empty = allowlist
ALLOWED_VMS:         list = _CFG.get("client_allowed_vms",      [])
ALLOWED_PROFILES:    list = _CFG.get("client_allowed_profiles", [])
MAX_MESSAGE_LEN:     int  = _CFG.get("max_message_length", 32_768)
MAX_SESSIONS:        int  = _CFG.get("max_sessions", 1_000)
SESSION_TTL_SECONDS: int  = _CFG.get("session_ttl_seconds", 3600)

# ── image/bundle delivery (proxy to the executor) ──────────────────────────────
IO_CHUNK_BYTES:         int = _CFG.get("io_chunk_bytes", 4 * 1024 * 1024)   # disk stream chunk
BUNDLE_CHUNK_BYTES:     int = _CFG.get("bundle_chunk_bytes", 65_536)        # tar.gz proxy chunk
PROXY_SHA256_TIMEOUT_S: int = _CFG.get("proxy_sha256_timeout_s", 30)        # sha256 proxy request
PROXY_STREAM_TIMEOUT_S: int = _CFG.get("proxy_stream_timeout_s", 300)       # disk/bundle stream proxy

LOCALHOST           = {"127.0.0.1", "::1", "localhost"}
SESSION_COOKIE_NAME = "gorgon_session"


def filter_allowed(names: list, allowlist: list) -> list:
    """Return names visible to clients. Empty allowlist means all are visible."""
    if not allowlist:
        return names
    return [n for n in names if n in allowlist]

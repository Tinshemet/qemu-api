"""
admin/api_client.py — HTTP access to the orchestrator from the admin TUI.

Thin request helpers plus the tool-execute wrapper and a briefly-cached health
check. All network config (URL, token, timeouts) comes from admin.config.
"""

import time

from admin import config


def post(path: str, body: dict) -> dict:
    """POST to an admin-server path; return the parsed JSON (or an error dict)."""
    import requests
    try:
        r = requests.post(
            f"{config.ORCH_URL}{path}",
            json=body,
            headers={"Authorization": f"Bearer {config.token()}"},
            timeout=config.HTTP_TIMEOUT_S,
        )
        return r.json() if r.ok else {"success": False, "error": r.text[:120]}
    except Exception as e:
        return {"success": False, "error": str(e)[:80]}


def get(path: str, params: dict = None) -> dict:
    """GET an admin-server path; return the parsed JSON (or an error dict)."""
    import requests
    try:
        r = requests.get(
            f"{config.ORCH_URL}{path}",
            params=params or {},
            headers={"Authorization": f"Bearer {config.token()}"},
            timeout=config.HTTP_TIMEOUT_S,
        )
        return r.json() if r.ok else {}
    except Exception:
        return {}


def exec_tool(tool_name: str, args: dict = None, log: bool = True) -> dict:
    """Run a tool via the admin server's /execute endpoint.

    Unwraps the {"ok": bool, "result": ...} envelope so callers get the tool's
    actual result directly. Falls back to the raw response on a transport-level
    failure (post's own {"success": False, "error": ...} on a network error),
    since that shape has no "result" key to unwrap.

    Passes verbose=True — the admin TUI is a machine caller polling once a second;
    without it the server prints a full Rich-rendered table to its own console/log
    on every single poll tick.

    log=False skips the server's persistent event log for this call — use it only
    for the dashboard's own automatic background refresh, never for a command the
    operator actually typed, otherwise the "list_vms" poll drowns the event feed.
    """
    r = post(config.EXECUTE_PATH, {"tool_name": tool_name, "args": args or {}, "verbose": True, "log": log})
    return r.get("result", r) if isinstance(r, dict) else r


def vm_list(raw) -> list:
    """Normalize a list_vms result to a bare list of VM dicts.

    execute_tool returns list_vms as {"success": True, "vms": [...]}, but older
    call sites here assumed a bare list. Accept both shapes (matching the
    orchestrator's own api_server handling) so a dict envelope isn't silently
    dropped, leaving the table empty while the server reports N items.
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return raw.get("vms", [])
    return []


def get_events(limit: int = config.EVENTS_LIMIT) -> list:
    """Return the most recent event-log entries from the server."""
    return get(config.EVENTS_PATH, {"limit": limit}).get("events", [])


# ── health check (cached briefly) ──────────────────────────────────────────────

_health_cache: tuple = (0.0, False)


def server_online() -> bool:
    """Return True if the admin server is reachable (result cached briefly)."""
    global _health_cache
    now = time.monotonic()
    if now - _health_cache[0] < config.HEALTH_CACHE_S:
        return _health_cache[1]
    import requests
    try:
        result = requests.get(f"{config.ORCH_URL}{config.HEALTH_PATH}", timeout=config.HEALTH_TIMEOUT_S).ok
    except Exception:
        result = False
    _health_cache = (now, result)
    return result

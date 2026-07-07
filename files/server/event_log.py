"""
event_log.py — Structured event logger for qemu-api server

Writes one JSON line per event to ~/.qemu_vms/events.log.
Each entry records the tool called, key args, outcome, and duration.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

_LOG_DIR  = Path.home() / ".qemu_vms"
_LOG_FILE = _LOG_DIR / "events.log"
_MAX_BYTES = 10 * 1024 * 1024  # rotate at 10 MB


def _summarise_args(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the meaningful fields from args without logging secrets.

    Args:
        tool: Tool name (unused, reserved for future per-tool filtering).
        args: Full argument dict passed to the tool.

    Returns:
        Dict containing only the allowed display keys.

    Example::

        _summarise_args("launch_vm", {"name": "myvm", "token": "secret"})
        # → {"name": "myvm"}
    """
    keep = {}
    for key in ("name", "src", "dst", "display", "size_gb", "tag", "network", "profile"):
        if key in args:
            keep[key] = args[key]
    return keep


def _summarise_result(result: Any) -> str:
    if isinstance(result, dict):
        if result.get("success") is False:
            return result.get("error", "failed")
        if result.get("already_running"):
            return "already_running"
        return "ok"
    if isinstance(result, list):
        return f"{len(result)} items"
    return "ok"


def log_event(tool: str, args: Dict[str, Any], result: Any, duration_ms: float):
    """Append one event line to the log. Safe to call from any thread.

    Args:
        tool:        Tool name (e.g. ``"launch_vm"``).
        args:        Full argument dict; secrets are stripped before writing.
        result:      Tool return value; summarised to ``"ok"``/``"failed"``/…
        duration_ms: Wall-clock time for the call in milliseconds.

    Example::

        log_event("launch_vm", {"name": "myvm"}, {"success": True}, 42.3)
        # appends {"ts": "...", "tool": "launch_vm", "args": {"name": "myvm"},
        #          "outcome": "ok", "duration_ms": 42.3} to events.log
    """
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)

        # Rotate if oversized
        if _LOG_FILE.exists() and _LOG_FILE.stat().st_size > _MAX_BYTES:
            _LOG_FILE.rename(_LOG_FILE.with_suffix(".log.1"))

        entry = {
            "ts":          datetime.now(timezone.utc).isoformat(),
            "tool":        tool,
            "args":        _summarise_args(tool, args),
            "outcome":     _summarise_result(result),
            "duration_ms": round(duration_ms, 1),
        }
        with open(_LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # never crash the caller


def read_events(limit: int = 100, since: str = "") -> list:
    """Read the last ``limit`` events, optionally filtered to after ``since``.

    Args:
        limit: Maximum number of events to return (most-recent first).
        since: ISO-8601 timestamp; skip any event at or before this time.

    Returns:
        List of event dicts, newest first. Empty list if log is missing.

    Example::

        read_events(limit=5)
        # → [{"ts": "2025-01-01T...", "tool": "launch_vm", ...}, ...]
        read_events(limit=100, since="2025-01-01T12:00:00+00:00")
        # → only events after noon on Jan 1
    """
    if not _LOG_FILE.exists():
        return []
    try:
        lines = _LOG_FILE.read_text().splitlines()
        events = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if since and e.get("ts", "") <= since:
                break
            events.append(e)
            if len(events) >= limit:
                break
        return list(reversed(events))
    except Exception:
        return []

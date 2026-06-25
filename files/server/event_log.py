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
    """Extract the meaningful fields from args without logging secrets."""
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
    """Append one event line to the log. Safe to call from any thread."""
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
    """Read the last `limit` events, optionally filtered to after `since` (ISO timestamp)."""
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

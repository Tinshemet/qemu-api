"""
session.py — Conversation Session Persistence Layer

Saves and loads the Ollama chat history to disk so conversations
persist across terminal restarts.
"""

import json
import os
from typing import Dict, List, Optional

_CFG         = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_SESSION_CFG = _CFG["session"]
_DRIFT       = _CFG.get("drift_thresholds", {})

SESSION_FILE        = os.path.expanduser(_SESSION_CFG["file"])
MAX_SESSION_HISTORY = _SESSION_CFG["max_history"]
AUTO_CLEAR_SESSION  = _SESSION_CFG.get("auto_clear", False)


# Reads chat history from ~/.qemu_vms/.session.json, capped at the last 40 messages.
# In: nothing → Out: List[dict]
def load_session() -> List[Dict]:
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE) as f:
                data = json.load(f)
            return data[-MAX_SESSION_HISTORY:]
        except Exception:
            pass
    return []


# Filters to only user/assistant turns and writes the last 40 to session.json.
# In: List[dict] messages → Out: nothing
def save_session(messages: List[Dict]):
    try:
        # Walk the raw message list to separate verified assistant messages
        # (those that immediately follow a tool result) from unverified ones
        # (text-only responses with no preceding tool execution). Unverified
        # "Done — X is created" patterns poison future sessions by training the
        # model to hallucinate success without calling any tool.
        filtered = []
        prev_was_tool_result = False
        for m in messages:
            role = m.get("role")
            content = m.get("content", "")
            if role == "tool":
                prev_was_tool_result = True
                continue                             # tool results are not persisted
            if not content or str(content).startswith("_INTERNAL_"):
                prev_was_tool_result = False
                continue
            if role == "user":
                filtered.append(m)
                prev_was_tool_result = False
            elif role == "assistant" and not m.get("tool_calls"):
                # Only persist assistant text if a real tool result preceded it.
                # Text that arrives without a tool result is unverified and must
                # not be saved regardless of what it claims.
                if prev_was_tool_result:
                    filtered.append(m)
                prev_was_tool_result = False

        # Remove trailing user messages with no following assistant response —
        # orphaned turns confuse the model on the next session load.
        while filtered and filtered[-1].get("role") == "user":
            filtered.pop()
        filtered = filtered[-MAX_SESSION_HISTORY:]
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w") as f:
            json.dump(filtered, f, indent=2)
    except Exception:
        pass


# Inspects loaded session history for signs of drift.
# Returns ("critical"|"warn", message) or None if the session looks healthy.
# critical: orphan ratio > 65% or >=6 consecutive unanswered turns (like the
#           77%-unverified session you saw — model is almost certainly poisoned).
# warn    : orphan ratio > 40% or >=3 consecutive unanswered turns (early signal).
# In: List[dict] messages → Out: Optional[tuple[str, str]]
def detect_drift(messages: List[Dict]) -> Optional[tuple]:
    if not messages:
        return None

    user_count      = sum(1 for m in messages if m.get("role") == "user")
    assistant_count = sum(1 for m in messages if m.get("role") == "assistant")

    _min_turns      = _DRIFT.get("min_user_turns",  4)
    _crit_ratio     = _DRIFT.get("critical_ratio",  0.65)
    _warn_ratio     = _DRIFT.get("warn_ratio",      0.40)
    _crit_consec    = _DRIFT.get("critical_consec", 6)
    _warn_consec    = _DRIFT.get("warn_consec",     3)

    if user_count >= _min_turns:
        orphan_ratio = (user_count - assistant_count) / user_count
        pct = int(orphan_ratio * 100)
        if orphan_ratio > _crit_ratio:
            return (
                "critical",
                f"session severely drifted: {user_count} user turns, only "
                f"{assistant_count} verified responses ({pct}% unverified) — "
                f"the model is likely poisoned. Type 'clear session' now.",
            )
        if orphan_ratio > _warn_ratio:
            return (
                "warn",
                f"session drift: {user_count} user turns but only "
                f"{assistant_count} verified responses ({pct}% unverified) — "
                f"type 'clear session' to reset",
            )

    max_consec, consec = 0, 0
    for m in messages:
        if m.get("role") == "user":
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 0
    if max_consec >= _crit_consec:
        return (
            "critical",
            f"session severely drifted: {max_consec} consecutive unanswered "
            f"user turns — the model is likely poisoned. Type 'clear session' now.",
        )
    if max_consec >= _warn_consec:
        return (
            "warn",
            f"session drift: {max_consec} consecutive unanswered user turns "
            f"— type 'clear session' to reset",
        )

    return None


# Deletes the session file if it exists.
# In: nothing → Out: nothing
def clear_session():
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)


# Patches the auto_clear flag in config.json and updates the module-level constant.
# In: bool enabled → Out: nothing
def set_auto_clear(enabled: bool):
    global AUTO_CLEAR_SESSION
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    cfg["session"]["auto_clear"] = enabled
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)
    AUTO_CLEAR_SESSION = enabled


# Patches tool_loop_max_override in config.json and returns the new effective limit.
# Pass None to clear the override and revert to the default.
# In: int|None limit → Out: int effective limit
def set_loop_max(limit):
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    cfg["chat"]["tool_loop_max_override"] = limit
    with open(cfg_path, "w") as f:
        json.dump(cfg, f, indent=2)
    return limit if limit is not None else cfg["chat"]["tool_loop_max"]


def get_loop_max() -> int:
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(cfg_path) as f:
        cfg = json.load(f)
    override = cfg["chat"].get("tool_loop_max_override")
    return override if override is not None else cfg["chat"]["tool_loop_max"]

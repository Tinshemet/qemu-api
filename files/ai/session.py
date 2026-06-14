"""
session.py — Conversation Session Persistence Layer

Saves and loads the Ollama chat history to disk so conversations
persist across terminal restarts.
"""

import json
import os
from typing import Dict, List

_CFG = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_SESSION_CFG = _CFG["session"]

SESSION_FILE        = os.path.expanduser(_SESSION_CFG["file"])
MAX_SESSION_HISTORY = _SESSION_CFG["max_history"]


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
        # Only persist real user/assistant turns.
        # Exclude: tool results, internal retry injections (_INTERNAL_ prefix),
        # and assistant messages that only have tool_calls but no visible content.
        filtered = [
            m for m in messages
            if m.get("role") in ("user", "assistant")
            and m.get("content")
            and not str(m.get("content", "")).startswith("_INTERNAL_")
            and not m.get("tool_calls")
        ][-MAX_SESSION_HISTORY:]
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w") as f:
            json.dump(filtered, f, indent=2)
    except Exception:
        pass


# Deletes the session file if it exists.
# In: nothing → Out: nothing
def clear_session():
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)

"""
session.py — Conversation Session Persistence Layer

Saves and loads the Ollama chat history to disk so conversations
persist across terminal restarts.
"""

import json
import os
from typing import Dict, List

SESSION_FILE        = os.path.expanduser("~/.qemu_vms/.session.json")
MAX_SESSION_HISTORY = 40  # keep last N messages to avoid context overflow


def load_session() -> List[Dict]:
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE) as f:
                data = json.load(f)
            return data[-MAX_SESSION_HISTORY:]
        except Exception:
            pass
    return []


def save_session(messages: List[Dict]):
    try:
        # Don't persist system prompt or tool results — just user/assistant turns
        filtered = [
            m for m in messages
            if m.get("role") in ("user", "assistant") and m.get("content")
        ][-MAX_SESSION_HISTORY:]
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w") as f:
            json.dump(filtered, f, indent=2)
    except Exception:
        pass


def clear_session():
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)

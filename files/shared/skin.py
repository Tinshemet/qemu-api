"""
skin.py — the per-agent appearance skin.

A skin is the slice of TUI/CLI appearance an agent can override for itself:
``text_color`` (the accent hex), ``font_size``, ``font_family``, and a ``banner``
line. It lives in the agent's bundle as ``skin.json``; when that agent is active,
its non-null values override the global defaults the caller supplies. A missing
skin (or an all-null one) inherits everything — so an agent has a distinct look
only if it opts in.

Kept in shared/ so both the client (which paints the TUI) and the server (which can
report the active skin) resolve it identically. The CALLER passes its global
defaults as ``base`` — shared/ never reaches into client/ config.
"""

import json
import os

from shared.bundle import Bundle

# The keys an agent may skin. Anything else stays global.
SKIN_KEYS = ("text_color", "font_size", "font_family", "banner")


def load_skin(agent: str, base: dict = None) -> dict:
    """The effective skin for ``agent``: ``base`` with the agent's bundle skin.json
    non-null values laid over it. Missing bundle/file/keys inherit ``base``."""
    eff = dict(base or {})
    path = Bundle(agent).skin_path
    try:
        with open(path) as f:
            skin = json.load(f)
    except Exception:
        return eff
    if isinstance(skin, dict):
        for k in SKIN_KEYS:
            if skin.get(k) is not None:
                eff[k] = skin[k]
    return eff


def write_skin(agent: str, skin: dict = None) -> str:
    """Write ``agent``'s skin.json (scaffolding a null/inherit template by default).
    Returns the path. Used when a bundle is created so the file is there to edit."""
    b = Bundle(agent)
    b.ensure()
    data = {k: None for k in SKIN_KEYS}          # null = inherit the global default
    if skin:
        data.update({k: skin[k] for k in SKIN_KEYS if k in skin})
    with open(b.skin_path, "w") as f:
        json.dump(data, f, indent=2)
    return b.skin_path

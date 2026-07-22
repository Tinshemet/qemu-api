"""
shortcuts — the REPL shortcut package.

Each module here (except base/context) defines a Shortcut subclass; importing them
fills ALL_SHORTCUTS, folded below into an ordered registry. handle_command() is the
dispatcher the chat loop calls before handing input to the AI: the first shortcut
that matches runs and the loop continues; no match → fall through to the model.
Adding a shortcut is one new file. Mirrors the chat/commands/ pattern.
"""

import importlib
import pkgutil
from typing import List

from .base import ALL_SHORTCUTS

# Import every shortcut module so its Shortcut subclass registers itself.
for _mod in pkgutil.iter_modules(__path__):
    if _mod.name not in ("base", "context"):
        importlib.import_module(f"{__name__}.{_mod.name}")

# Shortcut phrase sets are disjoint (distinct config entries / literal word sets),
# so registry order doesn't affect which one matches.
_REGISTRY = [cls() for cls in ALL_SHORTCUTS]


def handle_command(ui: str, messages: List[dict], runtime_drift_count: int,
                   verbose: bool) -> bool:
    """Handle a REPL shortcut if ``ui`` is one. Returns True when handled (the caller
    continues the REPL), False when the input isn't a shortcut (fall through to the AI).

    Example::

        handle_command("list", messages, 0, False)       # → True (ran list_vms)
        handle_command("make a vm", messages, 0, False)   # → False
    """
    for cmd in _REGISTRY:
        if cmd.matches(ui):
            cmd.run(ui, messages, runtime_drift_count, verbose)
            return True
    return False

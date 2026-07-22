"""
base.py — Shortcut base class + auto-registration for the REPL shortcut package.

A REPL shortcut is a fixed phrase the chat loop intercepts BEFORE the input falls
through to the AI (list / system / drift / verbose / loop-limit …). One Shortcut
subclass per file; declaring one registers it. Unlike a direct-CLI Command (which
dispatches on a verb + args), a Shortcut matches the whole input against a
config-driven phrase set and returns nothing — the dispatcher reports handled/not.
"""

from typing import List

# Every concrete Shortcut subclass appends itself here at class-definition time.
ALL_SHORTCUTS: list = []


class Shortcut:
    """One REPL shortcut. Simple shortcuts set ``config_key`` (a key into the
    ``shortcut_commands`` config block) and the base ``matches`` does membership;
    shortcuts with prefix/arg logic (list, loop-limit) or a literal word set
    (verbose) override ``matches``. All implement ``run``."""

    config_key: str = ""      # the shortcut_commands[...] set this matches, if simple

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        ALL_SHORTCUTS.append(cls)

    def matches(self, ui: str) -> bool:
        from . import context as ctx
        return bool(self.config_key) and ui in ctx.SHORTCUTS.get(self.config_key, [])

    def run(self, ui: str, messages: List[dict], runtime_drift_count: int,
            verbose: bool) -> None:
        """Perform the shortcut. ``ui`` is the raw input, ``messages`` the live
        session (mutated by clear-session), ``runtime_drift_count`` for the drift
        report, ``verbose`` the current debug-view flag."""
        raise NotImplementedError

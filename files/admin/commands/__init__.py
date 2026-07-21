"""
admin.commands — one class per admin command, auto-registered.

To add a command: drop a module in this package with a Command subclass that
sets `names` (and `needs_arg` if it requires an argument) and implements
run(args). It is auto-discovered here — no edit to this file needed — and, if
it should appear in the help overlay, add a matching entry to `help_sections`
in admin_config.defaults.json.
"""

import importlib
import pkgutil

from admin import state
from admin.commands.base import ALL_COMMANDS, Result

# Import every module in this package (except base) so each Command subclass
# registers itself via Command.__init_subclass__.
for _mod in pkgutil.iter_modules(__path__):
    if _mod.name != "base":
        importlib.import_module(f"{__name__}.{_mod.name}")

# verb -> command instance. Duplicate verbs are a programming error, not silent.
_REGISTRY = {}
for _cls in ALL_COMMANDS:
    _instance = _cls()
    for _name in _cls.names:
        if _name in _REGISTRY:
            raise RuntimeError(f"duplicate admin command verb: {_name!r}")
        _REGISTRY[_name] = _instance


def dispatch(cmd: str) -> None:
    """Parse and run one admin command; write the outcome to the shared status line.

    Runs on the dispatch worker thread (see keyboard.handle_key) — commands never
    touch curses, only shared Python state under state.lock, plus network / local-
    process calls.
    """
    if not cmd:
        return
    parts = cmd.split()
    verb  = parts[0].lower()
    args  = parts[1:]
    command = _REGISTRY.get(verb)

    try:
        if command is None or (command.needs_arg and not args):
            result = Result(f"unknown: {cmd}  (type 'help')")
        else:
            result = command.run(args)
    except Exception as e:
        result = Result(str(e)[:80])

    with state.lock:
        state.cmd_msg   = result.message
        state.help_mode = result.help_mode or state.help_mode

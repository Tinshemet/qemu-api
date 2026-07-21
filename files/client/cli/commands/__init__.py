"""
commands — the direct QEMU CLI (client-local), one class per sub-command.

Dispatches `gorgon <cmd>` directly to the local QEMU engine (via
shared.executioner.tool_executor) or to the configured server when QEMU isn't
installed locally. No AI involved.

Each command is a Command subclass in its own module here, auto-registered via
Command.__init_subclass__ + pkgutil discovery — adding a command is dropping a
file (set `names`, `min_args`, implement run(cmd, rest, verbose)). Shared
machinery lives in context.py; the flag handlers client_wrapper imports
(set_custom_mode_flag / fingerprint_report / clear_session_flag) are re-exported
below.
"""

import importlib
import pkgutil

from client.cli.commands.base import ALL_COMMANDS
from client.cli.commands.context import (
    _operator_gate_ok, console,
    set_custom_mode_flag, fingerprint_report, clear_session_flag,  # re-exported for client_wrapper
)

# Import every command module (not base/context) so each Command registers itself.
for _mod in pkgutil.iter_modules(__path__):
    if _mod.name not in ("base", "context"):
        importlib.import_module(f"{__name__}.{_mod.name}")

# verb -> command instance. Duplicate verbs are a programming error, not silent.
_REGISTRY = {}
for _cls in ALL_COMMANDS:
    _instance = _cls()
    for _name in _cls.names:
        if _name in _REGISTRY:
            raise RuntimeError(f"duplicate client command verb: {_name!r}")
        _REGISTRY[_name] = _instance


def run(args, verbose: bool = False) -> None:
    """Dispatch a direct ``gorgon <cmd>`` sub-command.

    Routes the first arg to its Command; commands whose min_args isn't met fall
    through to "unknown command" (preserving the old `cmd == "x" and rest`
    behaviour). ``verbose`` echoes the raw JSON result for each call.

    Example::

        run(["list"])                 # renders the VM table
        run(["launch", "myvm", "vnc"])
    """
    if not args:
        console.print("[dim]No command given. Try: list, launch, stop, status, profiles, system[/dim]")
        return

    cmd  = args[0]
    rest = args[1:]

    if not _operator_gate_ok(cmd):
        console.print("[bold red]Login required.[/bold red] Run [cyan]gorgon login[/cyan] first.")
        return

    command = _REGISTRY.get(cmd)
    if command is None or len(rest) < command.min_args:
        console.print(
            f"[bold yellow]Unknown command: {cmd}[/bold yellow]  "
            f"Run [bold]gorgon help[/bold] for usage."
        )
        return

    command.run(cmd, rest, verbose)

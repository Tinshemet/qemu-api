"""
admin/commands/base.py — the Command contract.

One admin command = one Command subclass in its own module under
admin/commands/. Set `names` (the verb(s) it answers to) and, if it requires an
argument, `needs_arg = True`, then implement run(args) -> Result. Subclasses are
auto-registered (see admin/commands/__init__.py) simply by existing, so adding a
command is: drop a file. No registry edit needed.
"""

from dataclasses import dataclass

# Every concrete Command subclass appends itself here as it's defined.
ALL_COMMANDS = []


@dataclass
class Result:
    """What a command produces: a status-line message and/or a mode switch."""
    message: str = ""
    help_mode: bool = False


class Command:
    names: tuple = ()          # verb(s) this command answers to (empty = abstract, not registered)
    needs_arg: bool = False    # require at least one argument, else the input is treated as unknown

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.names:          # only register concrete commands that claim a verb
            ALL_COMMANDS.append(cls)

    def run(self, args: list) -> Result:
        """Execute the command. `args` are the whitespace-split tokens after the
        verb (args[0] is the first argument). Return a Result."""
        raise NotImplementedError

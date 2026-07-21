"""
commands/base.py — the direct-CLI Command contract.

One command = one Command subclass in its own module. Set `names` (the verb(s)
it answers to) and `min_args` (how many arguments it needs to even match — fewer
falls through to "unknown command", preserving the old `cmd == "x" and rest`
behaviour). Implement run(cmd, rest, verbose). Subclasses auto-register (see
__init__.py) just by existing — adding a command is dropping a file.
"""

# Every concrete Command subclass appends itself here as it is defined.
ALL_COMMANDS = []


class Command:
    names: tuple = ()      # verb(s) this command answers to (empty = abstract, not registered)
    min_args: int = 0      # minimum args after the verb to match, else -> unknown command

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.names:
            ALL_COMMANDS.append(cls)

    def run(self, cmd: str, rest: list, verbose: bool) -> None:
        """Execute the command. `cmd` is the verb that matched (commands with
        several names, like mission/run, branch on it); `rest` is the args after
        the verb; `verbose` echoes raw JSON via context.pp."""
        raise NotImplementedError

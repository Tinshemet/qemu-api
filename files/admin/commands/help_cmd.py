"""admin/commands/help_cmd.py — open the help overlay."""

from admin.commands.base import Command, Result


class HelpCommand(Command):
    names = ("help",)

    def run(self, args: list) -> Result:
        return Result(help_mode=True)

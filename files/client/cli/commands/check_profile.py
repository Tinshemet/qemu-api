"""commands/check_profile.py — gorgon check-profile <profile>."""

from client.cli.commands.base import Command
from client.cli.commands.context import check_profile_compatibility, render_compat


class CheckProfileCommand(Command):
    names = ("check-profile",)
    min_args = 1

    def run(self, cmd, rest, verbose):
        render_compat(check_profile_compatibility(rest[0]))

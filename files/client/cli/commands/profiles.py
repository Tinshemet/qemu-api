"""commands/profiles.py — gorgon profiles."""

from client.cli.commands.base import Command
from client.cli.commands.context import list_profiles, render_profiles


class ProfilesCommand(Command):
    names = ("profiles",)

    def run(self, cmd, rest, verbose):
        render_profiles(list_profiles())

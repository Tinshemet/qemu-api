"""commands/system.py — gorgon system (capabilities + OVMF)."""

from client.cli.commands.base import Command
from client.cli.commands.context import OVMF, check_system_capabilities, render_system


class SystemCommand(Command):
    names = ("system",)

    def run(self, cmd, rest, verbose):
        caps = check_system_capabilities()
        caps["ovmf_paths"] = OVMF
        render_system(caps)

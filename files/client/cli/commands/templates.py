"""commands/templates.py — gorgon templates."""

from client.cli.commands.base import Command
from client.cli.commands.context import _require_manager, manager, render_templates


class TemplatesCommand(Command):
    names = ("templates",)

    def run(self, cmd, rest, verbose):
        _require_manager()
        render_templates(manager.list_templates())

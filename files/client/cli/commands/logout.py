"""commands/logout.py — gorgon logout."""

from client.cli.commands.base import Command
from client.cli.commands.context import _auth_sessions, console


class LogoutCommand(Command):
    names = ("logout",)

    def run(self, cmd, rest, verbose):
        if _auth_sessions is not None:
            _auth_sessions.invalidate_session(_auth_sessions.read_current_session())
            _auth_sessions.clear_current_session()
        console.print("[dim]Logged out.[/dim]")

"""commands/login.py — gorgon login [username] (creates the first operator, else authenticates)."""

import getpass

from client.cli.commands.base import Command
from client.cli.commands.context import _auth_sessions, _auth_store, console


class LoginCommand(Command):
    names = ("login",)

    def run(self, cmd, rest, verbose):
        if _auth_store is None:
            console.print("[bold red]Auth package unavailable on this checkout.[/bold red]")
            return
        username = rest[0] if rest else None
        if not _auth_store.operators_exist():
            console.print("[bold cyan]No operator account exists yet — creating the first one.[/bold cyan]")
            username = username or console.input("Username: ").strip()
            while True:
                pw1 = getpass.getpass("Password: ")
                pw2 = getpass.getpass("Confirm password: ")
                if pw1 != pw2:
                    console.print("[red]Passwords didn't match — try again.[/red]")
                    continue
                if len(pw1) < 8:
                    console.print("[red]Password must be at least 8 characters.[/red]")
                    continue
                break
            r = _auth_store.create_operator(username, pw1)
            if not r.get("success"):
                console.print(f"[bold red]{r.get('error')}[/bold red]")
                return
            password = pw1
        else:
            username = username or console.input("Username: ").strip()
            password = getpass.getpass("Password: ")
        if not _auth_store.verify_password(username, password):
            console.print("[bold red]Invalid username or password.[/bold red]")
            return
        token = _auth_sessions.create_session(username)
        _auth_sessions.write_current_session(token)
        console.print(f"[bold green]Logged in as '{username}'.[/bold green]")

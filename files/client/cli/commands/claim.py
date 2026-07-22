"""commands/claim.py — gorgon claim [list] | confirm <fact> | reject <fact>."""

from client.cli.commands.base import Command
from client.cli.commands.context import _auth_sessions, _require_operator_password, console


class ClaimCommand(Command):
    names = ("claim",)

    def run(self, cmd, rest, verbose):
        # gorgon claim [list] | confirm <fact> | reject <fact>
        # An unverifiable claim (a fact no read-only probe can confirm) is parked as
        # PENDING and can't close a goal until a human vouches for it. Confirming or
        # rejecting one asserts/retracts truth the AI will ACT on — high-impact, so
        # it's operator-gated (same bar as switching the agent or forging a contract).
        from orchestrator.ai.planner import findings_store as _store
        from orchestrator.ai.agent.contract import active_agent_key as _agent_key
        from shared import audit as _audit
        _op  = _auth_sessions.current_username() if _auth_sessions else None
        _key = _agent_key()
        sub  = rest[0] if rest else "list"
        if sub == "list":
            data = _store.listing(_key)
            pend, ver = data["pending"], data["verified"]
            console.print(f"[bold]Claims for agent [cyan]{_key}[/cyan][/bold]")
            if not pend and not ver:
                console.print("[dim]  none — no claims recorded yet.[/dim]")
            if pend:
                console.print("[yellow]  PENDING — awaiting confirmation (NOT usable until you confirm):[/yellow]")
                for e in pend:
                    console.print(f"    [yellow]{e['fact']}[/yellow] = {e['value']!r}")
                    console.print(f"        [dim]evidence: {e.get('evidence') or '—'}[/dim]")
            if ver:
                console.print("[green]  VERIFIED — you confirmed these; the AI may use them:[/green]")
                for e in ver:
                    console.print(f"    [green]{e['fact']}[/green] = {e['value']!r}")
            if pend:
                console.print("[dim]  Confirm: gorgon claim confirm '<fact>'   "
                              "Reject: gorgon claim reject '<fact>'[/dim]")
        elif sub in ("confirm", "reject") and len(rest) >= 2:
            fact = rest[1]
            action = "confirm a claim as true" if sub == "confirm" else "reject a claim"
            if not _require_operator_password(action):
                return
            ok = _store.confirm(_key, fact) if sub == "confirm" else _store.reject(_key, fact)
            if not ok:
                console.print(f"[bold red]No {'pending ' if sub == 'confirm' else ''}claim "
                              f"'{fact}' for agent {_key}.[/bold red] "
                              f"Run [cyan]gorgon claim list[/cyan] for the exact fact key.")
                return
            _audit.record(f"claim.{sub}", f"{_key}:{fact}", _op)
            if sub == "confirm":
                console.print(f"[green]Confirmed [bold]{fact}[/bold] — the AI may now use it "
                              f"to close goals (and future runs inherit it).[/green]")
            else:
                console.print(f"[green]Rejected [bold]{fact}[/bold] — dropped from the store.[/green]")
        else:
            console.print("[yellow]Usage: gorgon claim [list] | confirm '<fact>' | reject '<fact>'[/yellow]")

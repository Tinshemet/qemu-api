"""commands/agent.py — gorgon agent | agent <file> | agent load <file> | agent reset."""

import os

import requests

from client import config as _cfg
from client.cli.commands.base import Command
from client.cli.commands.context import (
    _auth_sessions, _HEADERS, _require_operator_password, _SERVER, _VERIFY, console,
)


class AgentCommand(Command):
    names = ("agent",)

    def run(self, cmd, rest, verbose):
        # gorgon agent | agent <file> | agent load <file> | agent reset
        # Switching the active agent is HIGH-IMPACT: it swaps the whole contract
        # (persona, toolkit, red lines, kill-switch). Guarded by (1) the active
        # contract's blacklist — an agent under a locked contract can't switch
        # itself out — and (2) operator re-authentication. The client never
        # restarts the server; a change takes effect when the operator reboots it.
        import shared.bundle as _bundle
        from shared import agent_select as _sel
        from shared import audit as _audit
        from orchestrator.ai.agent import forge as _forge
        from orchestrator.ai.agent import AGENT_DIR as _agent_dir   # code-resident templates
        _resolve  = lambda f: _bundle.resolve_grgn(f, _agent_dir)   # bundle-first, code fallback
        _op       = _auth_sessions.current_username() if _auth_sessions else None

        def _validate(f: str):
            p = _resolve(f)
            if not os.path.isfile(p):
                return f"no such agent file: {f}"
            try:
                from shared.grgn_sign import read as _read_grgn
                g, st = _read_grgn(p)              # decrypts / verifies either format
            except Exception as e:
                return f"{f} could not be read: {e}"
            if st in ("tampered", "missing") or g is None:
                return f"{f} failed its integrity check ({st}) — refusing to select it"
            if not (isinstance(g, dict) and "contract" in g and "persona" in g):
                return f"{f} is not a .grgn agent (missing contract/persona)"
            return None

        def _change_allowed() -> bool:
            # (1) the active contract may forbid agent-switching entirely
            try:
                from orchestrator.ai.agent.contract import is_forbidden
                if is_forbidden("switch_agent"):
                    console.print("[bold red]The active contract forbids switching agents "
                                  "(switch_agent is blacklisted).[/bold red]")
                    return False
            except Exception:
                pass  # contract layer unavailable — fall through to the auth gate
            # (2) operator re-authentication
            return _require_operator_password("switch the active agent")

        def _persist(f: str) -> None:
            _sel.set_selection(f if os.path.isabs(f) else os.path.basename(_resolve(f)))

        sub = rest[0] if rest else ""
        if not sub:
            cur = os.environ.get("GORGON_AGENT") or _sel.get_selection() or "doorman.grgn (default)"
            console.print(f"[bold]Active agent:[/bold] {cur}")
            console.print("[dim]Available (integrity):[/dim]")
            try:
                from shared.grgn_sign import status as _grgn_status
            except Exception:
                _grgn_status = lambda p: "unknown"
            _colors = {"encrypted": "green", "signed": "green",
                       "unsigned": "yellow", "tampered": "bold red"}
            for p in _bundle.list_agent_grgns(_agent_dir):   # bundle contracts + code templates
                st = _grgn_status(p)
                c = _colors.get(st, "dim")
                console.print(f"  [{c}]{os.path.basename(p):<22} {st}[/{c}]")
            if os.environ.get("GORGON_AGENT"):
                console.print("[dim](GORGON_AGENT env var is set — it overrides the saved selection.)[/dim]")
        elif sub == "reset":
            if not _change_allowed():
                return
            _sel.clear_selection()
            _audit.record("agent.reset", "doorman.grgn", _op)
            console.print("[green]Agent reset — doorman.grgn on next server boot.[/green]")
            console.print("[yellow]Restart the orchestrator server to apply.[/yellow]")
        elif sub == "load":
            if len(rest) < 2:
                console.print("[yellow]Usage: gorgon agent load <file>[/yellow]")
                return
            f = rest[1]
            err = _validate(f)
            if err:
                console.print(f"[bold red]{err}[/bold red]")
                return
            if not _change_allowed():
                return
            _persist(f)
            _audit.record("agent.load", os.path.basename(_resolve(f)), _op)
            # Operator access is required to reach here, so the client is allowed
            # to bounce the server — the respawn re-imports the contract and picks
            # up the new selection.
            try:
                from shared.grgn_sign import read as _read_grgn
                _g_agent, _ = _read_grgn(_resolve(f))       # encrypted or plaintext
                _persona = ((_g_agent or {}).get("persona") or {}).get("name")
            except Exception:
                _persona = None
            _label = _persona or os.path.basename(_resolve(f))
            console.print(f"\n[bold cyan]Loading agent “{_label}”[/bold cyan] … "
                          "restarting the orchestrator server.")
            from shared import server_control as _srv
            pid = _srv.restart_server()
            if pid:
                console.print(f"[green]✔ Server back up (pid {pid}) — “{_label}” is now the active agent.[/green]")
                # Surface any load-time drift for the freshly-loaded agent.
                try:
                    _info = requests.get(f"{_SERVER}/info", headers=_HEADERS,
                                         timeout=_cfg.REQUEST_TIMEOUT_S, verify=_VERIFY).json()
                    for _w in _info.get("agent_warnings", []):
                        console.print(f"[yellow]  ⚠ {_w}[/yellow]")
                except Exception:
                    pass  # server still settling — warnings are also in the server log
                console.print("[dim]Reopen the CLI in a few seconds to reconnect.[/dim]")
            else:
                console.print("[bold red]✖ Server did not come back up — check "
                              f"{os.environ.get('GORGON_SERVER_LOG', _cfg.LOG_PATH)}.[/bold red]")
                console.print("[dim]The selection is saved; start the server manually to apply it.[/dim]")
        elif sub not in ("load", "reset"):
            f = sub
            err = _validate(f)
            if err:
                console.print(f"[bold red]{err}[/bold red]")
                return
            if not _change_allowed():
                return
            _persist(f)
            _audit.record("agent.select", os.path.basename(_resolve(f)), _op)
            console.print(f"[green]Agent set to {f} — active on next server boot.[/green] "
                          f"Run [cyan]gorgon agent load {f}[/cyan] for the apply-now steps.")
        else:
            console.print("[yellow]Usage: gorgon agent | agent <file> | agent load <file> | agent reset[/yellow]")

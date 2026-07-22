"""commands/mission.py — gorgon mission new|list|show|run|edit|delete|"<goal>"  (and the `run` alias)."""

import json
import os

from client.cli.commands.base import Command
from client.cli.commands.context import _auth_sessions, _require_operator_password, console, pp


class MissionCommand(Command):
    names = ("mission", "run")

    def run(self, cmd, rest, verbose):
        # A MISSION is what you task the active agent to do (the agent + contract come
        # from GORGON_AGENT / the saved selection). Subverbs author/inspect SEALED
        # missions; a bare goal runs an ephemeral (unsigned) one that inherits every
        # agent default. `run <goal>` is a hidden alias for the quick form.
        #   gorgon mission new                 author + seal a mission (wizard)
        #   gorgon mission list                list this agent's sealed missions
        #   gorgon mission show <name>         view a sealed mission
        #   gorgon mission run <name>          run a sealed mission
        #   gorgon mission "<goal>"            quick ad-hoc task (ephemeral)
        try:
            from orchestrator.ai.mission import mission as _M
            from orchestrator.ai.mission import mission_forge as _MF
        except ImportError:
            console.print("[bold red]Mission subsystem unavailable (orchestrator package not present).[/bold red]")
            return

        def _fire(goal: str, mission_obj=None) -> None:
            """Run the autonomous loop on a goal (optionally under a sealed mission)."""
            try:
                from orchestrator.ai.planner.autonomous import run_autonomous_live
            except ImportError:
                console.print("[bold red]Autonomous runner unavailable.[/bold red]")
                return
            console.print(f"[bold cyan]▶ Mission:[/bold cyan] {goal}")
            try:
                result = run_autonomous_live(goal, mission=mission_obj)
            except Exception as e:
                console.print(f"[bold red]Mission failed: {e}[/bold red]  "
                              "[dim](is Ollama + the executor running?)[/dim]")
                return
            s = result.get("summary", {}) or {}
            mark = "[green]✔[/green]" if result.get("ok") else "[yellow]✖[/yellow]"
            console.print(f"\n{mark} status={s.get('status')}  executed={s.get('executed')}  "
                          f"unverified={s.get('unverified')}  halted={s.get('halted')}  aborted={s.get('aborted')}")
            econ = result.get("economics")
            if econ:
                console.print(f"[dim]economics: {econ}[/dim]")
            review = result.get("claims_for_review") or []
            if review:
                console.print(f"[yellow]⚠ {len(review)} claim(s) need your confirmation "
                              f"(not usable until you confirm):[/yellow]")
                for c in review:
                    console.print(f"    [yellow]{c['fact']}[/yellow] = {c['value']!r}")
                    console.print(f"        [dim]evidence: {c.get('evidence') or '—'}[/dim]")
                console.print("[dim]    Review: gorgon claim list   "
                              "Confirm: gorgon claim confirm '<fact>'[/dim]")
            pp(result, verbose)

        sub = rest[0] if (cmd == "mission" and rest) else ""

        if sub == "new":
            if not _require_operator_password("author a mission"):
                return
            path = _MF.forge_mission_interactive(
                ask=lambda p: console.input(f"[bold cyan]{p}:[/bold cyan] ").strip(),
                out=console.print,
            )
            if path:
                from shared import audit as _audit
                _op = _auth_sessions.current_username() if _auth_sessions else None
                _audit.record("mission.new", os.path.basename(path), _op)
        elif sub == "list":
            from orchestrator.ai.agent.contract import active_agent_key as _agent_key
            ms = _M.list_missions()
            console.print(f"[bold]Missions for agent [cyan]{_agent_key()}[/cyan][/bold]")
            if not ms:
                console.print("[dim]  none — author one with: gorgon mission new[/dim]")
            for m in ms:
                col = "green" if m["status"] in ("encrypted", "signed") else "yellow"
                console.print(f"  [{col}]{m['name']:<24}[/{col}] {m['title']}  "
                              f"[dim]({m['status']})[/dim]")
                console.print(f"      [dim]{m['goal']}[/dim]")
        elif sub == "show" and len(rest) >= 2:
            m, status = _M.load(rest[1])
            if not m:
                console.print(f"[bold red]No mission '{rest[1]}' ({status}).[/bold red] "
                              f"Run [cyan]gorgon mission list[/cyan].")
                return
            console.print(_M.render(m))
            console.print(f"[dim]  integrity: {status}[/dim]")
        elif sub == "run" and len(rest) >= 2:
            m, status = _M.load(rest[1])
            if not m:
                console.print(f"[bold red]No mission '{rest[1]}' ({status}).[/bold red] "
                              f"Run [cyan]gorgon mission list[/cyan].")
                return
            _fire(m.goal, mission_obj=m)
        elif sub == "delete" and len(rest) >= 2:
            if not _require_operator_password("delete a mission"):
                return
            if _M.delete(rest[1]):
                from shared import audit as _audit
                _audit.record("mission.delete", rest[1], _auth_sessions.current_username() if _auth_sessions else None)
                console.print(f"[green]Deleted mission [bold]{rest[1]}[/bold].[/green]")
            else:
                console.print(f"[yellow]No mission '{rest[1]}' to delete.[/yellow]")
        elif sub == "edit" and len(rest) >= 2:
            # Vault-style edit: decrypt → $EDITOR → re-validate → re-encrypt (mirrors
            # `contract edit`). Sealed missions are encrypted, so no hand-editing on disk.
            if not _require_operator_password("edit a mission"):
                return
            m, status = _M.load(rest[1])
            if not m:
                console.print(f"[bold red]No mission '{rest[1]}' ({status}).[/bold red]")
                return
            import subprocess, tempfile
            from shared import audit as _audit
            editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
            fd, tmp = tempfile.mkstemp(suffix=".mission.json"); os.close(fd); os.chmod(tmp, 0o600)
            try:
                with open(tmp, "w") as f:
                    json.dump(m.to_spec(), f, indent=2, ensure_ascii=False)
                subprocess.call([editor, tmp])
                with open(tmp) as f:
                    try:
                        edited = json.load(f)
                    except Exception as e:
                        console.print(f"[bold red]Invalid JSON — not saved: {e}[/bold red]")
                        edited = None
                if edited is not None:
                    issues = _M.validate(edited)
                    if issues:
                        console.print("[bold red]Refusing to save — mission incomplete:[/bold red]")
                        for i in issues:
                            console.print(f"  - {i}")
                    else:
                        path = _M.save(_M.prune(edited))
                        new_name = os.path.splitext(os.path.basename(path))[0]
                        if new_name != rest[1]:           # title changed → drop the old file
                            _M.delete(rest[1])
                        console.print(f"[green]✔ Saved and re-encrypted → {path}[/green]")
                        _audit.record("mission.edit", new_name, _auth_sessions.current_username() if _auth_sessions else None)
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        elif cmd == "run" and rest:
            _fire(" ".join(rest), mission_obj=_M.Mission.ephemeral(" ".join(rest)))
        elif sub in ("show", "run", "delete", "edit"):
            console.print(f"[yellow]Usage: gorgon mission {sub} <name>[/yellow]")
        elif rest:                                   # bare goal → quick ephemeral task
            goal = " ".join(rest)
            _fire(goal, mission_obj=_M.Mission.ephemeral(goal))
        else:
            console.print("[yellow]Usage: gorgon mission new | list | show <name> | "
                          "run <name> | edit <name> | delete <name> | \"<goal>\"[/yellow]")

"""commands/contract.py — gorgon contract forge|show|list|sign|edit|void|restore|audit."""

import json
import os

from client.cli.commands.base import Command
from client.cli.commands.context import _auth_sessions, _require_operator_password, console


class ContractCommand(Command):
    names = ("contract",)

    def run(self, cmd, rest, verbose):
        # gorgon contract forge [--full] | show <file> | sign <file> <safeword>
        # Forging is a deliberate, coherence-gated CLI act. The plain `forge`
        # asks only the essential fields (name / goal / toolkit / done-when) and
        # defaults the rest; `--full` walks every field in forge_fields.json.
        try:
            from orchestrator.ai import forge as _forge
        except ImportError:
            console.print("[bold red]Contract forging unavailable on this checkout "
                          "(orchestrator package not present).[/bold red]")
            return
        _agent_dir = os.path.dirname(os.path.abspath(_forge.__file__))
        from shared import audit as _audit
        _op = _auth_sessions.current_username() if _auth_sessions else None
        sub = rest[0] if rest else ""
        if sub == "forge":
            if not _require_operator_password("forge a contract"):
                return
            _full = "--full" in rest
            _p = _forge.forge_interactive(
                ask=lambda p: console.input(f"[bold cyan]{p}:[/bold cyan] ").strip(),
                out=console.print, write_dir=_agent_dir, essential_only=not _full)
            if _p:
                _audit.record("contract.forge", os.path.basename(_p), _op)
        elif sub == "show":
            from shared.grgn_sign import read as _read_grgn
            from shared import agent_select as _sel
            target = rest[1] if len(rest) >= 2 else (          # no arg → the active agent
                os.environ.get("GORGON_AGENT") or _sel.get_selection() or "doorman.grgn")
            path = target if os.path.isabs(target) else os.path.join(_agent_dir, target)
            g, st = _read_grgn(path)
            if g is None:
                console.print(f"[bold red]Cannot read {os.path.basename(target)} ({st}).[/bold red]")
            else:
                console.print(_forge.render(g))
                console.print(f"[dim]{os.path.basename(path)} · integrity: {st}[/dim]")
        elif sub == "list":
            import glob as _glob
            from shared.grgn_sign import read as _read_grgn
            from shared import agent_select as _sel
            active = os.path.basename(os.environ.get("GORGON_AGENT") or _sel.get_selection() or "doorman.grgn")
            files = sorted(_glob.glob(os.path.join(_agent_dir, "*.grgn")))
            if not files:
                console.print("[dim]No contracts found.[/dim]")
            from orchestrator.ai import revocation as _rev
            for p in files:
                name = os.path.basename(p)
                g, st = _read_grgn(p)
                con = (g or {}).get("contract", {}) or {}
                signed = "signed" if con.get("signed") else "unsigned"
                role = ((g or {}).get("persona", {}).get("role") or "—")[:40]
                mark = "[green]→[/green]" if name == active else " "
                void = "  [red]VOID[/red]" if _rev.is_voided(os.path.splitext(name)[0]) else ""
                console.print(f" {mark} {name:<24} {st:<9} {signed:<8} "
                              f"exp:{con.get('expiry') or 'never':<10} {role}{void}")
        elif sub == "audit":
            for line in _audit.tail(30) or ["(no audit entries yet)"]:
                console.print(f"  {line}")
        elif sub == "sign" and len(rest) >= 3:
            if not _require_operator_password("sign a contract"):
                return
            from shared.grgn_sign import read as _read_grgn
            path = rest[1] if os.path.isabs(rest[1]) else os.path.join(_agent_dir, rest[1])
            g, st = _read_grgn(path)
            if g is None:
                console.print(f"[bold red]Cannot read {rest[1]} ({st}).[/bold red]")
            else:
                try:
                    _forge.sign(g, rest[2]); _forge.write_grgn(g, path)
                    console.print(f"[green]Signed → {path}[/green]")
                    _audit.record("contract.sign", os.path.basename(path), _op)
                except ValueError as e:
                    console.print(f"[bold red]{e}[/bold red]")
        elif sub == "edit" and len(rest) >= 2:
            # Encrypted .grgn can't be hand-edited, so decrypt to a temp file,
            # open $EDITOR, re-review, and re-encrypt on save (ansible-vault style).
            if not _require_operator_password("edit a contract"):
                return
            import subprocess, tempfile
            from shared.grgn_sign import read as _read_grgn
            path = rest[1] if os.path.isabs(rest[1]) else os.path.join(_agent_dir, rest[1])
            g, st = _read_grgn(path)
            if g is None:
                console.print(f"[bold red]Cannot read {rest[1]} ({st}).[/bold red]")
            else:
                editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
                fd, tmp = tempfile.mkstemp(suffix=".grgn.json")
                os.close(fd); os.chmod(tmp, 0o600)      # decrypted plaintext — keep it private
                try:
                    with open(tmp, "w") as f:
                        json.dump(g, f, indent=2, ensure_ascii=False)
                    subprocess.call([editor, tmp])
                    with open(tmp) as f:
                        try:
                            edited = json.load(f)
                        except Exception as e:
                            console.print(f"[bold red]Invalid JSON — not saved: {e}[/bold red]")
                            edited = None
                    if edited is not None:
                        issues = _forge.review(edited)
                        if issues:
                            console.print("[bold red]Refusing to save — contract is incoherent:[/bold red]")
                            for i in issues:
                                console.print(f"  - {i}")
                        else:
                            _forge.write_grgn(edited, path)   # re-encrypts under the install key
                            console.print(f"[green]✔ Saved and re-encrypted → {path}[/green]")
                            _audit.record("contract.edit", os.path.basename(path), _op)
                finally:
                    try:
                        os.remove(tmp)                  # never leave decrypted content around
                    except OSError:
                        pass
        elif sub == "void" and len(rest) >= 2:
            # Voiding an agent revokes its existence — it AND all its missions are
            # disabled (the void cascade). High-impact → operator-gated.
            if not _require_operator_password("void an agent"):
                return
            from orchestrator.ai import revocation as _rev
            key = os.path.splitext(os.path.basename(rest[1]))[0]
            if _rev.void(key):
                _audit.record("contract.void", key, _op)
                console.print(f"[green]Voided agent [bold]{key}[/bold] — it and all its missions are disabled.[/green]")
                console.print(f"[dim]  Restore with: gorgon contract restore {key}[/dim]")
            else:
                console.print(f"[yellow]Could not void '{key}' — already voided, or protected "
                              f"(doorman is the fallback and can't be voided).[/yellow]")
        elif sub == "restore" and len(rest) >= 2:
            if not _require_operator_password("restore an agent"):
                return
            from orchestrator.ai import revocation as _rev
            key = os.path.splitext(os.path.basename(rest[1]))[0]
            if _rev.restore(key):
                _audit.record("contract.restore", key, _op)
                console.print(f"[green]Restored agent [bold]{key}[/bold] — it and its missions are enabled again.[/green]")
            else:
                console.print(f"[yellow]'{key}' was not voided.[/yellow]")
        else:
            console.print("[yellow]Usage: gorgon contract "
                          "forge [--full] | show [file] | list | sign <file> <safeword> "
                          "| edit <file> | void <agent> | restore <agent> | audit[/yellow]")

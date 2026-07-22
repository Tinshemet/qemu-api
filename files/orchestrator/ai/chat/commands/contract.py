"""contract — forge / show / sign a .grgn agent contract."""

import os
from typing import List

from .base import Command
from . import context as ctx


class ContractCommand(Command):
    names = ("contract",)

    def run(self, cmd: str, rest: List[str], verbose: bool) -> None:
        # gorgon contract forge | show <file> | sign <file> <safeword>
        from orchestrator.ai.agent import forge as _forge
        from orchestrator.ai.agent import contract as _contract
        import shared.bundle as _bundle
        _agent_dir = os.path.dirname(os.path.abspath(_forge.__file__))   # code-resident templates
        sub = rest[0] if rest else ""
        if sub == "forge":
            _forge.forge_interactive(
                ask=lambda p: ctx.console.input(f"[bold cyan]{p}:[/bold cyan] ").strip(),
                out=ctx.console.print, write_dir=_bundle.AGENTS_ROOT)
        elif sub == "show" and len(rest) >= 2:
            from shared.grgn_sign import read as _read_grgn
            path = _contract.agent_grgn_path(rest[1], _agent_dir)   # bundle-first, code fallback
            g, st = _read_grgn(path)
            if g is None:
                ctx.console.print(f"[error]Cannot read {rest[1]} ({st}).[/error]")
            else:
                ctx.console.print(_forge.render(g))
                ctx.console.print(f"[dim]integrity: {st}[/dim]")
        elif sub == "sign" and len(rest) >= 3:
            from shared.grgn_sign import read as _read_grgn
            path = _contract.agent_grgn_path(rest[1], _agent_dir)
            g, st = _read_grgn(path)
            if g is None:
                ctx.console.print(f"[error]Cannot read {rest[1]} ({st}).[/error]")
            else:
                try:
                    _forge.sign(g, rest[2]); _forge.write_grgn(g, path)
                    ctx.console.print(f"[success]Signed → {path}[/success]")
                except ValueError as e:
                    ctx.console.print(f"[error]{e}[/error]")
        else:
            ctx.console.print("[yellow]Usage: gorgon contract forge | show <file> | sign <file> <safeword>[/yellow]")

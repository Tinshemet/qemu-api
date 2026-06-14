"""
tests/renderer.py — Rich console rendering for test results.
"""

import json
from typing import Dict, List

from .shared import (
    TestResult,
    console,
    Table, Panel, box,
    OLLAMA_MODEL,
)

LAYER_NAMES   = {1:"Sanitiser", 2:"Executor", 3:"AI Integration",
                 4:"Profile + HTTP", 5:"Property-Based", 6:"Input Pipeline"}
LAYER_COLOURS = {1:"green", 2:"cyan", 3:"magenta", 4:"yellow", 5:"blue", 6:"white"}


def render_layer_results(results: List[TestResult], layer: int, verbose: bool = False):
    lr = [r for r in results if r.layer == layer]
    if not lr:
        return
    passed = sum(1 for r in lr if r.passed)
    total  = len(lr)
    sc     = "green" if passed==total else "yellow" if passed > total//2 else "red"
    console.print(Panel(
        f"[bold]{passed}/{total} passed[/bold]  "
        + ("[green]✓ All passing[/green]" if passed==total
           else f"[red]{total-passed} failing[/red]"),
        title=f"[bold {LAYER_COLOURS[layer]}]Layer {layer} — {LAYER_NAMES[layer]}[/bold {LAYER_COLOURS[layer]}]",
        border_style=sc,
    ))
    t = Table(box=box.SIMPLE_HEAVY, border_style=LAYER_COLOURS[layer],
              header_style=f"bold {LAYER_COLOURS[layer]}", show_lines=False)
    t.add_column("Test ID", style="bold white", width=36)
    t.add_column("Result", justify="center", width=8)
    t.add_column("Fixes", style="yellow", width=8)
    t.add_column("Time", justify="right", width=7, style="dim")
    t.add_column("Issue", style="red")
    for r in lr:
        rs  = "[green]✓[/green]" if r.passed else "[red]✗[/red]"
        fs  = str(len(r.fixes_applied)) if r.fixes_applied else "—"
        iss = r.issues[0][:70] if r.issues else "—"
        t.add_row(r.test_id, rs, fs, f"{r.duration_s:.1f}s", iss)
    console.print(t)
    for r in lr:
        if r.passed:
            continue
        lines = [f"  [red]✗[/red] {i}" for i in r.issues]
        if r.fixes_applied:
            lines += ["  [yellow]Fixes:[/yellow]"] + \
                     [f"    [yellow]→[/yellow] {f}" for f in r.fixes_applied[:4]]
        if verbose and r.detail:
            if "original" in r.detail:
                lines.append(f"  [dim]In:  {json.dumps(r.detail['original'],default=str)[:180]}[/dim]")
            if "sanitised" in r.detail and isinstance(r.detail["sanitised"], dict):
                lines.append(f"  [dim]Out: {json.dumps(r.detail['sanitised'],default=str)[:180]}[/dim]")
            if "all_messages" in r.detail:
                lines.append(f"  [dim]Issues: {r.detail['all_messages'][:200]}[/dim]")
            if "prompt_used" in r.detail:
                lines.append(f"  [dim]Prompt: {r.detail['prompt_used']}[/dim]")
        console.print(Panel("\n".join(lines),
                             title=f"[red]✗ {r.test_id}[/red]", border_style="red"))
    console.print()


def render_summary(results: List[TestResult]):
    passed    = sum(1 for r in results if r.passed)
    total     = len(results)
    all_fixes = [f for r in results for f in r.fixes_applied]
    colour    = "green" if passed==total else "yellow" if passed > total*0.8 else "red"
    console.print(Panel(
        f"[bold]Total: {passed}/{total} passed[/bold]  ·  "
        f"{len(all_fixes)} sanitiser/validator fixes applied",
        title="[bold]Overall Summary[/bold]", border_style=colour,
    ))


def render_benchmark(bm_results: Dict[str, List[TestResult]]):
    """Render side-by-side model comparison table."""
    models = list(bm_results.keys())
    t = Table(title="Model Benchmark — Layer 3", box=box.HEAVY_EDGE,
              header_style="bold cyan", show_lines=True)
    t.add_column("Test ID", style="bold white", width=28)
    for m in models:
        t.add_column(m, justify="center", width=14)

    all_ids = []
    for results in bm_results.values():
        for r in results:
            if r.test_id not in all_ids:
                all_ids.append(r.test_id)

    for tid in all_ids:
        row = [tid]
        for m in models:
            r = next((x for x in bm_results[m] if x.test_id == tid), None)
            if r is None:
                row.append("[dim]—[/dim]")
            elif r.passed:
                fixes = f" ({len(r.fixes_applied)}f)" if r.fixes_applied else ""
                row.append(f"[green]✓{fixes}[/green]\n[dim]{r.duration_s:.1f}s[/dim]")
            else:
                row.append(f"[red]✗[/red]\n[dim]{r.duration_s:.1f}s[/dim]")
        t.add_row(*row)

    summary_row = ["[bold]TOTAL[/bold]"]
    for m in models:
        results = bm_results[m]
        passed  = sum(1 for r in results if r.passed)
        total   = len(results)
        fixes   = sum(len(r.fixes_applied) for r in results)
        avg_t   = sum(r.duration_s for r in results) / total if total else 0
        colour  = "green" if passed==total else "yellow" if passed > total//2 else "red"
        summary_row.append(
            f"[{colour}]{passed}/{total}[/{colour}]\n"
            f"[dim]{fixes}f | {avg_t:.1f}s avg[/dim]"
        )
    t.add_row(*summary_row)
    console.print(t)

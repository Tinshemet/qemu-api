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
                 4:"Profile + HTTP", 5:"Property-Based", 6:"Input Pipeline",
                 7:"Context Assistant", 8:"Pipeline Probe",
                 9:"Gated Pipeline", 10:"Full Pipeline", 11:"Remote Split"}
LAYER_COLOURS = {1:"green", 2:"cyan", 3:"magenta", 4:"yellow", 5:"blue", 6:"white",
                 7:"bright_magenta", 8:"bright_cyan", 9:"bright_yellow", 10:"bright_green",
                 11:"bright_blue"}


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


def render_pipeline_table(results: List[TestResult]):
    """Per-tool breakdown table for layer 8 (pipeline probe)."""
    lr = [r for r in results if r.layer == 8]
    if not lr:
        return

    t = Table(
        title="[bold bright_cyan]Layer 8 — Per-Tool Breakdown[/bold bright_cyan]",
        box=box.SIMPLE_HEAVY, border_style="bright_cyan",
        header_style="bold bright_cyan", show_lines=True,
    )
    t.add_column("Tool",          style="cyan",   width=22)
    t.add_column("Category",      style="dim",    width=9)
    t.add_column("Expected Layer",style="yellow", width=14)
    t.add_column("Actual Layer",  style="yellow", width=14)
    t.add_column("Result",        justify="center", width=8)
    t.add_column("Error / Missing", style="red",  width=42)

    for r in lr:
        d        = r.detail
        tool     = d.get("tool", r.test_id)
        category = d.get("category", "—")
        exp_l    = d.get("expect_layer", "—") or "—"
        act_l    = d.get("actual_layer", "—") or "—"
        icon     = "[green]✓ PASS[/green]" if r.passed else "[red]✗ FAIL[/red]"

        # Error detail: prefer gate missing fields, then executor error, then issues
        missing  = d.get("missing", [])
        err      = d.get("error") or ""
        if missing:
            detail_str = f"gate blocked: {missing}"
        elif err:
            detail_str = err[:40]
        elif r.issues:
            detail_str = r.issues[0][:40]
        else:
            detail_str = "—"

        layer_colour = "green" if act_l == exp_l else "red"
        t.add_row(
            tool, category, exp_l,
            f"[{layer_colour}]{act_l}[/{layer_colour}]",
            icon, detail_str,
        )

    console.print(t)
    console.print()


def render_gated_table(results: List[TestResult]):
    """Per-test breakdown for Layer 9 (gate active). Highlights double-ask bugs."""
    lr = [r for r in results if r.layer == 9]
    if not lr:
        return

    t = Table(
        title="[bold bright_yellow]Layer 9 — Gated Pipeline Breakdown[/bold bright_yellow]",
        box=box.SIMPLE_HEAVY, border_style="bright_yellow",
        header_style="bold bright_yellow", show_lines=True,
    )
    t.add_column("Tool",        style="cyan",  width=20)
    t.add_column("Category",    style="dim",   width=12)
    t.add_column("Expected",    style="yellow",width=12)
    t.add_column("Actual",      style="yellow",width=12)
    t.add_column("Result",      justify="center", width=8)
    t.add_column("Missing / Error", style="red", width=44)

    for r in lr:
        d        = r.detail
        tool     = d.get("tool", r.test_id)
        category = d.get("category", "—")
        exp_l    = d.get("expect_layer") or "—"
        act_l    = d.get("actual_layer") or "—"
        icon     = "[green]✓ PASS[/green]" if r.passed else "[red]✗ FAIL[/red]"

        if d.get("double_ask"):
            icon = "[bold red]✗ DOUBLE-ASK[/bold red]"

        detail_str = "—"
        if d.get("double_ask"):
            detail_str = f"[bold red]gate asked for provided: {d.get('actual_missing')}[/bold red]"
        elif r.issues:
            detail_str = r.issues[0][:42]
        elif d.get("actual_missing"):
            detail_str = f"gate asked: {d['actual_missing']}"

        layer_colour = "green" if act_l == exp_l else ("red" if exp_l != "—" else "dim")
        t.add_row(tool, category, exp_l,
                  f"[{layer_colour}]{act_l}[/{layer_colour}]",
                  icon, detail_str)

    console.print(t)
    console.print()


def render_full_pipeline_table(results: List[TestResult]):
    """Per-test breakdown for Layer 10 (assistant + gate + executor)."""
    lr = [r for r in results if r.layer == 10]
    if not lr:
        return

    t = Table(
        title="[bold bright_green]Layer 10 — Full Pipeline Breakdown[/bold bright_green]",
        box=box.SIMPLE_HEAVY, border_style="bright_green",
        header_style="bold bright_green", show_lines=True,
    )
    t.add_column("Tool",       style="cyan",  width=18)
    t.add_column("Category",   style="dim",   width=10)
    t.add_column("Prompt",     style="white", width=28)
    t.add_column("Hint fired", justify="center", width=10)
    t.add_column("Gate fired", justify="center", width=10)
    t.add_column("Result",     justify="center", width=12)
    t.add_column("Issue",      style="red",   width=34)

    for r in lr:
        d         = r.detail
        tool      = d.get("tool", r.test_id)
        category  = d.get("category", "—")
        prompt    = (d.get("prompt") or "")[:26]
        hint_icon = "[yellow]hint[/yellow]"   if d.get("hint") else "[dim]—[/dim]"
        gate_icon = "[red]BLOCKED[/red]"      if d.get("gate_fired") else "[green]pass[/green]"

        if d.get("double_ask"):
            result_icon = "[bold red]✗ DOUBLE-ASK[/bold red]"
        elif r.passed:
            result_icon = "[green]✓ PASS[/green]"
        else:
            result_icon = "[red]✗ FAIL[/red]"

        issue_str = r.issues[0][:32] if r.issues else "—"

        t.add_row(tool, category, prompt, hint_icon, gate_icon, result_icon, issue_str)

    console.print(t)
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

"""
test_api.py — qemu-api Test Suite entry point (v5)
Five independent layers:
  LAYER 1 — Sanitiser:       pure unit tests, no AI, instant
  LAYER 2 — Executor:        unit tests against execute_tool/preflight, no AI
  LAYER 3 — AI Integration:  full AI tests with randomised prompts, needs Ollama
  LAYER 4 — Random Profiles: random profiles + preflight/HTTP validation
  LAYER 5 — Property-Based:  invariant checking with hypothesis

Usage:
  python3 test_api.py                      # all layers (5 random profiles)
  python3 test_api.py -l 1                 # sanitiser only (fast, no Ollama)
  python3 test_api.py -l 1,2              # no Ollama needed, ~2s
  python3 test_api.py -l 3                 # AI tests only
  python3 test_api.py -l 4 -n 20          # 20 random profiles
  python3 test_api.py -l 5                 # property tests (needs hypothesis)
  python3 test_api.py -l 4 -s 123         # seed 123 for reproducibility
  python3 test_api.py -t hallucination     # filter by tag
  python3 test_api.py -v                   # verbose
  python3 test_api.py --quick              # L1+L2+L5(low iter), skip L3
  python3 test_api.py --fuzz               # L5 with high iteration count
  python3 test_api.py --benchmark llama3.1 qwen2.5:7b mistral-nemo
"""

import json, os, sys
from datetime import datetime
from typing import Dict, List

from rich.panel import Panel
from rich.progress import track

# ── Layer imports ─────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

from tests.shared         import console, TestResult, OLLAMA_MODEL, OLLAMA_URL, _build_system_prompt
from tests.layer1_sanitizer import SANITISER_TESTS, run_sanitiser_test
from tests.layer2_executor  import (
    EXECUTOR_TESTS, run_executor_test,
    generate_random_preflight_tests,
)
from tests.layer3_ai        import (
    AI_TESTS, run_ai_test, call_ollama,
    _generate_ai_tests_from_profiles, _cleanup_random_ai_profiles,
)
from tests.layer4_profiles  import _generate_profile_tests, run_profile_test
from tests.layer5_property  import run_property_tests
from tests.layer6_context_gate import (
    GATE_TESTS, run_gate_test,
    generate_random_gate_tests,
)
from tests.renderer         import (
    LAYER_NAMES, render_layer_results, render_summary, render_benchmark,
)


def main():
    argv    = sys.argv[1:]
    verbose = "-v" in argv or "--verbose" in argv
    argv    = [a for a in argv if a not in ("-v","--verbose")]

    quick = "--quick" in argv
    fuzz  = "--fuzz"  in argv
    argv  = [a for a in argv if a not in ("--quick","--fuzz")]

    # ── Benchmark mode ────────────────────────────────────────────────────────
    if "--benchmark" in argv:
        idx = argv.index("--benchmark")
        bm_models = [a for a in argv[idx+1:] if not a.startswith("-") and not a.isdigit()]
        if not bm_models:
            bm_models = [OLLAMA_MODEL]
        bm_results: Dict[str, List[TestResult]] = {}
        sp   = _build_system_prompt()
        seed = 42
        for model in bm_models:
            console.print(f"\n[bold cyan]Benchmarking {model}...[/bold cyan]")
            model_results = []
            console.print(f"  [dim]Checking tool call format...[/dim]", end=" ")
            try:
                tcs, _ = call_ollama([
                    {"role":"system","content":"You are a VM assistant."},
                    {"role":"user",  "content":"list my vms"},
                ], model=model)
                fmt_ok = len(tcs) > 0
                console.print("[green]OK[/green]" if fmt_ok else "[red]no tool calls[/red]")
            except Exception as e:
                console.print(f"[red]ERROR: {e}[/red]")
                fmt_ok = False

            if fmt_ok:
                for tc in AI_TESTS:
                    r = run_ai_test(tc, sp, seed=seed, model=model)
                    model_results.append(r)
                    status = "[green]✓[/green]" if r.passed else "[red]✗[/red]"
                    console.print(f"    {status} {tc.id} [{r.duration_s:.1f}s]")
            bm_results[model] = model_results

        console.print()
        render_benchmark(bm_results)
        return

    # ── Layer filter ──────────────────────────────────────────────────────────
    run_layers = {1, 2, 5, 6} if quick else {1, 2, 3, 4, 5, 6}

    if "-l" in argv:
        idx = argv.index("-l")
        if idx+1 < len(argv):
            run_layers = {int(x) for x in argv[idx+1].split(",")}
            argv = argv[:idx] + argv[idx+2:]

    # ── Tag filter ────────────────────────────────────────────────────────────
    tag_filter = None
    if "-t" in argv:
        idx = argv.index("-t")
        if idx+1 < len(argv):
            tag_filter = argv[idx+1]
            argv = argv[:idx] + argv[idx+2:]

    # ── Random profile count ──────────────────────────────────────────────────
    n_random = 5
    if "-n" in argv:
        idx = argv.index("-n")
        if idx+1 < len(argv):
            try: n_random = int(argv[idx+1])
            except: pass
            argv = argv[:idx] + argv[idx+2:]

    # ── Seed ─────────────────────────────────────────────────────────────────
    seed = 42
    if "-s" in argv:
        idx = argv.index("-s")
        if idx+1 < len(argv):
            try: seed = int(argv[idx+1])
            except: pass
            argv = argv[:idx] + argv[idx+2:]

    prop_iters = 500 if fuzz else (20 if quick else 50)

    def tag_ok(tags): return tag_filter is None or tag_filter in tags

    san_tests  = [t for t in SANITISER_TESTS if tag_ok(t.tags)] if 1 in run_layers else []

    rand_pf_tests: list = []
    if 2 in run_layers and n_random > 0:
        rand_pf_tests = [t for t in generate_random_preflight_tests(n_random, seed)
                         if tag_ok(t.tags)]
    exec_tests = ([t for t in EXECUTOR_TESTS if tag_ok(t.tags)] + rand_pf_tests) \
                 if 2 in run_layers else []

    rand_ai_tests = []
    if 3 in run_layers and n_random > 0:
        rand_ai_tests = [t for t in _generate_ai_tests_from_profiles(n_random, seed)
                         if tag_ok(t.tags)]
    ai_tests = ([t for t in AI_TESTS if tag_ok(t.tags)] + rand_ai_tests) \
               if 3 in run_layers else []
    profile_tests = [t for t in _generate_profile_tests(n_random, seed)
                     if tag_ok(t.tags)] if 4 in run_layers else []
    run_props    = 5 in run_layers

    rand_gate_tests: list = []
    if 6 in run_layers and n_random > 0:
        rand_gate_tests = [t for t in generate_random_gate_tests(n_random * 4, seed)
                           if tag_ok(t.tags)]
    gate_tests = ([t for t in GATE_TESTS if tag_ok(t.tags)] + rand_gate_tests) \
                 if 6 in run_layers else []

    mode_str = "FUZZ" if fuzz else ("QUICK" if quick else "normal")
    console.print(Panel(
        f"[bold cyan]qemu-api Test Suite v5[/bold cyan]\n"
        f"Model: [bold]{OLLAMA_MODEL}[/bold]  |  {OLLAMA_URL}\n"
        f"Layers: {sorted(run_layers)}  "
        f"| L1={len(san_tests)} L2={len(EXECUTOR_TESTS)}+{len(rand_pf_tests)}r "
        f"L3={len(AI_TESTS)}+{len(rand_ai_tests)}dyn "
        f"L4={len(profile_tests)} L5={'yes' if run_props else 'no'} "
        f"L6={len(GATE_TESTS)}+{len(rand_gate_tests)}r\n"
        f"Seed: {seed}  |  Mode: {mode_str}"
        + (f"\nTag: [bold]{tag_filter}[/bold]" if tag_filter else ""),
        border_style="cyan", title="[bold]qemu-api[/bold]",
    ))

    all_results: List[TestResult] = []

    if san_tests:
        console.print(f"\n[bold green]Layer 1 — Sanitiser ({len(san_tests)})[/bold green]")
        for tc in track(san_tests, description="  Running..."):
            r = run_sanitiser_test(tc)
            all_results.append(r)
            console.print(f"    {'[green]✓[/green]' if r.passed else '[red]✗[/red]'} "
                           f"{tc.id} [{r.duration_s*1000:.0f}ms]")

    if exec_tests:
        console.print(f"\n[bold cyan]Layer 2 — Executor ({len(exec_tests)}) "
                       f"[dim]seed={seed}[/dim][/bold cyan]")
        for tc in track(exec_tests, description="  Running..."):
            r = run_executor_test(tc)
            all_results.append(r)
            console.print(f"    {'[green]✓[/green]' if r.passed else '[red]✗[/red]'} "
                           f"{tc.id} [{r.duration_s*1000:.0f}ms]")

    if ai_tests:
        console.print(f"\n[bold magenta]Layer 3 — AI Integration ({len(ai_tests)}) "
                       f"[dim]seed={seed}[/dim][/bold magenta]")
        sp = _build_system_prompt()
        for tc in track(ai_tests, description="  Running..."):
            console.print(f"    [dim]→ {tc.id}[/dim]", end=" ")
            r = run_ai_test(tc, sp, seed=seed)
            all_results.append(r)
            fs = f" [yellow]({len(r.fixes_applied)}f)[/yellow]" if r.fixes_applied else ""
            console.print(f"{'[green]✓[/green]' if r.passed else '[red]✗[/red]'}{fs} "
                           f"[{r.duration_s:.1f}s]")

    if rand_ai_tests:
        _cleanup_random_ai_profiles(rand_ai_tests)

    if profile_tests:
        console.print(f"\n[bold yellow]Layer 4 — Profile + HTTP ({len(profile_tests)}) "
                       f"[dim]seed={seed}[/dim][/bold yellow]")
        for tc in track(profile_tests, description="  Running..."):
            console.print(f"    [dim]→ {tc.id}[/dim]", end=" ")
            r = run_profile_test(tc)
            all_results.append(r)
            fs  = f" [yellow]({len(r.fixes_applied)}f)[/yellow]" if r.fixes_applied else ""
            ni  = r.detail.get("issue_count","?")
            console.print(f"{'[green]✓[/green]' if r.passed else '[red]✗[/red]'}{fs} "
                           f"[{r.duration_s:.1f}s] ({ni} issues)")

    if run_props:
        console.print(f"\n[bold blue]Layer 5 — Property-Based ({prop_iters} iterations)[/bold blue]")
        prop_results = run_property_tests(prop_iters)
        for r in prop_results:
            all_results.append(r)
            console.print(f"    {'[green]✓[/green]' if r.passed else '[red]✗[/red]'} "
                           f"{r.test_id} [{r.duration_s:.1f}s]")

    if gate_tests:
        console.print(f"\n[bold white]Layer 6 — Context Gate ({len(gate_tests)}) "
                       f"[dim]seed={seed}[/dim][/bold white]")
        for tc in track(gate_tests, description="  Running..."):
            r = run_gate_test(tc)
            all_results.append(r)
            console.print(f"    {'[green]✓[/green]' if r.passed else '[red]✗[/red]'} "
                           f"{tc.id} [{r.duration_s*1000:.0f}ms]")

    console.print()
    for layer in sorted(run_layers):
        render_layer_results(all_results, layer, verbose)
    render_summary(all_results)

    report = {
        "timestamp": datetime.now().isoformat(),
        "model":     OLLAMA_MODEL,
        "seed":      seed,
        "layers":    sorted(run_layers),
        "mode":      mode_str,
        "passed":    sum(1 for r in all_results if r.passed),
        "total":     len(all_results),
        "results": [{
            "id": r.test_id, "layer": r.layer, "passed": r.passed,
            "issues": r.issues, "fixes": r.fixes_applied, "duration": r.duration_s,
        } for r in all_results],
    }
    report_path = os.path.join(os.path.dirname(__file__), "test_report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    console.print(f"[dim]Report → {report_path}[/dim]")
    sys.exit(0 if all(r.passed for r in all_results) else 1)


if __name__ == "__main__":
    main()

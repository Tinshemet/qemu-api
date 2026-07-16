#!/usr/bin/env python3
"""
bench_reasoning.py — compare local models on the reasoning/reference failure
modes the Active Library was built to fix, NOT generic tool selection.

Seeds a deterministic fixture (a VM to reference by name, a labelled fleet),
snapshots the Active Library so the fixture lands in the system-prompt digest,
then scores each model on:
  * referential create   — "same OS as bench-ref" → create_vm with os_type=linux
  * fleet exec           — "run X on every benchfleet VM" → fleet(action=exec)
  * fleet stop           — "stop all benchfleet VMs"      → fleet(action=stop)
  * multi-step           — "create a ubuntu vm ... and launch it" → create_vm
Plus the format gate: does the model emit tool calls at all? (Reasoning-distilled
models often reason well but tool-call poorly — that shows up here.)

Runs entirely on ~6GB-VRAM-class local models via Ollama. Cleans up its fixture.

Usage:
  PYTHONPATH=files python3 files/tests/bench_reasoning.py                 # default model set
  PYTHONPATH=files python3 files/tests/bench_reasoning.py qwen2.5:7b deepseek-r1:7b phi4-mini
  PYTHONPATH=files python3 files/tests/bench_reasoning.py -n 3            # 3 samples per case
"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from executor.api.qemu_config import MachineConfig
from shared.executioner.tool_executor import manager
from orchestrator.ai.active_library import LIBRARY
from orchestrator.ai.ollama_client import _build_system_prompt
from tests.shared import AITest
from tests.layer3_ai import call_ollama, run_ai_test

_DEFAULT_MODELS = ["qwen2.5:7b", "qwen2.5-coder:7b", "deepseek-r1:7b", "phi4-mini", "llama3.1:8b"]

_FIXTURE = {
    "bench-ref":   dict(os_type="linux", os_name="ubuntu", guest_agent=True),
    "bench-red-1": dict(os_type="linux", os_name="kali",   guest_agent=True, labels=["benchfleet"]),
    "bench-red-2": dict(os_type="linux", os_name="debian", guest_agent=True, labels=["benchfleet"]),
}

CASES = [
    AITest(
        id="ref_create_same_os",
        tags=["referential"],
        description="Resolve 'same OS as bench-ref' (=ubuntu/linux) via the Library",
        prompt_pool=[
            "create a vm called probe-{name} with the same OS as bench-ref",
            "make a new VM named probe-{name}, same operating system as bench-ref",
        ],
        expect_tools=["create_vm"],
        expect_args={"os_type": "linux"},
    ),
    AITest(
        id="fleet_exec",
        tags=["fleet"],
        description="Broadcast a command across the benchfleet label → fleet(exec)",
        prompt_pool=[
            "run uptime on every VM labeled benchfleet",
            "execute 'whoami' on all my benchfleet VMs",
        ],
        expect_tools=["fleet"],
        expect_args={"action": "exec"},
    ),
    AITest(
        id="fleet_stop",
        tags=["fleet"],
        description="Stop a whole labelled fleet → fleet(stop)",
        prompt_pool=[
            "stop all benchfleet VMs",
            "shut down the entire benchfleet fleet",
        ],
        expect_tools=["fleet"],
        expect_args={"action": "stop"},
    ),
    AITest(
        id="multistep_create_launch",
        tags=["multistep"],
        description="Two-step intent: create then launch",
        prompt_pool=[
            "create a ubuntu vm called probe-{name} and launch it",
            "make an ubuntu VM named probe-{name}, then start it",
        ],
        expect_tools=["create_vm"],
        allow_alternatives={"create_vm": ["create_vm", "launch_vm"]},
    ),
]


def _seed():
    for name, kw in _FIXTURE.items():
        shutil.rmtree(os.path.expanduser(f"~/.qemu_vms/{name}"), ignore_errors=True)
        MachineConfig(name=name, **kw).save()

def _cleanup():
    for name in _FIXTURE:
        for lb in ("benchfleet",):
            try: manager.remove_label(name, lb)
            except Exception: pass
        shutil.rmtree(os.path.expanduser(f"~/.qemu_vms/{name}"), ignore_errors=True)


def main():
    argv    = sys.argv[1:]
    samples = 2
    if "-n" in argv:
        i = argv.index("-n"); samples = int(argv[i + 1]); argv = argv[:i] + argv[i + 2:]
    models = argv or _DEFAULT_MODELS

    _seed()
    LIBRARY.snapshot(manager)
    sp = _build_system_prompt()
    assert "bench-ref" in sp, "fixture not in the system-prompt digest — snapshot failed"
    print(f"Fixture seeded; digest carries bench-ref + benchfleet. Samples per case: {samples}\n")

    results = {}   # model -> {case_id: [passed bools], "_fmt": bool, "_durs": [..]}
    try:
        for model in models:
            print(f"── {model} " + "─" * (40 - len(model)))
            row = {"_durs": []}
            # format gate: does it emit tool calls at all?
            try:
                tcs, _ = call_ollama(
                    [{"role": "system", "content": "You are a VM assistant."},
                     {"role": "user", "content": "list my vms"}], model=model)
                row["_fmt"] = len(tcs) > 0
            except Exception as e:
                print(f"  UNAVAILABLE: {e}")
                row["_fmt"] = False
                results[model] = row
                continue
            print(f"  tool-call format: {'OK' if row['_fmt'] else 'NO TOOL CALLS'}")
            if not row["_fmt"]:
                results[model] = row
                continue
            for tc in CASES:
                oks = []
                for s in range(samples):
                    r = run_ai_test(tc, sp, seed=s, model=model)
                    oks.append(r.passed)
                    row["_durs"].append(r.duration_s)
                row[tc.id] = oks
                bar = "".join("✓" if o else "✗" for o in oks)
                print(f"  {tc.id:26} {bar}  {sum(oks)}/{len(oks)}")
            results[model] = row
    finally:
        _cleanup()

    # summary table
    print("\n" + "=" * 68)
    print(f"{'model':18} {'fmt':4} " + " ".join(f"{c.id[:10]:>11}" for c in CASES) + f" {'avg s':>7}")
    print("-" * 68)
    for model in models:
        row = results.get(model, {})
        if not row.get("_fmt"):
            print(f"{model:18} {'✗':4} {'— no tool calls / unavailable —':>55}")
            continue
        cells = []
        for c in CASES:
            oks = row.get(c.id, [])
            cells.append(f"{sum(oks)}/{len(oks)}".rjust(11)) if oks else cells.append("—".rjust(11))
        avg = (sum(row["_durs"]) / len(row["_durs"])) if row["_durs"] else 0
        print(f"{model:18} {'✓':4} " + " ".join(cells) + f" {avg:7.1f}")
    print("=" * 68)
    print("Higher fractions = better reference/fleet/multi-step handling. "
          "'no tool calls' = unusable for gorgon's loop regardless of reasoning.")


if __name__ == "__main__":
    main()

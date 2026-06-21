"""
probe_tools.py — Direct tool execution probe, no AI in the loop.

Fires 15 hand-crafted calls straight into execute_tool():
  · 5 VALID   — should succeed or return real data
  · 5 BROKEN  — wrong values, bad types, nonexistent targets
  · 5 MISSING — empty / null required fields (context gate territory)

For each call: prints args sent, result received, verdict, and which
layer is responsible (context_gate / sanitizer / executor / ok).
"""

import sys, os, json, traceback, time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from rich.console import Console
from rich.table   import Table
from rich         import box

from client.executioner.tool_executor import execute_tool

console = Console()

# ── Helpers ────────────────────────────────────────────────────────────────────

def _layer(result: dict) -> str:
    """Guess which layer produced this result."""
    if result.get("clarify") and result.get("missing"):
        return "context_gate"
    if result.get("clarify"):
        return "context_gate/executor"
    err = str(result.get("error", "")).lower()
    if "sanitiz" in err or "invalid" in err or "placeholder" in err:
        return "sanitizer"
    if result.get("success") is False:
        return "executor"
    return "ok"


def run_probe(category: str, idx: int, tool: str, args: dict,
              expect_success: bool, expect_clarify: bool = False,
              note: str = "") -> dict:
    tag = f"[{category}:{idx}]"
    t0  = time.perf_counter()
    try:
        result = execute_tool(tool, dict(args), verbose=True)
    except Exception as exc:
        result = {"success": False, "error": f"EXCEPTION: {exc}", "_tb": traceback.format_exc()}
    elapsed = time.perf_counter() - t0

    # Some tools return a plain list (list_vms, list_profiles, scan_isos)
    if isinstance(result, list):
        result = {"success": True, "_list_result": result, "_len": len(result)}

    success   = bool(result.get("success"))
    clarified = bool(result.get("clarify"))
    layer     = _layer(result)

    if expect_clarify:
        passed = clarified
        verdict_str = "PASS" if passed else "FAIL (expected clarify, didn't get one)"
    elif expect_success:
        passed = success and not clarified
        verdict_str = "PASS" if passed else f"FAIL (expected success, got: {result.get('error', result)})"
    else:
        passed = not success or clarified
        verdict_str = "PASS" if passed else "FAIL (expected failure, but got success)"

    colour = "green" if passed else "red"
    console.print(
        f"\n[bold]{tag}[/bold] [cyan]{tool}[/cyan]  "
        f"[dim]{note}[/dim]"
    )
    console.print(f"  args   : {json.dumps(args, default=str)}")
    console.print(
        f"  result : success={success}  clarify={clarified}  "
        f"layer=[yellow]{layer}[/yellow]"
    )
    if result.get("error"):
        console.print(f"  error  : [red]{result['error']}[/red]")
    if result.get("missing"):
        console.print(f"  missing: {[m['field'] for m in result['missing']]}")
    if result.get("_tb"):
        console.print(f"  [red]TRACEBACK:[/red] {result['_tb'][:300]}")
    console.print(f"  verdict: [{colour}]{verdict_str}[/{colour}]  ({elapsed*1000:.0f} ms)")

    return {
        "tag": tag, "tool": tool, "note": note,
        "passed": passed, "layer": layer, "elapsed_ms": round(elapsed * 1000),
        "success": success, "clarify": clarified,
        "error": result.get("error"),
    }


# ── Test definitions ───────────────────────────────────────────────────────────

# Real VMs on this system: hello2, office, uwuntu, yaron

VALID_TESTS = [
    # 1. Zero-arg read-only — no room to fail
    dict(tool="list_vms",    args={},
         note="no args — should return VM list"),

    # 2. System check — reads host caps
    dict(tool="check_system", args={},
         note="reads KVM/CPU/RAM/disk — should always succeed"),

    # 3. Status on a real VM
    dict(tool="vm_status",   args={"name": "uwuntu"},
         note="existing VM — should return stopped/running state"),

    # 4. Show config on a real VM
    dict(tool="show_config", args={"name": "hello2"},
         note="existing VM — should return full config dict"),

    # 5. List hardware profiles
    dict(tool="list_profiles", args={},
         note="reads profile dir — should return profile list"),
]

BROKEN_TESTS = [
    # 1. VM that does not exist
    dict(tool="launch_vm",   args={"name": "ghost_vm_that_never_existed"},
         note="nonexistent VM — executor should reject"),

    # 2. Resize disk with absurdly negative size — sanitizer clamps, then executor may fail
    dict(tool="resize_disk", args={"name": "uwuntu", "new_size_gb": -500},
         note="negative disk size — sanitizer clamps to min, executor may still error"),

    # 3. snapshot on a stopped VM (snapshot_create requires running)
    dict(tool="snapshot_create", args={"name": "hello2", "snap_name": "wont_work"},
         note="VM is stopped — snapshot requires running VM, should fail at executor"),

    # 4. send_monitor_cmd to a VM that is not running
    dict(tool="send_monitor_cmd", args={"name": "office", "cmd": "info status"},
         note="VM probably stopped — monitor socket unavailable, should fail"),

    # 5. create_vm with totally invalid os_type — sanitizer resets it to default but name collision
    dict(tool="create_vm",   args={"name": "probe_broken_vm_zz", "os_type": "unicorn_os"},
         note="bad os_type resets to 'other'; then vm may already exist or get created — either ok"),
]

MISSING_TESTS = [
    # 1. launch_vm with no name at all — context gate must block
    dict(tool="launch_vm",        args={},
         note="no name — context gate should fire"),

    # 2. stop_vm with empty string name — context gate must block
    dict(tool="stop_vm",          args={"name": ""},
         note="empty name string — context gate should fire"),

    # 3. clone_vm missing new_name — gate blocks (both fields required)
    dict(tool="clone_vm",         args={"source_name": "uwuntu"},
         note="missing new_name — context gate should fire"),

    # 4. snapshot_restore with snap_name empty — gate blocks
    dict(tool="snapshot_restore", args={"name": "uwuntu", "snap_name": ""},
         note="empty snap_name — context gate should fire"),

    # 5. create_vm missing os_type — gate blocks (both name+os_type required)
    dict(tool="create_vm",        args={"name": "probe_missing_ostype"},
         note="missing os_type — context gate should fire"),
]


# ── Runner ─────────────────────────────────────────────────────────────────────

def main():
    results = []

    console.rule("[bold blue]VALID CALLS (expect success)[/bold blue]")
    for i, t in enumerate(VALID_TESTS, 1):
        results.append(run_probe("VALID", i, t["tool"], t["args"],
                                 expect_success=True, note=t["note"]))

    console.rule("[bold red]BROKEN CALLS (expect failure)[/bold red]")
    for i, t in enumerate(BROKEN_TESTS, 1):
        # probe_broken_vm_zz might get created — that's an observable side-effect we flag
        results.append(run_probe("BROKEN", i, t["tool"], t["args"],
                                 expect_success=False, note=t["note"]))

    console.rule("[bold yellow]MISSING DATA CALLS (expect context_gate clarify)[/bold yellow]")
    for i, t in enumerate(MISSING_TESTS, 1):
        results.append(run_probe("MISSING", i, t["tool"], t["args"],
                                 expect_success=False, expect_clarify=True, note=t["note"]))

    # ── Summary table ──────────────────────────────────────────────────────────
    console.rule("[bold]SUMMARY[/bold]")
    tbl = Table(box=box.SIMPLE_HEAVY, show_header=True)
    tbl.add_column("Tag",     style="dim",    width=12)
    tbl.add_column("Tool",    style="cyan",   width=24)
    tbl.add_column("Layer",   style="yellow", width=14)
    tbl.add_column("ms",      justify="right",width=6)
    tbl.add_column("Result",  width=40)

    passed = failed = 0
    for r in results:
        icon = "[green]✓[/green]" if r["passed"] else "[red]✗[/red]"
        note = r.get("error") or ("clarify fired" if r["clarify"] else "ok")
        tbl.add_row(r["tag"], r["tool"], r["layer"],
                    str(r["elapsed_ms"]), f"{icon} {note[:38]}")
        if r["passed"]:
            passed += 1
        else:
            failed += 1

    console.print(tbl)
    console.print(f"\n[bold]Total: {len(results)}  "
                  f"[green]PASS {passed}[/green]  "
                  f"[red]FAIL {failed}[/red][/bold]\n")

    # Cleanup: remove probe_broken_vm_zz if it was accidentally created
    try:
        from client.executioner.tool_executor import execute_tool as et
        probe_vms = [v["name"] for v in et("list_vms", {}, verbose=True)
                     if "probe_" in v.get("name", "")]
        for pv in probe_vms:
            et("delete_vm", {"name": pv}, verbose=True)
            console.print(f"[dim]cleaned up: {pv}[/dim]")
    except Exception:
        pass


if __name__ == "__main__":
    main()

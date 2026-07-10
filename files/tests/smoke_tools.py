#!/usr/bin/env python3
"""
smoke_tools.py — Smoke test for all executor tools (local, no HTTP).

Run from repo root:
    PYTHONPATH=files python3 files/tests/smoke_tools.py

Tests every tool in the executor dispatch table via direct Python calls.
Creates a short-lived test VM and cleans it up at the end.
"""

import os
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rich.console import Console
from rich.table import Table
from rich import box

from shared.executioner.tool_executor import dispatch_tool

console = Console()

_UID     = uuid.uuid4().hex[:6]
_TEST_VM = f"smoke-{_UID}"
_LIVE_VM = "vm-executor"       # must be running
_PROFILE = "dell_g15_5520"

_results: list[tuple[str, bool, str]] = []


def check(name: str, result, *, key: str = "success", expect_list: bool = False,
          expect_key: str = "") -> bool:
    if expect_list:
        ok = isinstance(result, list)
        detail = f"{len(result)} items" if ok else f"expected list, got {type(result).__name__}"
    elif expect_key:
        ok = isinstance(result, dict) and expect_key in result
        detail = "" if ok else f"missing key '{expect_key}'"
    else:
        ok = isinstance(result, dict) and bool(result.get(key, False))
        detail = result.get("error", "") if isinstance(result, dict) and not ok else ""
    _results.append((name, ok, detail))
    status = "[green]PASS[/green]" if ok else "[red]FAIL[/red]"
    console.print(f"  {status}  {name}" + (f" — {detail}" if detail else ""))
    return ok


# ── Read-only tools ────────────────────────────────────────────────────────────

console.rule("[bold]Read-only tools")

check("check_system",  dispatch_tool("check_system", {}),            expect_key="kvm_available")
check("scan_isos",     dispatch_tool("scan_isos", {}),               expect_list=True)
check("list_profiles", dispatch_tool("list_profiles", {}),           expect_list=True)
vms_r = dispatch_tool("list_vms", {})
check("list_vms",      vms_r,                                       expect_list=True)

# Auto-detect a usable live VM: prefer the hardcoded default if it exists,
# else fall back to the first VM in the list (avoids false FAILs on a clean host).
if isinstance(vms_r, list) and vms_r:
    known_names = {v.get("name") for v in vms_r if isinstance(v, dict)}
    if _LIVE_VM not in known_names:
        _LIVE_VM = next((v["name"] for v in vms_r if isinstance(v, dict) and v.get("name")), _LIVE_VM)
        console.print(f"  [dim]Auto-selected live VM: {_LIVE_VM}[/dim]")

r = dispatch_tool("vm_status", {"name": _LIVE_VM})
ok = isinstance(r, dict) and "state" in r
_results.append(("vm_status", ok, r.get("state", str(r))))
console.print(f"  {'[green]PASS[/green]' if ok else '[red]FAIL[/red]'}  vm_status — state={r.get('state','?')}")

r = dispatch_tool("monitor_vm", {"name": _LIVE_VM})
ok = isinstance(r, dict) and "state" in r
_results.append(("monitor_vm", ok, r.get("state", "")))
console.print(f"  {'[green]PASS[/green]' if ok else '[red]FAIL[/red]'}  monitor_vm — state={r.get('state','?')}")

check("get_vm_logs",   dispatch_tool("get_vm_logs",   {"name": _LIVE_VM}),                  expect_key="log_exists")
check("print_command", dispatch_tool("print_command", {"name": _LIVE_VM}),                  expect_key="command")
check("list_networks", dispatch_tool("list_networks", {}),                                  expect_list=True)
check("send_monitor_cmd", dispatch_tool("send_monitor_cmd", {"name": _LIVE_VM, "cmd": "info status"}), expect_key="output")
check("fingerprint_vm",   dispatch_tool("fingerprint_vm",   {"name": _LIVE_VM}))

r = dispatch_tool("check_profile_compatibility", {"profile_name": _PROFILE})
ok = isinstance(r, dict) and ("compatible" in r or r.get("success"))
_results.append(("check_profile_compatibility", ok, ""))
console.print(f"  {'[green]PASS[/green]' if ok else '[red]FAIL[/red]'}  check_profile_compatibility")


# ── VM lifecycle ───────────────────────────────────────────────────────────────

console.rule("[bold]VM lifecycle")

r = dispatch_tool("create_vm", {"name": _TEST_VM, "os_type": "linux",
                                "memory_mb": 512, "cpu_cores": 1})
if not check("create_vm", r):
    console.print("[yellow]Skipping lifecycle tests — VM creation failed[/yellow]")
else:
    check("show_config",   dispatch_tool("show_config",   {"name": _TEST_VM}),                        expect_key="config")
    # Disk defaults to 60 GB; grow to 65 GB to avoid shrink error
    check("resize_disk",   dispatch_tool("resize_disk",   {"name": _TEST_VM, "disk_index": 0, "new_size_gb": 65}))
    check("update_config", dispatch_tool("update_config", {"name": _TEST_VM, "memory_mb": 768}))

    # Snapshots: create/list/delete all work live via QMP; restore needs VM stopped
    console.print("  [dim]Launching test VM for snapshot tests (OVMF prompt — no OS)[/dim]")
    launch_r = dispatch_tool("launch_vm", {"name": _TEST_VM, "display": "vnc"})
    if launch_r.get("success") or launch_r.get("already_running"):
        time.sleep(4)  # give QEMU/QMP time to initialise
        check("snapshot_create", dispatch_tool("snapshot_create", {"name": _TEST_VM, "snap_name": "snap1"}))
        check("snapshot_list",   dispatch_tool("snapshot_list",   {"name": _TEST_VM}), expect_key="snapshots")
        check("snapshot_delete", dispatch_tool("snapshot_delete", {"name": _TEST_VM, "snap_name": "snap1"}))
        # Offline restore: stop VM, restore, keep stopped for clone/delete
        dispatch_tool("stop_vm", {"name": _TEST_VM})
        for _ in range(15):
            if dispatch_tool("vm_status", {"name": _TEST_VM}).get("state") == "stopped":
                break
            time.sleep(1)
        # Create an offline snapshot first, then restore it
        dispatch_tool("snapshot_create", {"name": _TEST_VM, "snap_name": "snap2"})
        check("snapshot_restore", dispatch_tool("snapshot_restore", {"name": _TEST_VM, "snap_name": "snap2"}))
        dispatch_tool("snapshot_delete", {"name": _TEST_VM, "snap_name": "snap2"})
    else:
        console.print(f"  [yellow]SKIP[/yellow]  snapshot tests — launch failed: {launch_r.get('error','')}")
        for sname in ("snapshot_create", "snapshot_list", "snapshot_delete", "snapshot_restore"):
            _results.append((sname, False, "VM launch failed"))

    clone = f"{_TEST_VM}-c"
    r = dispatch_tool("clone_vm", {"source_name": _TEST_VM, "new_name": clone})
    if check("clone_vm", r):
        check("delete_vm (clone)", dispatch_tool("delete_vm", {"name": clone}))

    check("delete_vm", dispatch_tool("delete_vm", {"name": _TEST_VM}))


# ── Network lifecycle ──────────────────────────────────────────────────────────

console.rule("[bold]Network lifecycle")

_NET = f"smoke-net-{_UID}"
r = dispatch_tool("create_network", {"net_name": _NET})
if check("create_network", r):
    nets = dispatch_tool("list_networks", {})
    ok   = isinstance(nets, list) and any(
        n.get("name") == _NET for n in nets if isinstance(n, dict)
    )
    _results.append(("list_networks (after create)", ok, ""))
    console.print(f"  {'[green]PASS[/green]' if ok else '[red]FAIL[/red]'}  list_networks (after create)")
    check("delete_network", dispatch_tool("delete_network", {"net_name": _NET}))


# ── Summary ────────────────────────────────────────────────────────────────────

console.rule("[bold]Summary")
table = Table(box=box.SIMPLE_HEAD, show_header=True)
table.add_column("Tool", style="cyan")
table.add_column("Result", justify="center")
table.add_column("Detail", style="dim")

passed = failed = 0
for name, ok, detail in _results:
    table.add_row(name, "[green]PASS[/green]" if ok else "[red]FAIL[/red]", detail)
    passed += ok
    failed += not ok

console.print(table)
console.print(f"\n[bold]{'[green]' if failed == 0 else '[yellow]'}{passed}/{passed+failed} passed[/bold]")
sys.exit(0 if failed == 0 else 1)

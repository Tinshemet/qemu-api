#!/usr/bin/env python3
"""
smoke_commands.py — Smoke test for all client HTTP commands (local or remote).

Usage:
    # Against localhost orchestrator (local mode):
    python3 files/tests/smoke_commands.py --url http://localhost:8080 --token test

    # Against the orchestrator VM from the host:
    python3 files/tests/smoke_commands.py --url http://localhost:18080 --token orchestrator-token-123

    # From inside vm-client against vm-orchestrator:
    python3 files/tests/smoke_commands.py --url http://10.0.2.2:18080 --token orchestrator-token-123
"""

import argparse
import sys
import uuid

import requests
from rich.console import Console
from rich.markup import escape as _escape
from rich.table import Table
from rich import box

console = Console()

_UID      = uuid.uuid4().hex[:6]
_TEST_VM  = f"smoke-{_UID}"
_PROFILE  = "dell_g15_5520"
_results: list[tuple[str, bool, str]] = []

parser = argparse.ArgumentParser()
parser.add_argument("--url",    default="http://localhost:8080")
parser.add_argument("--token",  default="test")
parser.add_argument("--live-vm", default="vm-executor",
                    help="Name of a pre-existing VM to use for status/monitor tests")
parser.add_argument("--ca-cert", default=None,
                    help="Path to a CA cert / self-signed cert to verify HTTPS connections against")
parser.add_argument("--insecure", action="store_true",
                    help="Skip TLS certificate verification (dev only)")
args = parser.parse_args()

BASE    = args.url.rstrip("/")
HEADERS = {"Authorization": f"Bearer {args.token}"}
TIMEOUT = 30
_LIVE_VM = args.live_vm
_VERIFY  = False if args.insecure else (args.ca_cert or True)


def _get(path: str, **params):
    try:
        r = requests.get(f"{BASE}{path}", params=params, headers=HEADERS, timeout=TIMEOUT, verify=_VERIFY)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"success": False, "error": str(e)}


def _execute(tool_name: str, tool_args: dict = {}) -> dict:
    """Call POST /execute — unwraps orchestrator's {"ok": true, "result": ...} envelope."""
    try:
        r = requests.post(
            f"{BASE}/execute",
            json={"tool_name": tool_name, "args": tool_args},
            headers=HEADERS,
            timeout=TIMEOUT,
            verify=_VERIFY,
        )
        r.raise_for_status()
        data = r.json()
        # Orchestrator wraps results: {"ok": True, "result": <tool_result>}
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data
    except Exception as e:
        return {"success": False, "error": str(e)}


def check(name: str, result, *, key: str = "success", expect_list: bool = False,
          expect_key: str = "") -> bool:
    if expect_list:
        ok = isinstance(result, list)
        detail = f"{len(result)} items" if ok else f"expected list, got: {str(result)[:80]}"
    elif expect_key:
        ok = isinstance(result, dict) and expect_key in result
        detail = "" if ok else f"missing key '{expect_key}': {str(result)[:80]}"
    else:
        ok = isinstance(result, dict) and bool(result.get(key, False))
        detail = result.get("error", "") if isinstance(result, dict) and not ok else ""
    _results.append((name, ok, detail))
    status = "[green]PASS[/green]" if ok else "[red]FAIL[/red]"
    console.print(f"  {status}  {name}" + (f" — {_escape(str(detail))}" if detail else ""))
    return ok


# ── Orchestrator endpoints ─────────────────────────────────────────────────────

console.rule("[bold]Orchestrator endpoints")

r = _get("/health")
ok = isinstance(r, dict) and r.get("status") == "ok"
_results.append(("GET /health", ok, r.get("status", str(r))))
console.print(f"  {'[green]PASS[/green]' if ok else '[red]FAIL[/red]'}  GET /health — {r.get('status','?')}")

r = _get("/info")
check("GET /info", r, expect_key="ollama_model")

r = _get("/sync")
ok = isinstance(r, dict) and ("vms" in r or "profiles" in r)
_results.append(("GET /sync", ok, ""))
console.print(f"  {'[green]PASS[/green]' if ok else '[red]FAIL[/red]'}  GET /sync")


# ── Read-only tools via /execute ───────────────────────────────────────────────

console.rule("[bold]Read-only tools")

check("check_system",  _execute("check_system"),                                   expect_key="kvm_available")
check("scan_isos",     _execute("scan_isos"),                                      expect_list=True)
check("list_profiles", _execute("list_profiles"),                                  expect_list=True)
vms_r = _execute("list_vms")
check("list_vms",      vms_r,                                                      expect_list=True)

# Auto-detect a usable live VM: prefer --live-vm if it exists, else first VM in list
if isinstance(vms_r, list) and vms_r:
    known_names = {v.get("name") for v in vms_r if isinstance(v, dict)}
    if _LIVE_VM not in known_names:
        _LIVE_VM = next((v["name"] for v in vms_r if isinstance(v, dict) and v.get("name")), _LIVE_VM)
        console.print(f"  [dim]Auto-selected live VM: {_LIVE_VM}[/dim]")

r = _execute("vm_status", {"name": _LIVE_VM})
ok = isinstance(r, dict) and "state" in r
_results.append(("vm_status", ok, r.get("state", r.get("error", ""))))
console.print(f"  {'[green]PASS[/green]' if ok else '[red]FAIL[/red]'}  vm_status — {r.get('state','?')}")

r = _execute("monitor_vm", {"name": _LIVE_VM})
ok = isinstance(r, dict) and "state" in r
_results.append(("monitor_vm", ok, r.get("state", "")))
console.print(f"  {'[green]PASS[/green]' if ok else '[red]FAIL[/red]'}  monitor_vm — {r.get('state','?')}")

check("get_vm_logs",              _execute("get_vm_logs",   {"name": _LIVE_VM}),                    expect_key="log_exists")
check("print_command",            _execute("print_command", {"name": _LIVE_VM}),                    expect_key="command")
check("list_networks",            _execute("list_networks"),                                        expect_list=True)
_smc = _execute("send_monitor_cmd", {"name": _LIVE_VM, "cmd": "info status"})
_smc_ok = isinstance(_smc, dict) and ("output" in _smc or "error" in _smc)
_results.append(("send_monitor_cmd", _smc_ok, _smc.get("output", _smc.get("error", ""))[:80]))
console.print(f"  {'[green]PASS[/green]' if _smc_ok else '[red]FAIL[/red]'}  send_monitor_cmd — {_escape(_smc.get('output', _smc.get('error', ''))[:60])}")
check("fingerprint_vm",           _execute("fingerprint_vm", {"name": _LIVE_VM}))
check("check_profile_compat",     _execute("check_profile_compatibility", {"profile_name": _PROFILE}), expect_key="compatible")


# ── VM lifecycle ───────────────────────────────────────────────────────────────

console.rule("[bold]VM lifecycle")

r = _execute("create_vm", {"name": _TEST_VM, "os_type": "linux", "memory_mb": 512, "cpu_cores": 1})
if not check("create_vm", r):
    console.print("[yellow]Skipping lifecycle tests — create_vm failed[/yellow]")
else:
    check("show_config",   _execute("show_config",   {"name": _TEST_VM}),                        expect_key="config")
    check("resize_disk",   _execute("resize_disk",   {"name": _TEST_VM, "disk_index": 0, "new_size_gb": 65}))
    check("update_config", _execute("update_config", {"name": _TEST_VM, "memory_mb": 768}))

    clone = f"{_TEST_VM}-c"
    r = _execute("clone_vm", {"source_name": _TEST_VM, "new_name": clone})
    if check("clone_vm", r):
        check("delete_vm (clone)", _execute("delete_vm", {"name": clone, "force": True}))

    check("delete_vm", _execute("delete_vm", {"name": _TEST_VM, "force": True}))


# ── Snapshot lifecycle (needs a running VM) ────────────────────────────────────

console.rule("[bold]Snapshot lifecycle")

_SNAP_VM = f"smoke-snap-{_UID}"
r = _execute("create_vm", {"name": _SNAP_VM, "os_type": "linux", "memory_mb": 512, "cpu_cores": 1})
if not check("create_vm (snap)", r):
    console.print("[yellow]Skipping snapshot tests[/yellow]")
else:
    # All snapshot ops offline (VM never launched) — tests the chain, not KVM
    check("snapshot_create",  _execute("snapshot_create",  {"name": _SNAP_VM, "snap_name": "snap1"}))
    check("snapshot_list",    _execute("snapshot_list",    {"name": _SNAP_VM}),                expect_key="snapshots")
    check("snapshot_delete",  _execute("snapshot_delete",  {"name": _SNAP_VM, "snap_name": "snap1", "force": True}))
    _execute("snapshot_create", {"name": _SNAP_VM, "snap_name": "snap2"})
    check("snapshot_restore", _execute("snapshot_restore", {"name": _SNAP_VM, "snap_name": "snap2", "force": True}))
    _execute("snapshot_delete", {"name": _SNAP_VM, "snap_name": "snap2", "force": True})
    _execute("delete_vm", {"name": _SNAP_VM, "force": True})


# ── Network lifecycle ──────────────────────────────────────────────────────────

console.rule("[bold]Network lifecycle")

_NET = f"smoke-net-{_UID}"
r = _execute("create_network", {"net_name": _NET})
if check("create_network", r):
    nets = _execute("list_networks")
    ok   = isinstance(nets, list) and any(
        n.get("name") == _NET for n in nets if isinstance(n, dict)
    )
    _results.append(("list_networks (after create)", ok, ""))
    console.print(f"  {'[green]PASS[/green]' if ok else '[red]FAIL[/red]'}  list_networks (after create)")
    check("delete_network", _execute("delete_network", {"net_name": _NET}))


# ── Summary ────────────────────────────────────────────────────────────────────

console.rule("[bold]Summary")
table = Table(box=box.SIMPLE_HEAD, show_header=True)
table.add_column("Command / Endpoint", style="cyan")
table.add_column("Result", justify="center")
table.add_column("Detail", style="dim")

passed = failed = 0
for name, ok, detail in _results:
    table.add_row(name, "[green]PASS[/green]" if ok else "[red]FAIL[/red]", _escape(str(detail)))
    passed += ok
    failed += not ok

console.print(table)
console.print(f"\n[bold]{'[green]' if failed == 0 else '[yellow]'}{passed}/{passed+failed} passed[/bold]")
console.print(f"[dim]Tested against: {BASE}[/dim]")
sys.exit(0 if failed == 0 else 1)

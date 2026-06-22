"""
tests/layer9_pipeline_gated.py — Full executor probe WITH context gate active.

Gate is live (skip_gate=False). Every call goes through:
    gate_check() → _sanitise_args() → executor

Three categories:

  PASSTHROUGH  — all gate-required fields present with valid values.
                 Gate MUST return None (not block).  If a passthrough
                 test comes back with clarify=True that is the
                 double-clarification bug — the gate is blocking a
                 field the caller already provided.

  GATED        — one or more gate-required fields absent/empty.
                 Gate MUST fire.  The "missing" list in the response
                 must contain ONLY the truly absent fields — not any
                 field that was provided.  That is the other half of
                 the double-ask bug.

  EXEC_BROKEN  — all gate-required fields present, but values are
                 wrong for the executor (ghost VM, invalid field,
                 etc.).  Gate passes, executor should fail.

Anti double-ask checks embedded in every test:
  · PASSTHROUGH → assert no clarify key in result
  · GATED       → assert clarify=True AND missing list matches
                   exactly the fields we omitted (nothing extra)
"""

import time, traceback
from typing import Any, Dict, List, Optional

from .shared import PipelineTest, TestResult, execute_tool


# ── Layer 9 result classifier ──────────────────────────────────────────────────

def _classify(result: Any) -> str:
    """Returns 'gate' | 'executor' | 'ok'."""
    if not isinstance(result, dict):
        return "ok"
    # Gate always sets "missing" list AND "clarify" without setting "success"
    if result.get("clarify") and "missing" in result:
        return "gate"
    if result.get("success") is False:
        return "executor"
    return "ok"


# ── Test dataclass extension with gate-specific checks ─────────────────────────

# We reuse PipelineTest but interpret fields differently for Layer 9:
#   category="passthrough" → gate must NOT fire
#   category="gated"       → gate MUST fire; expect_missing_fields lists the
#                            fields we omitted (gate should report exactly those)
#   category="exec_broken" → gate must pass; executor should fail

def _pt(tool, args, note="", keys=None):
    """PASSTHROUGH — gate must not block."""
    return PipelineTest(
        id=f"p9_pass_{tool}_{'_'.join(str(v)[:12] for v in list(args.values())[:2])}",
        tags=["gated_pipeline", "passthrough", tool],
        description=f"[pass] {note or tool}",
        tool=tool, input_args=args,
        category="passthrough",
        expect_success=True,
        expect_layer="ok",
    )

def _ptk(tool, args, keys, note=""):
    """PASSTHROUGH for tools that return no success key — check keys instead."""
    return PipelineTest(
        id=f"p9_pass_{tool}_{'_'.join(str(v)[:12] for v in list(args.values())[:2])}",
        tags=["gated_pipeline", "passthrough", tool],
        description=f"[pass] {note or tool} (key check)",
        tool=tool, input_args=args,
        category="passthrough",
        expect_success=None,
        expect_layer="ok",
        expect_result_keys=keys,
    )

def _pts(tool, args, note=""):
    """PASSTHROUGH but result is state-dependent — only assert gate did not fire."""
    return PipelineTest(
        id=f"p9_pass_{tool}_{'_'.join(str(v)[:12] for v in list(args.values())[:2])}",
        tags=["gated_pipeline", "passthrough", "state_dep", tool],
        description=f"[pass/state] {note or tool}",
        tool=tool, input_args=args,
        category="passthrough",
        expect_success=None,
        expect_layer=None,  # don't check executor result
    )

def _g(tool, args, missing_fields: List[str], note=""):
    """GATED — gate must fire and report exactly these missing fields."""
    return PipelineTest(
        id=f"p9_gate_{tool}_miss_{'_'.join(missing_fields)}",
        tags=["gated_pipeline", "gated", tool],
        description=f"[gate] {note or tool} — missing: {missing_fields}",
        tool=tool, input_args=args,
        category="gated",
        expect_success=False,
        expect_layer="gate",
        # Store expected missing fields in expect_result_keys for runner to use
        expect_result_keys=missing_fields,
    )

def _eb(tool, args, note=""):
    """EXEC_BROKEN — gate passes, executor fails."""
    return PipelineTest(
        id=f"p9_exec_{tool}_{'_'.join(str(v)[:10] for v in list(args.values())[:2])}",
        tags=["gated_pipeline", "exec_broken", tool],
        description=f"[exec] {note or tool}",
        tool=tool, input_args=args,
        category="exec_broken",
        expect_success=False,
        expect_layer="executor",
    )

def _ebs(tool, args, note=""):
    """EXEC_BROKEN but state-dependent — gate passes, executor result varies."""
    return PipelineTest(
        id=f"p9_exec_{tool}_{'_'.join(str(v)[:10] for v in list(args.values())[:2])}",
        tags=["gated_pipeline", "exec_broken", "state_dep", tool],
        description=f"[exec/state] {note or tool}",
        tool=tool, input_args=args,
        category="exec_broken",
        expect_success=None,
        expect_layer=None,
    )


# ── Test matrix ────────────────────────────────────────────────────────────────
#
# For every gated tool we test:
#   1. Full passthrough    — all required fields + valid values → gate stays silent
#   2. Partial passthrough — some optional extras — gate still stays silent
#   3. Gate-all           — no required fields at all → gate fires, lists everything
#   4. Gate-partial       — one required field provided, one missing → gate asks
#                           ONLY for the missing one (anti-double-ask check)
#   5. Exec-broken        — all gate fields present but wrong values

GATED_TESTS: List[PipelineTest] = [

    # ── Ungated no-arg tools — run first, no VM needed ──────────────────────────
    _ptk("check_system",   {}, ["kvm_available","qemu_installed"], "ungated — gate passes"),
    _pt ("scan_isos",       {}, "ungated — returns list"),
    _pt ("list_vms",        {}, "ungated — returns list"),
    _pt ("list_profiles",   {}, "ungated — returns list"),
    _pt ("list_networks",   {}, "ungated — returns list"),

    # ── create_vm (gate: name, os_type) — runs before VM-dependent tests ─────
    # These create probe9_min / probe9_cpu etc. that later tests rely on.

    # Passthrough — gate silent
    _pt ("create_vm", {"name":"probe9_min",  "os_type":"linux"},                          "minimal — gate passes both fields"),
    _pt ("create_vm", {"name":"probe9_cpu",  "os_type":"linux","memory_mb":4096,"cpu_cores":4}, "all gate fields + extras"),
    _pt ("create_vm", {"name":"probe9_win",  "os_type":"windows","memory_mb":8192},       "windows — gate passes"),
    _pt ("create_vm", {"name":"probe9_disp", "os_type":"linux","display":"sdl"},          "with display override"),
    _pt ("create_vm", {"name":"probe9_dry",  "os_type":"linux"},                          "gate passes, executor creates"),

    # Gate fires
    _g  ("create_vm", {},                          ["name","os_type"], "both missing"),
    _g  ("create_vm", {"name":"probe9_noOs"},      ["os_type"],        "name present → gate asks ONLY os_type (anti double-ask)"),
    _g  ("create_vm", {"os_type":"linux"},         ["name"],           "os_type present → gate asks ONLY name (anti double-ask)"),
    _g  ("create_vm", {"name":""},                 ["name","os_type"], "empty name string → treated as missing"),
    _g  ("create_vm", {"name":"  "},               ["name","os_type"], "whitespace-only name → treated as missing"),
    # empty os_type is sanitized to "linux" (default) BEFORE the gate sees it,
    # so the gate never fires for os_type; executor then runs normally
    _ebs("create_vm", {"name":"probe9_noOs_e","os_type":""}, "empty os_type → sanitizer fills in 'linux' → gate passes → executor runs"),

    # "windows-vm" is a placeholder name: sanitizer strips it to "" THEN gate asks for name
    _g  ("create_vm", {"name":"windows-vm","os_type":"windows"}, ["name"], "placeholder name sanitized to '' → gate asks for real name"),

    # ── Ungated VM-dependent tools — run AFTER probe9_min is created above ────
    _ptk("fingerprint_vm", {"name":"probe9_min"},                       ["score"],         "ungated — gate never fires"),
    _ptk("fingerprint_vm", {"name":"probe9_min","summary":True},        ["score"],         "ungated + summary flag"),
    _pts("check_disk",     {"name":"probe9_min"},                                          "ungated — executor validates disk"),
    _eb ("check_disk",     {"name":"ghost_xyz"},                                           "ungated — executor fails for ghost VM"),
    _ptk("get_vm_logs",    {"name":"probe9_min"},                       ["name","log_exists"], "ungated — returns log dict"),

    # ── clone_vm (gate: source_name, new_name) ───────────────────────────────

    _pt ("clone_vm", {"source_name":"probe9_min", "new_name":"probe9_clone"}, "both fields — gate passes"),
    _g  ("clone_vm", {},                          ["source_name","new_name"], "both missing"),
    _g  ("clone_vm", {"source_name":"probe9_min"},    ["new_name"],   "source_name present → only new_name asked"),
    _g  ("clone_vm", {"new_name":"probe9_clone"}, ["source_name"],"new_name present → only source_name asked"),
    _eb ("clone_vm", {"source_name":"ghost_xyz",  "new_name":"probe9_clone"}, "source doesn't exist"),
    _eb ("clone_vm", {"source_name":"probe9_min",     "new_name":"probe9_cpu"},   "new_name collides with existing VM"),

    # ── launch_vm (gate: name) ───────────────────────────────────────────────

    _pts("launch_vm", {"name":"probe9_cpu"},                        "gate passes — executor may fail if already running"),
    _pts("launch_vm", {"name":"probe9_min","dry_run":True},         "dry_run — gate passes"),
    _g  ("launch_vm", {},                    ["name"],          "name missing"),
    _g  ("launch_vm", {"dry_run":True},      ["name"],          "dry_run only — gate asks for name"),
    _g  ("launch_vm", {"name":""},           ["name"],          "empty name"),
    _eb ("launch_vm", {"name":"ghost_xyz"},                     "VM doesn't exist — gate passes, executor fails"),

    # ── stop_vm (gate: name) ────────────────────────────────────────────────

    _ebs("stop_vm",  {"name":"probe9_min"},             "gate passes — executor result depends on VM state"),
    _ebs("stop_vm",  {"name":"all"},                "stop all — gate passes (name='all' is valid)"),
    _g  ("stop_vm",  {},               ["name"],    "name missing"),
    _g  ("stop_vm",  {"force":True},   ["name"],    "force only — gate asks for name"),
    _eb ("stop_vm",  {"name":"ghost_xyz"}, "VM doesn't exist"),

    # ── delete_vm (gate: name) ───────────────────────────────────────────────

    _eb ("delete_vm", {"name":"ghost_xyz"},           "VM doesn't exist — gate passes, executor fails"),
    _g  ("delete_vm", {},               ["name"],     "name missing"),
    _g  ("delete_vm", {"name":""},      ["name"],     "empty name"),

    # ── vm_status (gate: name) ───────────────────────────────────────────────

    _ptk("vm_status", {"name":"probe9_min"},    ["name","state"], "gate passes — returns state dict"),
    _ptk("vm_status", {"name":"probe9_cpu"},    ["name","state"], "second VM"),
    _ptk("vm_status", {"name":"ghost_xyz"}, ["name","state"], "ghost VM — gate passes, executor returns stopped state"),
    _g  ("vm_status", {},                   ["name"],         "name missing"),
    _g  ("vm_status", {"name":""},          ["name"],         "empty name"),

    # ── monitor_vm (gate: name) ──────────────────────────────────────────────

    _ptk("monitor_vm", {"name":"probe9_min"},    ["name","state"], "gate passes"),
    _ptk("monitor_vm", {"name":"all"},       ["name","state"] if False else [], "monitor all — gate passes (special value)"),
    _g  ("monitor_vm", {},                   ["name"],          "name missing"),
    _g  ("monitor_vm", {"name":""},          ["name"],          "empty name"),

    # ── show_config (gate: name) ─────────────────────────────────────────────

    _pt ("show_config", {"name":"probe9_min"}, "gate passes — config returned"),
    _pt ("show_config", {"name":"probe9_cpu"}, "second VM"),
    _g  ("show_config", {},               ["name"],  "name missing"),
    _g  ("show_config", {"name":""},      ["name"],  "empty name"),
    _eb ("show_config", {"name":"ghost_xyz"},        "VM doesn't exist — gate passes, executor fails"),

    # ── update_config (gate: name) ───────────────────────────────────────────

    _pt ("update_config", {"name":"probe9_min","updates":{"memory_mb":4096}},            "gate passes"),
    _pt ("update_config", {"name":"probe9_min","updates":{"display":"sdl","audio":"hda"}},"multiple updates"),
    _g  ("update_config", {},                        ["name"],   "name missing"),
    _g  ("update_config", {"updates":{"memory_mb":4096}}, ["name"], "name missing but updates present"),
    _g  ("update_config", {"name":""},               ["name"],   "empty name"),
    _eb ("update_config", {"name":"ghost_xyz","updates":{"memory_mb":4096}}, "VM doesn't exist"),

    # ── print_command (gate: name) ───────────────────────────────────────────

    _pt ("print_command", {"name":"probe9_min"}, "gate passes"),
    _pt ("print_command", {"name":"probe9_cpu"}, "second VM"),
    _g  ("print_command", {},               ["name"],  "name missing"),
    _eb ("print_command", {"name":"ghost_xyz"},        "VM doesn't exist"),

    # ── resize_disk (gate: name, new_size_gb) ───────────────────────────────

    _ebs("resize_disk", {"name":"probe9_min","new_size_gb":99999},  "gate passes — executor result state-dep"),
    _g  ("resize_disk", {},                                       ["name","new_size_gb"], "both missing"),
    _g  ("resize_disk", {"name":"probe9_min"},                        ["new_size_gb"],        "name present → only new_size_gb asked"),
    _g  ("resize_disk", {"new_size_gb":80},                       ["name"],               "new_size_gb present → only name asked"),
    _g  ("resize_disk", {"name":""},                              ["name","new_size_gb"], "empty name"),
    _eb ("resize_disk", {"name":"ghost_xyz","new_size_gb":80},                           "VM doesn't exist"),

    # ── snapshot_create (gate: name, snap_name) ──────────────────────────────

    _eb ("snapshot_create", {"name":"probe9_min","snap_name":"probe9_snap"},  "gate passes — VM stopped so executor fails"),
    _g  ("snapshot_create", {},                                            ["name","snap_name"], "both missing"),
    _g  ("snapshot_create", {"name":"probe9_min"},                             ["snap_name"],        "name present → only snap_name asked"),
    _g  ("snapshot_create", {"snap_name":"snap1"},                         ["name"],             "snap_name present → only name asked"),
    _eb ("snapshot_create", {"name":"ghost_xyz","snap_name":"snap1"},                           "VM doesn't exist"),

    # ── snapshot_list (gate: name) ───────────────────────────────────────────

    _pt ("snapshot_list", {"name":"probe9_min"}, "gate passes"),
    _g  ("snapshot_list", {},               ["name"],  "name missing"),
    _eb ("snapshot_list", {"name":"ghost_xyz"},        "VM doesn't exist"),

    # ── snapshot_restore (gate: name, snap_name) ─────────────────────────────

    _eb ("snapshot_restore", {"name":"probe9_min","snap_name":"ghost_snap"}, "gate passes — snap doesn't exist"),
    _g  ("snapshot_restore", {},                                          ["name","snap_name"], "both missing"),
    _g  ("snapshot_restore", {"name":"probe9_min"},                           ["snap_name"],        "name present → only snap_name asked"),
    _g  ("snapshot_restore", {"snap_name":"snap1"},                       ["name"],             "snap_name present → only name asked"),

    # ── snapshot_delete (gate: name, snap_name) ──────────────────────────────

    _eb ("snapshot_delete", {"name":"probe9_min","snap_name":"ghost_snap"}, "gate passes — snap doesn't exist"),
    _g  ("snapshot_delete", {},                                          ["name","snap_name"], "both missing"),
    _g  ("snapshot_delete", {"name":"probe9_min"},                           ["snap_name"],        "name present → only snap_name asked"),

    # ── set_resource_limits (gate: name) ─────────────────────────────────────

    _ebs("set_resource_limits", {"name":"probe9_min","cpu_percent":50},  "gate passes — executor needs running VM"),
    _ebs("set_resource_limits", {"name":"probe9_min","memory_mb":2048},  "gate passes — memory limit"),
    _g  ("set_resource_limits", {},                    ["name"],     "name missing"),
    _g  ("set_resource_limits", {"cpu_percent":50},    ["name"],     "limit present but name missing → gate asks only name"),
    _eb ("set_resource_limits", {"name":"ghost_xyz","cpu_percent":50}, "VM doesn't exist"),

    # ── open_display (gate: name) ─────────────────────────────────────────────

    _ebs("open_display", {"name":"probe9_min"}, "gate passes — needs running VM with display"),
    _g  ("open_display", {},               ["name"],  "name missing"),
    _eb ("open_display", {"name":"ghost_xyz"},        "VM doesn't exist"),

    # ── open_shell (gate: name) ───────────────────────────────────────────────

    _ebs("open_shell", {"name":"probe9_min"}, "gate passes — needs running VM"),
    _g  ("open_shell", {},               ["name"],  "name missing"),
    _eb ("open_shell", {"name":"ghost_xyz"},        "VM doesn't exist"),

    # ── send_monitor_cmd (gate: name, cmd) ────────────────────────────────────

    _eb ("send_monitor_cmd", {"name":"probe9_min","cmd":"info status"},   "gate passes — VM stopped, no socket"),
    _eb ("send_monitor_cmd", {"name":"probe9_min","cmd":"info block"},    "gate passes — different cmd"),
    _g  ("send_monitor_cmd", {},                                      ["name","cmd"],  "both missing"),
    _g  ("send_monitor_cmd", {"name":"probe9_min"},                       ["cmd"],         "name present → only cmd asked"),
    _g  ("send_monitor_cmd", {"cmd":"info status"},                   ["name"],        "cmd present → only name asked"),
    _g  ("send_monitor_cmd", {"name":"probe9_min","cmd":""},              ["cmd"],         "empty cmd"),
    _eb ("send_monitor_cmd", {"name":"ghost_xyz","cmd":"info status"},                "VM doesn't exist"),

    # ── check_profile_compatibility (gate: profile_name) ─────────────────────

    _ptk("check_profile_compatibility", {"profile_name":"raspberry_pi_3b"}, ["compatible","warnings"], "gate passes"),
    _ptk("check_profile_compatibility", {"profile_name":"minimal"},          ["compatible"],            "second profile"),
    _g  ("check_profile_compatibility", {},                        ["profile_name"], "profile_name missing"),
    _g  ("check_profile_compatibility", {"profile_name":""},       ["profile_name"], "empty profile_name"),
    _ptk("check_profile_compatibility", {"profile_name":"ghost_xyz"}, ["compatible"],  "gate passes — executor returns compatible:False"),

    # ── create_profile (gate: profile_name, description) ─────────────────────

    _pt ("create_profile", {"profile_name":"probe9_base","description":"base","memory_mb":2048},  "gate passes both required fields"),
    _pt ("create_profile", {"profile_name":"probe9_arm","description":"arm","machine_arch":"aarch64","machine_type":"virt","cpu_model":"cortex-a72","force":True}, "gate passes + extras"),
    _g  ("create_profile", {},                                                          ["profile_name","description"], "both missing"),
    _g  ("create_profile", {"profile_name":"probe9_nodesc"},                            ["description"],    "profile_name present → only description asked"),
    _g  ("create_profile", {"description":"a profile"},                                 ["profile_name"],   "description present → only profile_name asked"),
    _g  ("create_profile", {"profile_name":"","description":"a profile"},               ["profile_name"],   "empty profile_name → only profile_name asked"),
    _g  ("create_profile", {"profile_name":"probe9_x","description":""},                ["description"],    "empty description → only description asked"),
    _ebs("create_profile", {"profile_name":"probe9_badcpu","description":"bad","cpu_model":"cortex-a72","machine_arch":"x86_64","machine_type":"q35"}, "gate passes — sanitizer corrects ARM cpu+x86 arch mismatch → executor creates profile"),

    # ── delete_profile (gate: profile_name) ──────────────────────────────────

    _pt ("delete_profile", {"profile_name":"probe9_base"},       "delete profile created above — gate passes"),
    _g  ("delete_profile", {},                      ["profile_name"], "profile_name missing"),
    _g  ("delete_profile", {"profile_name":""},     ["profile_name"], "empty profile_name"),
    _eb ("delete_profile", {"profile_name":"ghost_xyz"},          "profile doesn't exist — gate passes"),

    # ── create_network (gate: net_name) ──────────────────────────────────────

    _pt ("create_network", {"net_name":"probe9_net"},    "gate passes"),
    _g  ("create_network", {},                ["net_name"], "net_name missing"),
    _g  ("create_network", {"net_name":""},   ["net_name"], "empty net_name"),

    # ── delete_network (gate: net_name) ──────────────────────────────────────

    _pt ("delete_network", {"net_name":"probe9_net"},    "delete network created above — gate passes"),
    _g  ("delete_network", {},                ["net_name"], "net_name missing"),
    _eb ("delete_network", {"net_name":"ghost_net_xyz"},  "network doesn't exist — gate passes"),

    # ── add_vm_to_network (gate: net_name, vm_name) ──────────────────────────

    _eb ("add_vm_to_network", {"net_name":"ghost_net","vm_name":"probe9_min"},  "network doesn't exist — gate passes"),
    _g  ("add_vm_to_network", {},                                              ["net_name","vm_name"], "both missing"),
    _g  ("add_vm_to_network", {"net_name":"probe9_net"},                       ["vm_name"],   "net_name present → only vm_name asked"),
    _g  ("add_vm_to_network", {"vm_name":"probe9_min"},                        ["net_name"],  "vm_name present → only net_name asked"),
]


# ── Deduplication ─────────────────────────────────────────────────────────────

def _dedup(tests: List[PipelineTest]) -> List[PipelineTest]:
    seen: Dict[str, int] = {}
    out: List[PipelineTest] = []
    for t in tests:
        base = t.id
        if base in seen:
            n = seen[base]
            seen[base] = n + 1
            t = PipelineTest(
                id=f"{base}_{n}", tags=t.tags, description=t.description,
                tool=t.tool, input_args=t.input_args, category=t.category,
                expect_success=t.expect_success, expect_layer=t.expect_layer,
                expect_result_keys=t.expect_result_keys,
            )
        else:
            seen[base] = 1
        out.append(t)
    return out

# import needed for _dedup
from typing import Dict

GATED_TESTS = _dedup(GATED_TESTS)


# ── Cleanup ────────────────────────────────────────────────────────────────────

def cleanup_gated_artifacts():
    import shutil, os
    from shared.executioner.tool_executor import execute_tool as _et
    from shared.api.qemu_config import get_all_profiles, delete_custom_profile

    vm_dir = os.path.expanduser("~/.qemu_vms")
    if os.path.isdir(vm_dir):
        for entry in os.listdir(vm_dir):
            if entry.startswith("probe9"):
                try:
                    _et("delete_vm", {"name": entry}, verbose=False, skip_gate=True)
                except Exception:
                    shutil.rmtree(os.path.join(vm_dir, entry), ignore_errors=True)

    for pname in list(get_all_profiles().keys()):
        if pname.startswith("probe9"):
            try:
                delete_custom_profile(pname)
            except Exception:
                pass

    try:
        nets = _et("list_networks", {}, verbose=False, skip_gate=True)
        if isinstance(nets, (list, dict)):
            net_list = nets if isinstance(nets, list) else nets.get("networks", [])
            for n in net_list:
                nname = n if isinstance(n, str) else n.get("name", "")
                if nname.startswith("probe9"):
                    _et("delete_network", {"net_name": nname}, verbose=False, skip_gate=True)
    except Exception:
        pass


# ── Runner ────────────────────────────────────────────────────────────────────

def run_gated_test(tc: PipelineTest) -> TestResult:
    """
    Runs with skip_gate=False. The checks differ by category:

    passthrough → result must NOT have clarify:True / missing key from gate
    gated       → result MUST have clarify:True; missing list must match
                  exactly the fields in tc.expect_result_keys (no extras,
                  no fields that were actually provided — that's the double-ask bug)
    exec_broken → gate must NOT fire; result must have success:False
    """
    start = time.time()
    issues: List[str] = []

    try:
        result = execute_tool(tc.tool, dict(tc.input_args), verbose=True, skip_gate=False)
    except Exception:
        tb = traceback.format_exc()
        # An exception means neither gate nor executor returned — unexpected
        passed = False
        return TestResult(
            test_id=tc.id, layer=9, passed=False,
            issues=[f"Unexpected exception: {tb[:200]}"],
            fixes_applied=[], duration_s=time.time() - start,
            detail={
                "category": tc.category, "tool": tc.tool, "args": tc.input_args,
                "actual_layer": "exception", "error": tb[:200],
                "double_ask": False, "state_dep": tc.expect_success is None,
            },
        )

    if isinstance(result, list):
        result = {"success": True, "_list_len": len(result)}

    actual_layer   = _classify(result)
    actual_success = result.get("success")
    actual_clarify = bool(result.get("clarify"))
    actual_missing = [m.get("field") for m in result.get("missing", [])] if isinstance(result.get("missing"), list) else []
    double_ask     = False

    # ── PASSTHROUGH checks ────────────────────────────────────────────────────
    if tc.category == "passthrough":
        if actual_clarify and "missing" in result:
            # Gate fired even though all required fields were provided.
            # Identify which provided fields appear in missing — that's the bug.
            provided = [f for f in actual_missing if tc.input_args.get(f) not in (None, "")]
            double_ask = bool(provided)
            msg = (f"DOUBLE-ASK BUG: gate blocked fields that were already provided: {provided}"
                   if double_ask else
                   f"Gate blocked despite all required fields present. Missing: {actual_missing}")
            issues.append(msg)

        elif tc.expect_success is True and actual_success is not True:
            issues.append(f"Expected success=True got {actual_success} — {result.get('error','')}")

        elif tc.expect_layer and actual_layer != tc.expect_layer:
            issues.append(f"Expected layer '{tc.expect_layer}' got '{actual_layer}'")

        for key in tc.expect_result_keys:
            if key not in result:
                issues.append(f"Result missing key '{key}'")

    # ── GATED checks ─────────────────────────────────────────────────────────
    elif tc.category == "gated":
        expected_missing = tc.expect_result_keys  # field names we expect gate to ask for

        if not actual_clarify or "missing" not in result:
            issues.append(f"Expected gate to fire but got success={actual_success}, clarify={actual_clarify}")
        else:
            # Verify gate asked for EXACTLY the fields we omitted — nothing more.
            extra_asked = [f for f in actual_missing if f not in expected_missing]
            not_asked   = [f for f in expected_missing if f not in actual_missing]

            if extra_asked:
                # Gate asked for a field that WAS provided — double-ask bug
                double_ask = True
                issues.append(f"DOUBLE-ASK BUG: gate asked for provided fields: {extra_asked}")
            if not_asked:
                issues.append(f"Gate missed fields we expected it to ask for: {not_asked}")

    # ── EXEC_BROKEN checks ────────────────────────────────────────────────────
    elif tc.category == "exec_broken":
        if actual_clarify and "missing" in result:
            issues.append(f"Gate fired unexpectedly (all required fields were provided). Missing: {actual_missing}")
        elif tc.expect_success is False and actual_success is not False:
            # State-dependent — only flag if we actually asserted success
            issues.append(f"Expected success=False got {actual_success}")
        # No layer check for exec_broken — state-dep tools may vary

    return TestResult(
        test_id=tc.id, layer=9,
        passed=len(issues) == 0,
        issues=issues, fixes_applied=[],
        duration_s=time.time() - start,
        detail={
            "category":       tc.category,
            "tool":           tc.tool,
            "args":           tc.input_args,
            "actual_layer":   actual_layer,
            "expect_layer":   tc.expect_layer,
            "actual_missing": actual_missing,
            "expect_missing": tc.expect_result_keys if tc.category == "gated" else [],
            "double_ask":     double_ask,
            "error":          result.get("error"),
            "state_dep":      tc.expect_success is None,
        },
    )


# ── Randomised test generator ─────────────────────────────────────────────────
#
# For each gated tool, defines the gate-required fields and their valid values.
# Generator randomly picks which required fields to include.
#
# Key anti-double-ask property: for each generated test, the "expected missing"
# list is derived from what was actually omitted — the runner then verifies the
# gate's response matches exactly that list (no fields that were provided).

_GATE_SCHEMAS: Dict[str, Dict] = {
    # tool → {required: {field: {"valid": [...], "name_uid": bool}}}
    # name_uid=True means substitute a unique suffix so VMs don't collide

    "create_vm":    {"name": {"valid": ["probe9r_{uid}"], "uid": True},
                     "os_type": {"valid": ["linux", "windows", "other"]}},
    "clone_vm":     {"source_name": {"valid": ["probe9_min"]},
                     "new_name": {"valid": ["probe9r_{uid}"], "uid": True}},
    "launch_vm":    {"name": {"valid": ["probe9_min"]}},
    "stop_vm":      {"name": {"valid": ["ghost_r9_xyz"]}},
    "delete_vm":    {"name": {"valid": ["ghost_r9_del"]}},
    "vm_status":    {"name": {"valid": ["probe9_min", "probe9_cpu"]}},
    "monitor_vm":   {"name": {"valid": ["probe9_min", "all"]}},
    "get_vm_logs":  {"name": {"valid": ["probe9_min", "probe9_cpu"]}},
    "show_config":  {"name": {"valid": ["probe9_min", "probe9_cpu"]}},
    # Only "name" is gate-required for update_config (see context_gate_config.json) —
    # "updates" is not gated, so it must not appear here or the random generator will
    # expect the gate to ask for it when omitted, which it never does.
    "update_config":{"name": {"valid": ["probe9_min"]}},
    "print_command":{"name": {"valid": ["probe9_min", "probe9_cpu"]}},
    "resize_disk":  {"name": {"valid": ["probe9_min"]},
                     "new_size_gb": {"valid": [99999]}},
    "snapshot_create":  {"name": {"valid": ["probe9_min"]},
                         "snap_name": {"valid": ["probe9r_snap_{uid}"], "uid": True}},
    "snapshot_list":    {"name": {"valid": ["probe9_min"]}},
    "snapshot_restore": {"name": {"valid": ["probe9_min"]},
                         "snap_name": {"valid": ["ghost_r9_snap"]}},
    "snapshot_delete":  {"name": {"valid": ["probe9_min"]},
                         "snap_name": {"valid": ["ghost_r9_snap"]}},
    "set_resource_limits": {"name": {"valid": ["probe9_min"]}},
    "open_display": {"name": {"valid": ["probe9_min"]}},
    "open_shell":   {"name": {"valid": ["probe9_min"]}},
    "send_monitor_cmd": {"name": {"valid": ["probe9_min"]},
                         "cmd":  {"valid": ["info status", "info block"]}},
    "check_profile_compatibility": {"profile_name": {"valid": ["minimal", "raspberry_pi_3b"]}},
    "create_profile": {"profile_name": {"valid": ["probe9r_p_{uid}"], "uid": True},
                       "description":  {"valid": ["a test profile", "random profile"]}},
    "delete_profile": {"profile_name": {"valid": ["ghost_r9_profile"]}},
    "create_network": {"net_name": {"valid": ["probe9r_net_{uid}"], "uid": True}},
    "delete_network": {"net_name": {"valid": ["ghost_r9_net"]}},
    "add_vm_to_network": {"net_name": {"valid": ["ghost_r9_net"]},
                          "vm_name":  {"valid": ["probe9_min"]}},
}

# These tools' executor results are inherently state-dependent even with gate fields present.
# Includes tools whose _GATE_SCHEMAS valid values are ghost entities (always fail) or
# whose outcome depends on VM/snapshot/profile/network state.
_STATE_DEP_GATED = {
    "launch_vm", "stop_vm", "set_resource_limits", "open_display",
    "open_shell", "snapshot_create", "resize_disk", "send_monitor_cmd",
    "update_config",
    # Ghost-entity tools — executor always fails because the entity doesn't exist;
    # the gate test is about confirming the gate PASSES (doesn't double-ask)
    "delete_vm", "delete_profile", "delete_network",
    "snapshot_delete", "snapshot_restore", "add_vm_to_network",
    # create_profile fails without hardware fields (gate only requires profile_name+description);
    # clone_vm fails if the source VM is running ("stop source VM before cloning") — both state-dep
    "create_profile", "clone_vm",
}
# These return custom dict shapes without a "success" key
_KEY_ONLY_GATED = {
    "vm_status", "monitor_vm", "get_vm_logs", "check_profile_compatibility",
}


import random as _random


def generate_random_gated_tests(n: int = 30, seed: Optional[int] = None) -> List[PipelineTest]:
    """
    Generate n randomised Layer 9 tests.

    For each test:
      · Pick a gated tool
      · req_mode = all | partial | none (how many gate-required fields to provide)
      · All provided fields use valid values

    Expected outcome:
      · req=all  → passthrough or exec (state-dep if applicable)
      · req<all  → gated, and expect_missing = exactly the omitted fields

    Anti-double-ask: every GATED test's expected_missing is computed from
    what we actually omitted, so the runner can verify the gate asks for
    exactly those fields and nothing that was provided.
    """
    rng   = _random.Random(seed)
    tests: List[PipelineTest] = []
    tools = list(_GATE_SCHEMAS.keys())
    uid   = 0

    while len(tests) < n:
        tool    = rng.choice(tools)
        schema  = _GATE_SCHEMAS[tool]
        req_keys = list(schema.keys())

        req_mode = rng.choice(["all", "all", "partial", "none"])
        if req_mode == "all":
            included = req_keys
        elif req_mode == "partial" and len(req_keys) > 1:
            included = rng.sample(req_keys, max(1, len(req_keys) - 1))
        else:
            included = []

        omitted = [k for k in req_keys if k not in included]

        args: Dict[str, Any] = {}
        for field in included:
            fdef = schema[field]
            val  = rng.choice(fdef["valid"])
            if "{uid}" in str(val):
                uid += 1
                val = val.replace("{uid}", f"{uid:04d}")
            args[field] = val

        uid += 1
        tid = f"p9_rand_{tool}_{uid:04d}"

        if omitted:
            # GATED test — anti-double-ask: expect gate asks for exactly omitted fields
            tests.append(PipelineTest(
                id=tid,
                tags=["gated_pipeline", "random", "gated", tool,
                      f"req={req_mode}", f"omit={omitted}"],
                description=(
                    f"{tool} | req={req_mode} | omitted={omitted}"
                    + (f" | provided={list(args.keys())}" if args else "")
                ),
                tool=tool, input_args=args,
                category="gated",
                expect_success=False,
                expect_layer="gate",
                expect_result_keys=omitted,   # gate must ask for ONLY these
            ))
        else:
            # PASSTHROUGH or EXEC test
            is_state = tool in _STATE_DEP_GATED
            is_key   = tool in _KEY_ONLY_GATED
            exp_s    = None if (is_state or is_key) else True
            exp_l    = None if is_state else "ok"
            cat      = "passthrough"
            tests.append(PipelineTest(
                id=tid,
                tags=["gated_pipeline", "random", cat, tool, f"req=all"],
                description=f"{tool} | all gate fields provided",
                tool=tool, input_args=args,
                category=cat,
                expect_success=exp_s,
                expect_layer=exp_l,
            ))

    return tests

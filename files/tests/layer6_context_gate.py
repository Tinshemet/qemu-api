"""
tests/layer6_context_gate.py — Layer 6: Context Gate unit tests.

Tests gate_check() in isolation — pure function, no AI, instant.
Covers fixed cases for all major tools plus randomised tests generated
from the gate config by randomly omitting required fields.
"""

import json, pathlib, random, time, traceback
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

from .shared import TestResult

# Load config the same way context_gate.py does
_CONFIG_PATH = pathlib.Path(__file__).parents[1] / "shared" / "sanitizer" / "context_gate_config.json"
with _CONFIG_PATH.open() as _f:
    _GATE_CONFIG: Dict[str, List] = json.load(_f)

from shared.sanitizer.context_gate import gate_check


# ─────────────────────────────────────────────
#  DATACLASS
# ─────────────────────────────────────────────

@dataclass
class GateTest:
    id:             str
    tags:           List[str]
    description:    str
    tool:           str
    args:           Dict[str, Any]
    expect_blocked: bool      = False
    expect_missing: List[str] = field(default_factory=list)


# ─────────────────────────────────────────────
#  FIXED GATE TEST CASES
# ─────────────────────────────────────────────

GATE_TESTS: List[GateTest] = [

    # ── Tools with no gate config → always pass ──
    GateTest(
        id="gate_list_vms_no_gate",
        tags=["gate","passthrough"],
        description="list_vms has no gated args — always passes",
        tool="list_vms", args={}, expect_blocked=False,
    ),
    GateTest(
        id="gate_check_system_no_gate",
        tags=["gate","passthrough"],
        description="check_system has no gated args — always passes",
        tool="check_system", args={}, expect_blocked=False,
    ),
    GateTest(
        id="gate_scan_isos_no_gate",
        tags=["gate","passthrough"],
        description="scan_isos has no gated args — always passes",
        tool="scan_isos", args={}, expect_blocked=False,
    ),
    GateTest(
        id="gate_list_profiles_no_gate",
        tags=["gate","passthrough"],
        description="list_profiles has no gated args — always passes",
        tool="list_profiles", args={}, expect_blocked=False,
    ),
    GateTest(
        id="gate_list_networks_no_gate",
        tags=["gate","passthrough"],
        description="list_networks has no gated args — always passes",
        tool="list_networks", args={}, expect_blocked=False,
    ),
    GateTest(
        id="gate_unknown_tool_passes",
        tags=["gate","passthrough"],
        description="Tool not in config is always passed through",
        tool="nonexistent_tool", args={}, expect_blocked=False,
    ),

    # ── create_vm: name + os_type ────────────────
    GateTest(
        id="gate_create_vm_complete",
        tags=["gate","create_vm"],
        description="create_vm with all required args passes",
        tool="create_vm",
        args={"name": "dev-box", "os_type": "linux"},
        expect_blocked=False,
    ),
    GateTest(
        id="gate_create_vm_missing_both",
        tags=["gate","create_vm","missing"],
        description="create_vm with no args blocked — both fields reported",
        tool="create_vm", args={}, expect_blocked=True,
        expect_missing=["name", "os_type"],
    ),
    GateTest(
        id="gate_create_vm_missing_name",
        tags=["gate","create_vm","missing"],
        description="create_vm with os_type only — name missing",
        tool="create_vm", args={"os_type": "linux"}, expect_blocked=True,
        expect_missing=["name"],
    ),
    GateTest(
        id="gate_create_vm_missing_os_type",
        tags=["gate","create_vm","missing"],
        description="create_vm with name only — os_type missing",
        tool="create_vm", args={"name": "dev-box"}, expect_blocked=True,
        expect_missing=["os_type"],
    ),
    GateTest(
        id="gate_create_vm_whitespace_name_blocked",
        tags=["gate","create_vm","missing"],
        description="create_vm with whitespace-only name is blocked",
        tool="create_vm", args={"name": "   ", "os_type": "linux"}, expect_blocked=True,
        expect_missing=["name"],
    ),

    # ── clone_vm: source_name + new_name ─────────
    GateTest(
        id="gate_clone_vm_complete",
        tags=["gate","clone_vm"],
        description="clone_vm with all required args passes",
        tool="clone_vm", args={"source_name": "origin", "new_name": "copy"},
        expect_blocked=False,
    ),
    GateTest(
        id="gate_clone_vm_missing_new_name",
        tags=["gate","clone_vm","missing"],
        description="clone_vm without new_name is blocked",
        tool="clone_vm", args={"source_name": "origin"}, expect_blocked=True,
        expect_missing=["new_name"],
    ),
    GateTest(
        id="gate_clone_vm_missing_both",
        tags=["gate","clone_vm","missing"],
        description="clone_vm with no args blocked — both fields reported",
        tool="clone_vm", args={}, expect_blocked=True,
        expect_missing=["source_name", "new_name"],
    ),

    # ── Single-name tools: launch, stop, delete, status, monitor, logs ──
    GateTest(
        id="gate_launch_vm_complete",
        tags=["gate","launch_vm"],
        description="launch_vm with name passes",
        tool="launch_vm", args={"name": "dev-box"}, expect_blocked=False,
    ),
    GateTest(
        id="gate_launch_vm_missing_name",
        tags=["gate","launch_vm","missing"],
        description="launch_vm without name is blocked",
        tool="launch_vm", args={}, expect_blocked=True,
        expect_missing=["name"],
    ),
    GateTest(
        id="gate_stop_vm_complete",
        tags=["gate","stop_vm"],
        description="stop_vm with name passes",
        tool="stop_vm", args={"name": "dev-box"}, expect_blocked=False,
    ),
    GateTest(
        id="gate_stop_vm_missing_name",
        tags=["gate","stop_vm","missing"],
        description="stop_vm without name is blocked",
        tool="stop_vm", args={}, expect_blocked=True,
        expect_missing=["name"],
    ),
    GateTest(
        id="gate_delete_vm_complete",
        tags=["gate","delete_vm"],
        description="delete_vm with name passes gate (preflight handles confirmation)",
        tool="delete_vm", args={"name": "dev-box"}, expect_blocked=False,
    ),
    GateTest(
        id="gate_delete_vm_missing_name",
        tags=["gate","delete_vm","missing"],
        description="delete_vm without name is blocked",
        tool="delete_vm", args={}, expect_blocked=True,
        expect_missing=["name"],
    ),
    GateTest(
        id="gate_vm_status_complete",
        tags=["gate","vm_status"],
        description="vm_status with name passes",
        tool="vm_status", args={"name": "dev-box"}, expect_blocked=False,
    ),
    GateTest(
        id="gate_monitor_vm_missing_name",
        tags=["gate","monitor_vm","missing"],
        description="monitor_vm without name is blocked",
        tool="monitor_vm", args={}, expect_blocked=True,
        expect_missing=["name"],
    ),
    GateTest(
        id="gate_get_vm_logs_missing_name",
        tags=["gate","get_vm_logs","missing"],
        description="get_vm_logs without name is blocked",
        tool="get_vm_logs", args={}, expect_blocked=True,
        expect_missing=["name"],
    ),

    # ── Config tools ──────────────────────────────
    GateTest(
        id="gate_show_config_complete",
        tags=["gate","show_config"],
        description="show_config with name passes",
        tool="show_config", args={"name": "dev-box"}, expect_blocked=False,
    ),
    GateTest(
        id="gate_update_config_missing_name",
        tags=["gate","update_config","missing"],
        description="update_config without name is blocked",
        tool="update_config", args={}, expect_blocked=True,
        expect_missing=["name"],
    ),
    GateTest(
        id="gate_print_command_missing_name",
        tags=["gate","print_command","missing"],
        description="print_command without name is blocked",
        tool="print_command", args={}, expect_blocked=True,
        expect_missing=["name"],
    ),

    # ── resize_disk: name + new_size_gb ──────────
    GateTest(
        id="gate_resize_disk_complete",
        tags=["gate","resize_disk"],
        description="resize_disk with all required args passes",
        tool="resize_disk", args={"name": "dev-box", "new_size_gb": 100},
        expect_blocked=False,
    ),
    GateTest(
        id="gate_resize_disk_missing_size",
        tags=["gate","resize_disk","missing"],
        description="resize_disk without new_size_gb is blocked",
        tool="resize_disk", args={"name": "dev-box"}, expect_blocked=True,
        expect_missing=["new_size_gb"],
    ),
    GateTest(
        id="gate_resize_disk_missing_both",
        tags=["gate","resize_disk","missing"],
        description="resize_disk with no args blocked",
        tool="resize_disk", args={}, expect_blocked=True,
        expect_missing=["name", "new_size_gb"],
    ),

    # ── Snapshot tools ────────────────────────────
    GateTest(
        id="gate_snapshot_create_complete",
        tags=["gate","snapshot"],
        description="snapshot_create with all required args passes",
        tool="snapshot_create", args={"name": "dev-box", "snap_name": "pre-update"},
        expect_blocked=False,
    ),
    GateTest(
        id="gate_snapshot_create_missing_snap_name",
        tags=["gate","snapshot","missing"],
        description="snapshot_create without snap_name is blocked",
        tool="snapshot_create", args={"name": "dev-box"}, expect_blocked=True,
        expect_missing=["snap_name"],
    ),
    GateTest(
        id="gate_snapshot_list_missing_name",
        tags=["gate","snapshot","missing"],
        description="snapshot_list without name is blocked",
        tool="snapshot_list", args={}, expect_blocked=True,
        expect_missing=["name"],
    ),
    GateTest(
        id="gate_snapshot_restore_complete",
        tags=["gate","snapshot"],
        description="snapshot_restore with all required args passes",
        tool="snapshot_restore", args={"name": "dev-box", "snap_name": "pre-update"},
        expect_blocked=False,
    ),
    GateTest(
        id="gate_snapshot_restore_missing_snap_name",
        tags=["gate","snapshot","missing"],
        description="snapshot_restore without snap_name is blocked",
        tool="snapshot_restore", args={"name": "dev-box"}, expect_blocked=True,
        expect_missing=["snap_name"],
    ),
    GateTest(
        id="gate_snapshot_delete_missing_both",
        tags=["gate","snapshot","missing"],
        description="snapshot_delete with no args blocked",
        tool="snapshot_delete", args={}, expect_blocked=True,
        expect_missing=["name", "snap_name"],
    ),

    # ── send_monitor_cmd: name + cmd ─────────────
    GateTest(
        id="gate_send_monitor_complete",
        tags=["gate","send_monitor_cmd"],
        description="send_monitor_cmd with all required args passes",
        tool="send_monitor_cmd", args={"name": "dev-box", "cmd": "info status"},
        expect_blocked=False,
    ),
    GateTest(
        id="gate_send_monitor_missing_cmd",
        tags=["gate","send_monitor_cmd","missing"],
        description="send_monitor_cmd without cmd is blocked",
        tool="send_monitor_cmd", args={"name": "dev-box"}, expect_blocked=True,
        expect_missing=["cmd"],
    ),
    GateTest(
        id="gate_send_monitor_missing_both",
        tags=["gate","send_monitor_cmd","missing"],
        description="send_monitor_cmd with no args blocked",
        tool="send_monitor_cmd", args={}, expect_blocked=True,
        expect_missing=["name", "cmd"],
    ),

    # ── Profile tools ─────────────────────────────
    GateTest(
        id="gate_check_profile_compat_complete",
        tags=["gate","profile"],
        description="check_profile_compatibility with profile_name passes",
        tool="check_profile_compatibility", args={"profile_name": "raspberry_pi_3b"},
        expect_blocked=False,
    ),
    GateTest(
        id="gate_check_profile_compat_missing",
        tags=["gate","profile","missing"],
        description="check_profile_compatibility without profile_name is blocked",
        tool="check_profile_compatibility", args={}, expect_blocked=True,
        expect_missing=["profile_name"],
    ),
    GateTest(
        id="gate_create_profile_complete",
        tags=["gate","profile"],
        description="create_profile with all required args passes",
        tool="create_profile", args={"profile_name": "my-profile", "description": "test"},
        expect_blocked=False,
    ),
    GateTest(
        id="gate_create_profile_missing_description",
        tags=["gate","profile","missing"],
        description="create_profile without description is blocked",
        tool="create_profile", args={"profile_name": "my-profile"}, expect_blocked=True,
        expect_missing=["description"],
    ),
    GateTest(
        id="gate_delete_profile_missing",
        tags=["gate","profile","missing"],
        description="delete_profile without profile_name is blocked",
        tool="delete_profile", args={}, expect_blocked=True,
        expect_missing=["profile_name"],
    ),

    # ── Network tools ─────────────────────────────
    GateTest(
        id="gate_create_network_complete",
        tags=["gate","network"],
        description="create_network with net_name passes",
        tool="create_network", args={"net_name": "lab-net"}, expect_blocked=False,
    ),
    GateTest(
        id="gate_create_network_missing",
        tags=["gate","network","missing"],
        description="create_network without net_name is blocked",
        tool="create_network", args={}, expect_blocked=True,
        expect_missing=["net_name"],
    ),
    GateTest(
        id="gate_delete_network_missing",
        tags=["gate","network","missing"],
        description="delete_network without net_name is blocked",
        tool="delete_network", args={}, expect_blocked=True,
        expect_missing=["net_name"],
    ),
    GateTest(
        id="gate_add_vm_to_network_complete",
        tags=["gate","network"],
        description="add_vm_to_network with all required args passes",
        tool="add_vm_to_network", args={"net_name": "lab-net", "vm_name": "dev-box"},
        expect_blocked=False,
    ),
    GateTest(
        id="gate_add_vm_to_network_missing_vm",
        tags=["gate","network","missing"],
        description="add_vm_to_network without vm_name is blocked",
        tool="add_vm_to_network", args={"net_name": "lab-net"}, expect_blocked=True,
        expect_missing=["vm_name"],
    ),
    GateTest(
        id="gate_add_vm_to_network_missing_both",
        tags=["gate","network","missing"],
        description="add_vm_to_network with no args blocked",
        tool="add_vm_to_network", args={}, expect_blocked=True,
        expect_missing=["net_name", "vm_name"],
    ),

    # ── Remaining single-name tools ───────────────
    GateTest(
        id="gate_set_resource_limits_missing",
        tags=["gate","missing"],
        description="set_resource_limits without name is blocked",
        tool="set_resource_limits", args={}, expect_blocked=True,
        expect_missing=["name"],
    ),
    GateTest(
        id="gate_open_display_missing",
        tags=["gate","missing"],
        description="open_display without name is blocked",
        tool="open_display", args={}, expect_blocked=True,
        expect_missing=["name"],
    ),
    GateTest(
        id="gate_open_shell_missing",
        tags=["gate","missing"],
        description="open_shell without name is blocked",
        tool="open_shell", args={}, expect_blocked=True,
        expect_missing=["name"],
    ),
]


# ─────────────────────────────────────────────
#  RANDOMISED GATE TESTS
# ─────────────────────────────────────────────

# Canonical sample values used when building complete/partial arg sets
_SAMPLE_VALUES: Dict[str, Any] = {
    "name":         "test-vm",
    "os_type":      "linux",
    "source_name":  "origin-vm",
    "new_name":     "clone-vm",
    "snap_name":    "checkpoint",
    "new_size_gb":  100,
    "cmd":          "info status",
    "profile_name": "minimal",
    "description":  "a test profile",
    "net_name":     "lab-net",
    "vm_name":      "dev-box",
}


def generate_random_gate_tests(n: int = 20, seed: Optional[int] = None) -> List[GateTest]:
    """
    Auto-generate gate tests from the config by randomly omitting required fields.

    For each iteration alternates between:
    - A "complete" test (all required fields present → gate passes)
    - A "partial" test (1-all required fields dropped → gate blocks)
    """
    rng   = random.Random(seed)
    tools = list(_GATE_CONFIG.keys())
    tests: List[GateTest] = []
    idx   = 0

    while len(tests) < n:
        tool    = tools[idx % len(tools)]
        fields  = [entry[0] for entry in _GATE_CONFIG[tool]]
        seq     = len(tests)

        if seq % 2 == 0:
            # Complete args → gate should pass
            args = {f: _SAMPLE_VALUES.get(f, f"val_{f}") for f in fields}
            tests.append(GateTest(
                id=f"gate_rand_{tool}_{seq:03d}_complete",
                tags=["random", "gate", "complete", tool.replace("_", "-")],
                description=f"All {len(fields)} required field(s) present for {tool}",
                tool=tool, args=args, expect_blocked=False,
            ))
        else:
            # Drop a random non-empty subset of required fields → gate should block
            n_drop  = rng.randint(1, len(fields))
            dropped = rng.sample(fields, n_drop)
            args    = {f: _SAMPLE_VALUES.get(f, f"val_{f}") for f in fields if f not in dropped}
            tests.append(GateTest(
                id=f"gate_rand_{tool}_{seq:03d}_missing",
                tags=["random", "gate", "missing", tool.replace("_", "-")],
                description=f"{tool} missing {dropped} — gate must block",
                tool=tool, args=args,
                expect_blocked=True, expect_missing=dropped,
            ))

        idx += 1

    return tests[:n]


# ─────────────────────────────────────────────
#  LAYER 6 RUNNER
# ─────────────────────────────────────────────

def run_gate_test(tc: GateTest) -> TestResult:
    start  = time.time()
    issues: List[str] = []
    detail: Dict[str, Any] = {}
    try:
        result = gate_check(tc.tool, tc.args)
        detail["gate_result"] = result

        if tc.expect_blocked:
            if result is None:
                issues.append(
                    f"gate_check({tc.tool!r}, {tc.args}) returned None "
                    f"but expected block on fields {tc.expect_missing}"
                )
            else:
                reported = [m["field"] for m in result.get("missing", [])]
                detail["missing_reported"] = reported
                for f in tc.expect_missing:
                    if f not in reported:
                        issues.append(
                            f"Expected '{f}' in missing list, got {reported}"
                        )
                if not result.get("clarify"):
                    issues.append("Gate result missing 'clarify': True")
                if result.get("success") is not False:
                    issues.append("Gate result should have 'success': False")
        else:
            if result is not None:
                issues.append(
                    f"gate_check({tc.tool!r}, {tc.args}) unexpectedly blocked: {result}"
                )

    except Exception:
        issues.append(f"Exception: {traceback.format_exc()}")

    return TestResult(
        test_id=tc.id, layer=6, passed=len(issues) == 0,
        issues=issues, fixes_applied=[],
        duration_s=time.time() - start, detail=detail,
    )

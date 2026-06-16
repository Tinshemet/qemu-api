"""
tests/layer2_executor.py — Layer 2: Executor / preflight unit tests (no AI needed).
"""

import io, os, contextlib, traceback, time, uuid, random
from typing import List, Optional

from .shared import (
    ExecutorTest, TestResult,
    _preflight_check, execute_tool,
    REAL_HOME,
)


def _uid() -> str:
    """Short unique suffix for test VM names to avoid leftover conflicts."""
    return uuid.uuid4().hex[:6]


# ─────────────────────────────────────────────
#  EXECUTOR TEST CASES
# ─────────────────────────────────────────────

EXECUTOR_TESTS: List[ExecutorTest] = [

    # ── Pre-flight: VM names ──────────────────
    ExecutorTest(
        id="preflight_placeholder_windows_vm",
        tags=["preflight","name"],
        description="'windows-vm' triggers ask_user",
        tool="create_vm",
        input_args={"name": "windows-vm", "os_type": "windows"},
        expect_preflight="ask_user",
    ),
    ExecutorTest(
        id="preflight_placeholder_my_vm",
        tags=["preflight","name"],
        description="'my-vm' triggers ask_user",
        tool="create_vm",
        input_args={"name": "my-vm", "os_type": "linux"},
        expect_preflight="ask_user",
    ),
    ExecutorTest(
        id="preflight_empty_name_clarify",
        tags=["preflight","name"],
        description="Missing name returns clarify response",
        tool="create_vm",
        input_args={"os_type": "linux"},
        expect_clarify=True,
        expect_success=False,
    ),
    ExecutorTest(
        id="preflight_valid_name_ok",
        tags=["preflight","name"],
        description="A real descriptive name passes (unique suffix avoids conflicts)",
        tool="create_vm",
        input_args={"name": f"pf-valid-{_uid()}", "os_type": "linux"},
        expect_preflight="ok",
    ),

    # ── Pre-flight: machine type ──────────────
    ExecutorTest(
        id="preflight_profile_machine_type_auto_fix",
        tags=["preflight","machine_type","hallucination"],
        description="dell_g15_5520 as machine_type → auto_fix",
        tool="create_vm",
        input_args={"name": f"pf-mt-{_uid()}", "machine_type": "dell_g15_5520", "os_type": "linux"},
        expect_preflight="auto_fix",
    ),
    ExecutorTest(
        id="preflight_office_laptop_machine_type",
        tags=["preflight","machine_type","hallucination"],
        description="office_laptop as machine_type → auto_fix",
        tool="create_vm",
        input_args={"name": f"pf-ol-{_uid()}", "machine_type": "office_laptop", "os_type": "linux"},
        expect_preflight="auto_fix",
    ),

    # ── Pre-flight: destructive operations ────
    ExecutorTest(
        id="preflight_delete_asks_user",
        tags=["preflight","delete"],
        description="delete_vm always asks confirmation",
        tool="delete_vm",
        input_args={"name": "any-vm-name"},
        expect_preflight="ask_user",
    ),
    ExecutorTest(
        id="preflight_snapshot_restore_asks",
        tags=["preflight","snapshot"],
        description="snapshot_restore asks confirmation",
        tool="snapshot_restore",
        input_args={"name": "myvm", "snap_name": "snap1"},
        expect_preflight="ask_user",
    ),
    ExecutorTest(
        id="preflight_snapshot_delete_asks",
        tags=["preflight","snapshot"],
        description="snapshot_delete asks confirmation",
        tool="snapshot_delete",
        input_args={"name": "myvm", "snap_name": "snap1"},
        expect_preflight="ask_user",
    ),
    ExecutorTest(
        id="preflight_disk_shrink_aborted",
        tags=["preflight","disk"],
        description="Disk resize on non-existent VM → abort",
        tool="resize_disk",
        input_args={"name": "nonexistent-xyz-99999", "new_size_gb": 1},
        expect_preflight="abort",
    ),
    ExecutorTest(
        id="preflight_dangerous_monitor_cmd",
        tags=["preflight","monitor"],
        description="'quit' monitor command triggers ask_user",
        tool="send_monitor_cmd",
        input_args={"name": "myvm", "cmd": "quit"},
        expect_preflight="ask_user",
    ),
    ExecutorTest(
        id="preflight_safe_monitor_cmd",
        tags=["preflight","monitor"],
        description="'info status' safe monitor command passes",
        tool="send_monitor_cmd",
        input_args={"name": "myvm", "cmd": "info status"},
        expect_preflight="ok",
    ),

    # ── Pre-flight: profile validation ────────
    ExecutorTest(
        id="preflight_minimal_profile_ok",
        tags=["preflight","profile"],
        description="minimal profile with fresh name passes pre-flight",
        tool="create_vm",
        input_args={"name": f"pf-min-{_uid()}", "profile": "minimal", "os_type": "linux"},
        expect_preflight="ok",
    ),
    ExecutorTest(
        id="preflight_raspi_kvm_ok",
        tags=["preflight","profile","raspi","arm"],
        description="raspi3b: sanitiser already fixed kvm before preflight, so preflight returns ok",
        tool="create_vm",
        input_args={"name": f"pf-rpi-{_uid()}", "profile": "raspberry_pi_3b", "kvm": False},
        expect_preflight="ok",
    ),

    # ── Internet validator: local QEMU ────────
    ExecutorTest(
        id="internet_arm_cpu_x86_caught",
        tags=["internet","cpu","arch"],
        description="ARM CPU on x86 VM caught — returns ask_user (error severity)",
        tool="create_vm",
        input_args={"name": f"pf-cpu-{_uid()}", "cpu_model": "cortex-a72", "machine_arch": "x86_64", "os_type": "linux"},
        expect_preflight="ask_user",
    ),
    ExecutorTest(
        id="internet_valid_q35_ok",
        tags=["internet","machine_type"],
        description="q35 passes QEMU machine type check",
        tool="create_vm",
        input_args={"name": f"pf-q35-{_uid()}", "machine_type": "q35", "os_type": "linux"},
        expect_preflight="ok",
    ),
    ExecutorTest(
        id="internet_arm64_iso_x86_vm_blocked",
        tags=["internet","iso","arch"],
        description="ARM64 ISO filename + x86 VM → ask_user (file must exist on disk)",
        tool="create_vm",
        input_args={
            "name":         f"pf-iso-{_uid()}",
            "iso_path":     f"{REAL_HOME}/Desktop/Images/Win11_25H2_EnglishInternational_Arm64_v2.iso",
            "machine_arch": "x86_64",
            "os_type":      "windows",
        },
        expect_preflight="ask_user",
    ),

    # ── Basic executor tools ──────────────────
    ExecutorTest(
        id="executor_list_vms_ok",
        tags=["executor","basic"],
        description="list_vms returns a list",
        tool="list_vms",
        input_args={},
        expect_success=True,
    ),
    ExecutorTest(
        id="executor_list_profiles_ok",
        tags=["executor","basic"],
        description="list_profiles returns known profiles",
        tool="list_profiles",
        input_args={},
        expect_success=True,
    ),
    ExecutorTest(
        id="executor_check_system_ok",
        tags=["executor","basic"],
        description="check_system returns capability keys",
        tool="check_system",
        input_args={},
        expect_result_keys=["kvm_available","qemu_installed","host_cpu"],
    ),
    ExecutorTest(
        id="executor_scan_isos_ok",
        tags=["executor","iso"],
        description="scan_isos returns a list",
        tool="scan_isos",
        input_args={},
        expect_success=True,
    ),
    ExecutorTest(
        id="executor_raspi_compat_check",
        tags=["executor","raspi","compat"],
        description="raspberry_pi_3b compat check returns expected keys",
        tool="check_profile_compatibility",
        input_args={"profile_name": "raspberry_pi_3b"},
        expect_result_keys=["compatible","warnings","host_summary"],
    ),
    ExecutorTest(
        id="executor_minimal_compat_check",
        tags=["executor","profile","compat"],
        description="minimal profile compat check passes",
        tool="check_profile_compatibility",
        input_args={"profile_name": "minimal"},
        expect_result_keys=["compatible","host_summary"],
    ),

    # ── New tools (revert / check_disk / fingerprint_vm) ──
    ExecutorTest(
        id="executor_revert_nothing_to_revert",
        tags=["executor","revert"],
        description="revert with no prior action returns success=False immediately (no prompt)",
        tool="revert",
        input_args={},
        expect_success=False,
    ),
    ExecutorTest(
        id="executor_check_disk_nonexistent",
        tags=["executor","check_disk"],
        description="check_disk on a VM that doesn't exist returns success=False",
        tool="check_disk",
        input_args={"name": "nonexistent-vm-xyz-99999"},
        expect_success=False,
    ),
    ExecutorTest(
        id="executor_fingerprint_vm_nonexistent",
        tags=["executor","fingerprint"],
        description="fingerprint_vm on a VM that doesn't exist returns success=False",
        tool="fingerprint_vm",
        input_args={"name": "nonexistent-vm-xyz-99999", "summary": True},
        expect_success=False,
    ),
]


# ─────────────────────────────────────────────
#  RANDOMISED PREFLIGHT TESTS
# ─────────────────────────────────────────────

_PF_PLACEHOLDER_NAMES = [
    "windows-vm", "linux-vm", "my-vm", "vm", "ubuntu-vm",
    "new-vm", "test-vm", "default-vm", "example-vm",
]

_PF_DESTRUCTIVE_TOOLS = ["delete_vm", "snapshot_restore", "snapshot_delete"]

_PF_SAFE_MONITOR_CMDS = ["info status", "info block", "info network", "info kvm", "info pci"]

_PF_DANGEROUS_MONITOR_CMDS = ["quit", "system_reset", "system_powerdown", "drive_del"]

_PF_CATEGORIES = ["placeholder", "destructive", "safe_monitor", "dangerous_monitor"]


def generate_random_preflight_tests(n: int = 5, seed: Optional[int] = None) -> List[ExecutorTest]:
    """
    Generate n randomised preflight tests covering:
    - Placeholder VM names that should always trigger ask_user
    - Destructive operations (delete, snapshot_restore/delete) that require confirmation
    - Safe monitor commands that should pass through
    - Dangerous monitor commands that require confirmation
    """
    rng   = random.Random(seed)
    tests: List[ExecutorTest] = []

    for i in range(n):
        category = _PF_CATEGORIES[i % len(_PF_CATEGORIES)]

        if category == "placeholder":
            name = rng.choice(_PF_PLACEHOLDER_NAMES)
            tests.append(ExecutorTest(
                id=f"pf_rand_placeholder_{i:03d}",
                tags=["random", "preflight", "name", "placeholder"],
                description=f"Placeholder name '{name}' always triggers ask_user",
                tool="create_vm",
                input_args={"name": name, "os_type": rng.choice(["linux", "windows"])},
                expect_preflight="ask_user",
            ))

        elif category == "destructive":
            tool = rng.choice(_PF_DESTRUCTIVE_TOOLS)
            vm   = f"rand-vm-{i:03d}"
            if tool == "delete_vm":
                args = {"name": vm}
            else:
                args = {"name": vm, "snap_name": rng.choice(["snap1", "baseline", "backup"])}
            tests.append(ExecutorTest(
                id=f"pf_rand_destructive_{i:03d}",
                tags=["random", "preflight", "destructive"],
                description=f"Destructive {tool} always triggers ask_user",
                tool=tool,
                input_args=args,
                expect_preflight="ask_user",
            ))

        elif category == "safe_monitor":
            cmd = rng.choice(_PF_SAFE_MONITOR_CMDS)
            tests.append(ExecutorTest(
                id=f"pf_rand_safe_monitor_{i:03d}",
                tags=["random", "preflight", "monitor", "safe"],
                description=f"Safe monitor cmd '{cmd}' passes preflight",
                tool="send_monitor_cmd",
                input_args={"name": f"rand-vm-{i:03d}", "cmd": cmd},
                expect_preflight="ok",
            ))

        else:  # dangerous_monitor
            cmd = rng.choice(_PF_DANGEROUS_MONITOR_CMDS)
            tests.append(ExecutorTest(
                id=f"pf_rand_dangerous_monitor_{i:03d}",
                tags=["random", "preflight", "monitor", "dangerous"],
                description=f"Dangerous monitor cmd '{cmd}' triggers ask_user",
                tool="send_monitor_cmd",
                input_args={"name": f"rand-vm-{i:03d}", "cmd": cmd},
                expect_preflight="ask_user",
            ))

    return tests


# ─────────────────────────────────────────────
#  LAYER 2 RUNNER
# ─────────────────────────────────────────────

def run_executor_test(tc: ExecutorTest) -> TestResult:
    start  = time.time()
    issues: List[str] = []
    fixes:  List[str] = []
    try:
        args = dict(tc.input_args)

        if tc.expect_preflight is not None:
            pf     = _preflight_check(tc.tool, args, [], verbose=False)
            actual = pf.get("action", "ok")
            if actual != tc.expect_preflight:
                issues.append(
                    f"Pre-flight: expected '{tc.expect_preflight}' got '{actual}'"
                    + (f" (reason: {pf.get('reason','')})" if pf.get("reason") else "")
                )

        if tc.expect_preflight in ("ask_user","abort"):
            return TestResult(test_id=tc.id, layer=2, passed=len(issues)==0,
                              issues=issues, fixes_applied=fixes, duration_s=time.time()-start)

        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
            result = execute_tool(tc.tool, args, verbose=False)

        if tc.expect_clarify:
            if not (isinstance(result, dict) and result.get("clarify")):
                issues.append(f"Expected clarify response got: {result}")

        if tc.expect_success is not None:
            actual_ok = result.get("success", True) if isinstance(result, dict) else True
            if actual_ok != tc.expect_success:
                issues.append(f"Expected success={tc.expect_success} got {actual_ok}"
                               + (f" ({result.get('error','')})" if isinstance(result,dict) else ""))

        if isinstance(result, dict):
            for k in tc.expect_result_keys:
                if k not in result:
                    issues.append(f"Result missing key '{k}'")

    except Exception:
        issues.append(f"Exception: {traceback.format_exc()}")
    finally:
        vm_name = tc.input_args.get("name","")
        if vm_name and tc.tool == "create_vm":
            vm_dir = os.path.join(os.path.expanduser("~"), ".qemu_vms", vm_name)
            if os.path.exists(vm_dir):
                import shutil as _shutil
                _shutil.rmtree(vm_dir, ignore_errors=True)

    return TestResult(test_id=tc.id, layer=2, passed=len(issues)==0,
                      issues=issues, fixes_applied=fixes, duration_s=time.time()-start)

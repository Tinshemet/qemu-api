"""
tests/layer5_property.py — Layer 5: Property-based invariant tests (no AI, no network).
"""

import random, time
from typing import List

from .shared import (
    TestResult,
    _sanitise_args, _preflight_check,
    VALID_MACHINE_TYPES,
    get_all_profiles,
)


# ─────────────────────────────────────────────
#  LAYER 5 RUNNER
# ─────────────────────────────────────────────

def run_property_tests(iterations: int = 50) -> List[TestResult]:
    """
    Property-based tests using random inputs.
    Does NOT require hypothesis — uses Python's random module.
    Tests invariants that must hold for ALL valid inputs.
    """
    results: List[TestResult] = []

    def _make_result(test_id, passed, issues, duration):
        return TestResult(test_id=test_id, layer=5, passed=passed,
                          issues=issues, fixes_applied=[], duration_s=duration)

    # ── Property 1: Sanitiser never crashes ──────────────────────────────────
    start  = time.time()
    issues = []
    for i in range(iterations):
        args = {
            "name":         random.choice(["test","my-vm","","linux-vm","dev-box-01"]),
            "os_type":      random.choice(["linux","windows","ubuntu","win11","osx","invalid"]),
            "memory_mb":    random.choice([512, "8192", 999999, "8GB", 0, -1]),
            "cpu_cores":    random.choice([1, 4, 128, "4", 0]),
            "disk_size_gb": random.choice([10, 60, "60", 2, 0]),
            "kvm":          random.choice([True, False, "true", "false", "yes"]),
            "machine_type": random.choice(["q35","pc","dell_g15_5520","raspi3b","invalid"]),
            "network_mode": random.choice(["nat","NAT","bridge","BRIDGE","wifi","invalid"]),
            "display":      random.choice(["sdl","SDL","vnc","x11","invalid"]),
            "bridge_iface": random.choice(["virbr0","eth0","ens33","wlan0",""]),
            "mac_address":  random.choice(["AA:BB:CC:DD:EE:FF","AA:BB:CC:DD:EE:FF:11","","invalid"]),
            "iso_path":     random.choice(["","/path/to/iso.iso","/home/user/test.iso","scan_isos()[0]"]),
        }
        try:
            result = _sanitise_args("create_vm", dict(args))
            if not isinstance(result, dict):
                issues.append(f"iter {i}: sanitiser returned non-dict: {type(result)}")
            nm = result.get("network_mode")
            if nm and nm not in ("nat","bridge","none"):
                issues.append(f"iter {i}: invalid network_mode after sanitise: {nm!r}")
            ot = result.get("os_type")
            if ot and ot not in ("linux","windows","macos","other"):
                issues.append(f"iter {i}: invalid os_type after sanitise: {ot!r}")
            kvm = result.get("kvm")
            if kvm is not None and not isinstance(kvm, bool):
                issues.append(f"iter {i}: kvm is not bool after sanitise: {type(kvm)}")
            mem = result.get("memory_mb")
            if mem is not None and not isinstance(mem, int):
                issues.append(f"iter {i}: memory_mb is not int: {type(mem)}")
            if result.get("machine_arch") == "aarch64" and result.get("kvm") is True:
                issues.append(f"iter {i}: aarch64 VM has kvm=True after sanitise")
        except Exception as e:
            issues.append(f"iter {i}: CRASH: {e}")

    results.append(_make_result(
        "prop_sanitiser_never_crashes",
        len(issues) == 0,
        issues[:5],
        time.time() - start,
    ))

    # ── Property 2: Preflight never crashes ──────────────────────────────────
    start  = time.time()
    issues = []
    tools  = ["create_vm","launch_vm","delete_vm","resize_disk","send_monitor_cmd"]
    for i in range(iterations):
        tool = random.choice(tools)
        args = {
            "name":        random.choice(["test","my-vm","","dev-box-99"]),
            "os_type":     random.choice(["linux","windows",""]),
            "new_size_gb": random.choice([1, 60, 999]),
            "cmd":         random.choice(["info status","quit","system_reset","ls"]),
        }
        try:
            result = _preflight_check(tool, args, [], verbose=False)
            if not isinstance(result, dict):
                issues.append(f"iter {i}: preflight returned non-dict: {type(result)}")
            if "action" not in result:
                issues.append(f"iter {i}: preflight missing 'action' key")
            if result.get("action") not in ("ok","auto_fix","ask_user","abort"):
                issues.append(f"iter {i}: invalid action: {result.get('action')!r}")
        except Exception as e:
            issues.append(f"iter {i}: CRASH in preflight({tool}): {e}")

    results.append(_make_result(
        "prop_preflight_never_crashes",
        len(issues) == 0,
        issues[:5],
        time.time() - start,
    ))

    # ── Property 3: Sanitiser is idempotent ──────────────────────────────────
    start  = time.time()
    issues = []
    for i in range(iterations):
        args = {
            "name":         random.choice(["dev-box","linux-vm","test","my-vm-01"]),
            "os_type":      random.choice(["linux","ubuntu","windows","win11"]),
            "memory_mb":    random.choice([4096, "8192", 2048]),
            "network_mode": random.choice(["nat","NAT","bridge"]),
            "kvm":          random.choice([True, "true", False]),
        }
        try:
            once  = _sanitise_args("create_vm", dict(args))
            twice = _sanitise_args("create_vm", dict(once))
            for k in ("os_type","network_mode","kvm","memory_mb"):
                if k in once and k in twice:
                    if str(once[k]) != str(twice[k]):
                        issues.append(f"iter {i}: not idempotent — {k}: {once[k]!r} → {twice[k]!r}")
        except Exception as e:
            issues.append(f"iter {i}: CRASH: {e}")

    results.append(_make_result(
        "prop_sanitiser_idempotent",
        len(issues) == 0,
        issues[:5],
        time.time() - start,
    ))

    # ── Property 4: Placeholder names always cleared ─────────────────────────
    start  = time.time()
    issues = []
    placeholders = [
        "windows-vm","linux-vm","ubuntu-vm","my-vm","vm","myvm",
        "new-vm","unnamed","windows_vm","linux_vm","ubuntu_vm",
        "my_vm","new_vm","virtual-machine","virtual_machine",
    ]
    for name in placeholders:
        try:
            result = _sanitise_args("create_vm", {"name": name, "os_type": "linux"})
            if result.get("name"):
                issues.append(f"Placeholder '{name}' not cleared: got '{result['name']}'")
        except Exception as e:
            issues.append(f"CRASH on '{name}': {e}")

    results.append(_make_result(
        "prop_placeholders_always_cleared",
        len(issues) == 0,
        issues,
        time.time() - start,
    ))

    # ── Property 5: Profile auto-set from known profile names ─────────────────
    start  = time.time()
    issues = []
    all_profiles = list(get_all_profiles().keys())
    for pname in all_profiles:
        if pname in VALID_MACHINE_TYPES:
            continue
        try:
            result = _sanitise_args("create_vm", {"name": "test", "machine_type": pname})
            if "machine_type" in result and result["machine_type"] == pname:
                issues.append(f"Profile '{pname}' not stripped from machine_type")
            if result.get("profile") != pname:
                issues.append(f"Profile '{pname}' not auto-set: got '{result.get('profile')}'")
        except Exception as e:
            issues.append(f"CRASH on profile '{pname}': {e}")

    results.append(_make_result(
        "prop_profile_always_auto_set",
        len(issues) == 0,
        issues,
        time.time() - start,
    ))

    return results

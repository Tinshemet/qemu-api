"""
tests/layer4_profiles.py — Layer 4: Random profile tests with preflight/HTTP validation.
"""

import random, string, time, traceback
from typing import Any, Dict, List, Optional, Tuple

from .shared import (
    ProfileTest, TestResult,
    _validate_profile_for_host, _validate_with_internet,
    get_all_profiles, save_custom_profile, delete_custom_profile,
)


def _rand_name(prefix: str = "test") -> str:
    return f"{prefix}-{''.join(random.choices(string.ascii_lowercase, k=6))}"


# ─────────────────────────────────────────────
#  PROFILE DATA GENERATORS
# ─────────────────────────────────────────────

def _generate_valid_x86_profile() -> Dict[str, Any]:
    return {
        "description":   "Random x86 test profile",
        "machine_class": random.choice(["desktop","laptop","server"]),
        "machine_type":  random.choice(["q35","pc"]),
        "machine_arch":  "x86_64",
        "qemu_binary":   "qemu-system-x86_64",
        "cpu_model":     random.choice(["host","kvm64","Haswell","Skylake","IceLake"]),
        "cpu_cores":     random.randint(1, 8),
        "cpu_threads":   random.choice([1, 2]),
        "memory_mb":     random.choice([1024, 2048, 4096, 8192]),
        "gpu":           random.choice(["virtio","qxl","vga","none"]),
        "audio":         random.choice(["hda","ich9","none"]),
        "display":       random.choice(["sdl","gtk","none"]),
        "battery":       random.choice([True, False]),
        "uefi":          random.choice([True, False]),
        "bios":          "ovmf",
        "kvm":           True,
        "hugepages":     False,
        "manufacturer":  random.choice(["Dell Inc.","Lenovo","HP","ASUS","Custom"]),
        "product_name":  random.choice(["OptiPlex 7090","ThinkPad E14","ProBook 450",""]),
    }


def _generate_arm_profile() -> Dict[str, Any]:
    return {
        "description":   "Random ARM test profile",
        "machine_class": "custom",
        "machine_type":  random.choice(["virt","raspi3b"]),
        "machine_arch":  "aarch64",
        "qemu_binary":   "qemu-system-aarch64",
        "cpu_model":     random.choice(["cortex-a72","cortex-a53"]),
        "cpu_cores":     random.randint(1, 4),
        "cpu_threads":   1,
        "memory_mb":     random.choice([512, 1024, 2048]),
        "gpu":           "none",
        "audio":         "none",
        "display":       "none",
        "uefi":          False,
        "bios":          "seabios",
        "kvm":           False,
        "hugepages":     False,
        "manufacturer":  "Raspberry Pi Foundation",
        "product_name":  "Raspberry Pi 4 Model B",
    }


def _generate_broken_profile() -> Tuple[Dict[str, Any], List[str]]:
    """Return (profile_data, expected_issue_keywords)."""
    broken_type = random.choice([
        "arm_cpu_x86", "hugepages_no_alloc", "excessive_ram",
        "wrong_binary_for_arch", "raspi_with_kvm",
    ])
    if broken_type == "arm_cpu_x86":
        p = _generate_valid_x86_profile()
        p["cpu_model"] = random.choice(["cortex-a72","cortex-a53","cortex-a15"])
        return p, ["arm", "x86_64"]
    elif broken_type == "hugepages_no_alloc":
        p = _generate_valid_x86_profile()
        p["hugepages"] = True
        return p, ["hugepages"]
    elif broken_type == "excessive_ram":
        p = _generate_valid_x86_profile()
        p["memory_mb"] = 999999
        return p, ["ram", "31779"]
    elif broken_type == "wrong_binary_for_arch":
        p = _generate_arm_profile()
        p["qemu_binary"] = "qemu-system-x86_64"
        p["machine_type"] = "virt"
        p["machine_arch"] = "aarch64"
        p["kvm"] = True
        return p, ["not supported", "kvm"]
    else:  # raspi_with_kvm
        p = _generate_arm_profile()
        p["machine_type"] = "raspi3b"
        p["kvm"]          = True
        return p, ["kvm", "arm"]


# ─────────────────────────────────────────────
#  PROFILE TEST CASE GENERATOR
# ─────────────────────────────────────────────

def _generate_profile_tests(n: int = 5, seed: Optional[int] = None) -> List[ProfileTest]:
    """Generate N random ProfileTest objects for Layer 4."""
    random.seed(seed)
    tests: List[ProfileTest] = [
        ProfileTest(
            id="profile_builtin_minimal_valid",
            tags=["profile","builtin","compat"],
            description="minimal profile — fully compatible",
            profile_name="minimal", profile_data={},
            expect_no_issues=True, expect_qemu_check=True, cleanup=False,
        ),
        ProfileTest(
            id="profile_builtin_server_hugepages",
            tags=["profile","builtin","hugepages"],
            description="server profile — hugepages check",
            profile_name="server", profile_data={},
            expect_issues=["hugepages"], expect_auto_fix=True,
            expect_qemu_check=True, cleanup=False,
        ),
        ProfileTest(
            id="profile_builtin_raspi_arm_warnings",
            tags=["profile","builtin","raspi","arm"],
            description="raspi3b — QEMU marks it unsupported on x86 binary",
            profile_name="raspberry_pi_3b", profile_data={},
            expect_issues=["not supported"],
            expect_qemu_check=False, cleanup=False,
        ),
        ProfileTest(
            id="profile_http_product_lookup",
            tags=["profile","internet","http"],
            description="Real Dell product — DuckDuckGo verify",
            profile_name=_rand_name("http-test"),
            profile_data={
                "description": "HTTP test", "machine_class": "laptop",
                "machine_type": "q35", "machine_arch": "x86_64",
                "qemu_binary": "qemu-system-x86_64", "cpu_model": "host",
                "cpu_cores": 4, "cpu_threads": 2, "memory_mb": 8192,
                "gpu": "virtio", "audio": "hda", "display": "sdl",
                "uefi": True, "bios": "ovmf", "kvm": True, "hugepages": False,
                "manufacturer": "Dell Inc.", "product_name": "XPS 15 9520",
            },
            expect_http_check=True, expect_qemu_check=True, cleanup=True,
        ),
        ProfileTest(
            id="profile_http_fake_product",
            tags=["profile","internet","http","hallucination"],
            description="Made-up product — may warn",
            profile_name=_rand_name("fake-prod"),
            profile_data={
                "description": "Fake product test", "machine_class": "desktop",
                "machine_type": "q35", "machine_arch": "x86_64",
                "qemu_binary": "qemu-system-x86_64", "cpu_model": "host",
                "cpu_cores": 2, "cpu_threads": 1, "memory_mb": 4096,
                "gpu": "vga", "audio": "none", "display": "none",
                "uefi": False, "bios": "seabios", "kvm": True, "hugepages": False,
                "manufacturer": "FakeCompany Inc.",
                "product_name": "NonExistentModel XZ-9999",
            },
            expect_http_check=True, cleanup=True,
        ),
        ProfileTest(
            id="profile_arm_cpu_on_x86_custom",
            tags=["profile","custom","cpu","hallucination"],
            description="Custom profile: ARM CPU on x86 — must be caught",
            profile_name=_rand_name("bad-cpu"),
            profile_data={
                "description": "ARM CPU on x86 test", "machine_class": "custom",
                "machine_type": "q35", "machine_arch": "x86_64",
                "qemu_binary": "qemu-system-x86_64", "cpu_model": "cortex-a72",
                "cpu_cores": 4, "cpu_threads": 2, "memory_mb": 4096,
                "gpu": "virtio", "audio": "none", "display": "sdl",
                "uefi": True, "bios": "ovmf", "kvm": True, "hugepages": False,
            },
            expect_issues=["arm", "x86_64"], expect_auto_fix=True,
            expect_qemu_check=True, cleanup=True,
        ),
        ProfileTest(
            id="profile_qemu_machine_type_check",
            tags=["profile","internet","machine_type"],
            description="Invalid machine type caught by QEMU",
            profile_name=_rand_name("bad-mt"),
            profile_data={
                "description": "Invalid machine type test", "machine_class": "desktop",
                "machine_type": "invalid-machine-xyz", "machine_arch": "x86_64",
                "qemu_binary": "qemu-system-x86_64", "cpu_model": "host",
                "cpu_cores": 2, "cpu_threads": 1, "memory_mb": 2048,
                "gpu": "none", "audio": "none", "display": "none",
                "uefi": False, "bios": "seabios", "kvm": True, "hugepages": False,
            },
            expect_issues=["not supported"], expect_qemu_check=True, cleanup=True,
        ),
        ProfileTest(
            id="profile_qemu_cpu_model_check",
            tags=["profile","internet","cpu"],
            description="Invalid CPU model caught by QEMU",
            profile_name=_rand_name("bad-cpu2"),
            profile_data={
                "description": "Invalid CPU model test", "machine_class": "desktop",
                "machine_type": "q35", "machine_arch": "x86_64",
                "qemu_binary": "qemu-system-x86_64",
                "cpu_model": "NonExistentCPU-v99",
                "cpu_cores": 2, "cpu_threads": 1, "memory_mb": 2048,
                "gpu": "none", "audio": "none", "display": "none",
                "uefi": False, "bios": "seabios", "kvm": True, "hugepages": False,
            },
            expect_issues=["not found"], expect_qemu_check=True, cleanup=True,
        ),
    ]

    for i in range(n):
        kind = random.choice(["valid_x86", "arm", "broken"])
        if kind == "valid_x86":
            tests.append(ProfileTest(
                id=f"profile_rand_x86_{i:02d}",
                tags=["profile","random","x86"],
                description=f"Random valid x86 profile #{i}",
                profile_name=_rand_name("rand-x86"),
                profile_data=_generate_valid_x86_profile(),
                expect_no_issues=False, expect_qemu_check=True, cleanup=True,
            ))
        elif kind == "arm":
            tests.append(ProfileTest(
                id=f"profile_rand_arm_{i:02d}",
                tags=["profile","random","arm"],
                description=f"Random ARM profile #{i}",
                profile_name=_rand_name("rand-arm"),
                profile_data=_generate_arm_profile(),
                expect_issues=["not supported"],
                expect_qemu_check=False, cleanup=True,
            ))
        else:
            pdata, expected = _generate_broken_profile()
            tests.append(ProfileTest(
                id=f"profile_rand_broken_{i:02d}",
                tags=["profile","random","broken"],
                description=f"Random broken profile #{i}",
                profile_name=_rand_name("rand-broken"),
                profile_data=pdata,
                expect_issues=expected,
                expect_auto_fix=True, expect_qemu_check=True, cleanup=True,
            ))

    return tests


# ─────────────────────────────────────────────
#  LAYER 4 RUNNER
# ─────────────────────────────────────────────

def run_profile_test(tc: ProfileTest) -> TestResult:
    start  = time.time()
    issues: List[str] = []
    fixes:  List[str] = []
    detail = {}
    actual_name = tc.profile_name

    try:
        if tc.profile_data:
            result = save_custom_profile(tc.profile_name, dict(tc.profile_data))
            if not result.get("success"):
                return TestResult(test_id=tc.id, layer=4, passed=False,
                                  issues=[f"Failed to save profile: {result}"],
                                  fixes_applied=[], duration_s=time.time()-start)
            actual_name = result.get("profile_name", tc.profile_name)

        profile_issues  = _validate_profile_for_host(actual_name)
        all_profiles    = get_all_profiles()
        profile         = all_profiles.get(actual_name, tc.profile_data)
        vm_args = {
            "name":         f"test-{actual_name[:10]}",
            "os_type":      "linux",
            "profile":      actual_name,
            "cpu_model":    profile.get("cpu_model", "host"),
            "machine_type": profile.get("machine_type", "q35"),
            "machine_arch": profile.get("machine_arch", "x86_64"),
            "memory_mb":    profile.get("memory_mb", 2048),
            "manufacturer": profile.get("manufacturer", ""),
            "product_name": profile.get("product_name", ""),
            "qemu_binary":  profile.get("qemu_binary", "qemu-system-x86_64"),
            "hugepages":    profile.get("hugepages", False),
        }
        internet_issues = _validate_with_internet(vm_args, verbose=False)
        all_issues      = profile_issues + internet_issues
        all_messages    = " ".join(i.get("message","").lower() for i in all_issues)
        detail["all_messages"] = all_messages
        detail["issue_count"]  = len(all_issues)

        for issue in all_issues:
            if issue.get("auto_fix"):
                fixes.append(f"{issue.get('fix_field','?')}: {issue.get('message','')[:60]}")

        if tc.expect_no_issues:
            hard = [i for i in all_issues if i.get("severity") == "error"]
            for h in hard:
                issues.append(f"Expected no issues but got ERROR: {h.get('message','')}")

        for kw in tc.expect_issues:
            if kw.lower() not in all_messages:
                issues.append(
                    f"Expected keyword '{kw}' not found in: "
                    f"{[i.get('message','')[:60] for i in all_issues]}"
                )

        if tc.expect_auto_fix:
            if not [i for i in all_issues if i.get("auto_fix")]:
                issues.append("Expected at least one auto-fixable issue but none found")

    except Exception:
        issues.append(f"Exception: {traceback.format_exc()}")
    finally:
        if tc.cleanup and tc.profile_data:
            try: delete_custom_profile(tc.profile_name)
            except: pass

    return TestResult(test_id=tc.id, layer=4, passed=len(issues)==0,
                      issues=issues, fixes_applied=fixes,
                      duration_s=time.time()-start, detail=detail)

"""
tests/layer1_sanitizer.py — Layer 1: Sanitiser unit tests (pure, no AI, instant).
"""

import json, time, traceback
from typing import List

from .shared import (
    SanitiserTest, TestResult,
    _sanitise_args,
    REAL_HOME,
)


# ─────────────────────────────────────────────
#  SANITISER TEST CASES
# ─────────────────────────────────────────────

SANITISER_TESTS: List[SanitiserTest] = [

    # ── VM names ──────────────────────────────
    SanitiserTest(
        id="name_hyphen_preserved",
        tags=["name"],
        description="Hyphens in VM names preserved",
        tool="create_vm",
        input_args={"name": "my-test-vm", "os_type": "linux"},
        expect_fields={"name": "my-test-vm"},
        unchanged_fields=["name"],
    ),
    SanitiserTest(
        id="name_spaces_to_underscore",
        tags=["name","hallucination"],
        description="Spaces → underscores",
        tool="create_vm",
        input_args={"name": "my test vm", "os_type": "linux"},
        expect_fields={"name": "my_test_vm"},
        changed_fields=["name"],
    ),
    SanitiserTest(
        id="name_placeholder_windows_vm",
        tags=["name","hallucination"],
        description="'windows-vm' cleared (hyphenated placeholder)",
        tool="create_vm",
        input_args={"name": "windows-vm", "os_type": "windows"},
        expect_fields={"name": ""},
        changed_fields=["name"],
    ),
    SanitiserTest(
        id="name_placeholder_linux_vm",
        tags=["name","hallucination"],
        description="'linux-vm' cleared (hyphenated placeholder)",
        tool="create_vm",
        input_args={"name": "linux-vm", "os_type": "linux"},
        expect_fields={"name": ""},
        changed_fields=["name"],
    ),
    SanitiserTest(
        id="name_placeholder_my_vm",
        tags=["name","hallucination"],
        description="'my-vm' cleared (hyphenated placeholder)",
        tool="create_vm",
        input_args={"name": "my-vm", "os_type": "linux"},
        expect_fields={"name": ""},
        changed_fields=["name"],
    ),
    SanitiserTest(
        id="name_vm_single_word",
        tags=["name","hallucination"],
        description="'vm' alone cleared",
        tool="create_vm",
        input_args={"name": "vm", "os_type": "linux"},
        expect_fields={"name": ""},
        changed_fields=["name"],
    ),
    SanitiserTest(
        id="name_placeholder_ubuntu_vm",
        tags=["name","hallucination"],
        description="'ubuntu-vm' cleared",
        tool="create_vm",
        input_args={"name": "ubuntu-vm", "os_type": "linux"},
        expect_fields={"name": ""},
        changed_fields=["name"],
    ),
    SanitiserTest(
        id="name_placeholder_new_vm",
        tags=["name","hallucination"],
        description="'new-vm' cleared",
        tool="create_vm",
        input_args={"name": "new-vm", "os_type": "linux"},
        expect_fields={"name": ""},
        changed_fields=["name"],
    ),
    SanitiserTest(
        id="name_real_name_preserved",
        tags=["name"],
        description="Real descriptive name preserved",
        tool="create_vm",
        input_args={"name": "dev-server-01", "os_type": "linux"},
        expect_fields={"name": "dev-server-01"},
        unchanged_fields=["name"],
    ),

    # ── OS type aliases ───────────────────────
    SanitiserTest(
        id="os_ubuntu_to_linux",
        tags=["os_type","hallucination"],
        description="'ubuntu' → 'linux'",
        tool="create_vm",
        input_args={"name": "test", "os_type": "ubuntu"},
        expect_fields={"os_type": "linux"},
        changed_fields=["os_type"],
    ),
    SanitiserTest(
        id="os_Ubuntu_caps_to_linux",
        tags=["os_type","hallucination"],
        description="'Ubuntu' → 'linux'",
        tool="create_vm",
        input_args={"name": "test", "os_type": "Ubuntu"},
        expect_fields={"os_type": "linux"},
        changed_fields=["os_type"],
    ),
    SanitiserTest(
        id="os_win11_to_windows",
        tags=["os_type","hallucination"],
        description="'win11' → 'windows'",
        tool="create_vm",
        input_args={"name": "test", "os_type": "win11"},
        expect_fields={"os_type": "windows"},
        changed_fields=["os_type"],
    ),
    SanitiserTest(
        id="os_win64_to_windows",
        tags=["os_type","hallucination"],
        description="'win64' → 'windows'",
        tool="create_vm",
        input_args={"name": "test", "os_type": "win64"},
        expect_fields={"os_type": "windows"},
        changed_fields=["os_type"],
    ),
    SanitiserTest(
        id="os_windows11_to_windows",
        tags=["os_type","hallucination"],
        description="'windows11' → 'windows'",
        tool="create_vm",
        input_args={"name": "test", "os_type": "windows11"},
        expect_fields={"os_type": "windows"},
        changed_fields=["os_type"],
    ),
    SanitiserTest(
        id="os_osx_to_macos",
        tags=["os_type","hallucination"],
        description="'osx' → 'macos'",
        tool="create_vm",
        input_args={"name": "test", "os_type": "osx"},
        expect_fields={"os_type": "macos"},
        changed_fields=["os_type"],
    ),
    SanitiserTest(
        id="os_linux_preserved",
        tags=["os_type"],
        description="'linux' unchanged",
        tool="create_vm",
        input_args={"name": "test", "os_type": "linux"},
        expect_fields={"os_type": "linux"},
        unchanged_fields=["os_type"],
    ),
    SanitiserTest(
        id="os_windows_preserved",
        tags=["os_type"],
        description="'windows' unchanged",
        tool="create_vm",
        input_args={"name": "test", "os_type": "windows"},
        expect_fields={"os_type": "windows"},
        unchanged_fields=["os_type"],
    ),

    # ── Enum case normalisation ───────────────
    SanitiserTest(
        id="enum_NAT_to_nat",
        tags=["network","case"],
        description="'NAT' → 'nat'",
        tool="create_vm",
        input_args={"name": "test", "network_mode": "NAT"},
        expect_fields={"network_mode": "nat"},
        changed_fields=["network_mode"],
    ),
    SanitiserTest(
        id="enum_BRIDGE_to_bridge",
        tags=["network","case"],
        description="'BRIDGE' → 'bridge'",
        tool="create_vm",
        input_args={"name": "test", "network_mode": "BRIDGE"},
        expect_fields={"network_mode": "bridge"},
        changed_fields=["network_mode"],
    ),
    SanitiserTest(
        id="enum_SDL_to_sdl",
        tags=["display","case"],
        description="'SDL' → 'sdl'",
        tool="create_vm",
        input_args={"name": "test", "display": "SDL"},
        expect_fields={"display": "sdl"},
        changed_fields=["display"],
    ),
    SanitiserTest(
        id="enum_VNC_to_vnc",
        tags=["display","case"],
        description="'VNC' → 'vnc'",
        tool="create_vm",
        input_args={"name": "test", "display": "VNC"},
        expect_fields={"display": "vnc"},
        changed_fields=["display"],
    ),
    SanitiserTest(
        id="enum_invalid_display_defaulted",
        tags=["display","hallucination"],
        description="Invalid 'x11' → 'sdl'",
        tool="create_vm",
        input_args={"name": "test", "display": "x11"},
        expect_fields={"display": "sdl"},
        changed_fields=["display"],
    ),
    SanitiserTest(
        id="enum_audio_alsa_defaulted",
        tags=["audio","hallucination"],
        description="'alsa' → 'hda'",
        tool="create_vm",
        input_args={"name": "test", "audio": "alsa"},
        expect_fields={"audio": "hda"},
        changed_fields=["audio"],
    ),
    SanitiserTest(
        id="enum_audio_default_string",
        tags=["audio","hallucination"],
        description="'default' → 'hda'",
        tool="create_vm",
        input_args={"name": "test", "audio": "default"},
        expect_fields={"audio": "hda"},
        changed_fields=["audio"],
    ),

    # ── Machine type ──────────────────────────
    SanitiserTest(
        id="machine_type_q35_valid",
        tags=["machine_type"],
        description="q35 preserved",
        tool="create_vm",
        input_args={"name": "test", "machine_type": "q35"},
        expect_fields={"machine_type": "q35"},
        unchanged_fields=["machine_type"],
    ),
    SanitiserTest(
        id="machine_type_dell_profile_auto_set",
        tags=["machine_type","profile","hallucination"],
        description="dell_g15_5520 as machine_type → profile set, machine_type removed",
        tool="create_vm",
        input_args={"name": "test", "machine_type": "dell_g15_5520"},
        removed_fields=["machine_type"],
        expect_fields={"profile": "dell_g15_5520"},
        changed_fields=["profile"],
    ),
    SanitiserTest(
        id="machine_type_office_laptop_profile_auto_set",
        tags=["machine_type","profile","hallucination"],
        description="office_laptop as machine_type → profile set",
        tool="create_vm",
        input_args={"name": "test", "machine_type": "office_laptop"},
        removed_fields=["machine_type"],
        expect_fields={"profile": "office_laptop"},
    ),
    SanitiserTest(
        id="machine_type_gaming_desktop_auto_set",
        tags=["machine_type","profile","hallucination"],
        description="gaming_desktop as machine_type → profile set",
        tool="create_vm",
        input_args={"name": "test", "machine_type": "gaming_desktop"},
        removed_fields=["machine_type"],
        expect_fields={"profile": "gaming_desktop"},
    ),
    SanitiserTest(
        id="machine_type_server_stripped",
        tags=["machine_type","hallucination"],
        description="'server' stripped (no exact profile match)",
        tool="create_vm",
        input_args={"name": "test", "machine_type": "server"},
        removed_fields=["machine_type"],
    ),
    SanitiserTest(
        id="machine_type_lenovo_stripped",
        tags=["machine_type","hallucination"],
        description="'lenovo_laptop' stripped",
        tool="create_vm",
        input_args={"name": "test", "machine_type": "lenovo_laptop"},
        removed_fields=["machine_type"],
    ),

    # ── CPU models ────────────────────────────
    SanitiserTest(
        id="cpu_cortex_a53_x86_fixed",
        tags=["cpu","arch","hallucination"],
        description="cortex-a53 on x86 → host",
        tool="create_vm",
        input_args={"name": "test", "cpu_model": "cortex-a53", "machine_arch": "x86_64"},
        expect_fields={"cpu_model": "host"},
        changed_fields=["cpu_model"],
    ),
    SanitiserTest(
        id="cpu_cortex_a72_x86_fixed",
        tags=["cpu","arch","hallucination"],
        description="cortex-a72 on x86 → host",
        tool="create_vm",
        input_args={"name": "test", "cpu_model": "cortex-a72"},
        expect_fields={"cpu_model": "host"},
        changed_fields=["cpu_model"],
    ),
    SanitiserTest(
        id="cpu_cortex_a15_x86_fixed",
        tags=["cpu","arch","hallucination"],
        description="cortex-a15 on x86 → host",
        tool="create_vm",
        input_args={"name": "test", "cpu_model": "cortex-a15", "machine_arch": "x86_64"},
        expect_fields={"cpu_model": "host"},
        changed_fields=["cpu_model"],
    ),
    SanitiserTest(
        id="cpu_host_preserved",
        tags=["cpu"],
        description="'host' preserved",
        tool="create_vm",
        input_args={"name": "test", "cpu_model": "host"},
        expect_fields={"cpu_model": "host"},
        unchanged_fields=["cpu_model"],
    ),
    SanitiserTest(
        id="cpu_arm_on_aarch64_preserved",
        tags=["cpu","arm"],
        description="cortex-a72 on aarch64 preserved",
        tool="create_vm",
        input_args={"name": "test", "cpu_model": "cortex-a72", "machine_arch": "aarch64"},
        expect_fields={"cpu_model": "cortex-a72"},
        unchanged_fields=["cpu_model"],
    ),

    # ── Networking ────────────────────────────
    SanitiserTest(
        id="bridge_eth0_to_virbr0",
        tags=["network","bridge","hallucination"],
        description="eth0 → virbr0",
        tool="create_vm",
        input_args={"name": "test", "bridge_iface": "eth0"},
        expect_fields={"bridge_iface": "virbr0"},
        changed_fields=["bridge_iface"],
    ),
    SanitiserTest(
        id="bridge_ens33_to_virbr0",
        tags=["network","bridge","hallucination"],
        description="ens33 → virbr0",
        tool="create_vm",
        input_args={"name": "test", "bridge_iface": "ens33"},
        expect_fields={"bridge_iface": "virbr0"},
        changed_fields=["bridge_iface"],
    ),
    SanitiserTest(
        id="bridge_wlan0_to_virbr0",
        tags=["network","bridge","hallucination"],
        description="wlan0 → virbr0",
        tool="create_vm",
        input_args={"name": "test", "bridge_iface": "wlan0"},
        expect_fields={"bridge_iface": "virbr0"},
        changed_fields=["bridge_iface"],
    ),
    SanitiserTest(
        id="bridge_virbr0_preserved",
        tags=["network","bridge"],
        description="virbr0 preserved",
        tool="create_vm",
        input_args={"name": "test", "bridge_iface": "virbr0"},
        expect_fields={"bridge_iface": "virbr0"},
        unchanged_fields=["bridge_iface"],
    ),
    SanitiserTest(
        id="mac_7octet_stripped",
        tags=["network","mac","hallucination"],
        description="7-octet MAC stripped",
        tool="create_vm",
        input_args={"name": "test", "mac_address": "AA:BB:CC:DD:EE:FF:11"},
        removed_fields=["mac_address"],
    ),
    SanitiserTest(
        id="mac_valid_preserved",
        tags=["network","mac"],
        description="Valid 6-octet MAC preserved",
        tool="create_vm",
        input_args={"name": "test", "mac_address": "AA:BB:CC:DD:EE:FF"},
        expect_fields={"mac_address": "AA:BB:CC:DD:EE:FF"},
        unchanged_fields=["mac_address"],
    ),

    # ── Resources ─────────────────────────────
    SanitiserTest(
        id="memory_excessive_capped",
        tags=["resources","hallucination"],
        description="512000MB capped to ≤95% host RAM",
        tool="create_vm",
        input_args={"name": "test", "memory_mb": 512000},
        changed_fields=["memory_mb"],
    ),
    SanitiserTest(
        id="memory_string_coerced",
        tags=["resources","type_coercion"],
        description="'8192' string coerced to int 8192",
        tool="create_vm",
        input_args={"name": "test", "memory_mb": "8192"},
        expect_fields={"memory_mb": 8192},
    ),
    SanitiserTest(
        id="memory_8gb_string_coerced",
        tags=["resources","type_coercion"],
        description="'8GB' stripped to integer",
        tool="create_vm",
        input_args={"name": "test", "memory_mb": "8GB"},
        changed_fields=["memory_mb"],
    ),
    SanitiserTest(
        id="cpu_cores_excessive_capped",
        tags=["resources","hallucination"],
        description="128 cores capped to host count",
        tool="create_vm",
        input_args={"name": "test", "cpu_cores": 128},
        changed_fields=["cpu_cores"],
    ),
    SanitiserTest(
        id="disk_size_string_coerced",
        tags=["resources","type_coercion"],
        description="'60' string coerced to int 60",
        tool="create_vm",
        input_args={"name": "test", "disk_size_gb": "60"},
        expect_fields={"disk_size_gb": 60},
    ),
    SanitiserTest(
        id="disk_size_min_enforced",
        tags=["resources"],
        description="disk < 8GB raised to 8GB",
        tool="create_vm",
        input_args={"name": "test", "disk_size_gb": 2},
        expect_fields={"disk_size_gb": 8},
        changed_fields=["disk_size_gb"],
    ),

    # ── ISO paths ─────────────────────────────
    SanitiserTest(
        id="iso_wrong_user_fixed",
        tags=["iso","paths","hallucination"],
        description="/home/user/ → real home",
        tool="create_vm",
        input_args={"name": "test", "iso_path": "/home/user/Desktop/Images/ubuntu.iso"},
        changed_fields=["iso_path"],
    ),
    SanitiserTest(
        id="iso_path_to_placeholder_removed",
        tags=["iso","paths","hallucination"],
        description="/path/to/ removed",
        tool="create_vm",
        input_args={"name": "test", "iso_path": "/path/to/ubuntu.iso"},
        removed_fields=["iso_path"],
    ),
    SanitiserTest(
        id="iso_scan_isos_literal_removed",
        tags=["iso","paths","hallucination"],
        description="scan_isos()[0] literal removed (not resolved to real ISO)",
        tool="create_vm",
        input_args={"name": "test", "iso_path": "/home/user/Desktop/Images/scan_isos()[0]"},
        removed_fields=["iso_path"],
    ),
    SanitiserTest(
        id="iso_angle_bracket_removed",
        tags=["iso","paths","hallucination"],
        description="<iso> placeholder removed",
        tool="create_vm",
        input_args={"name": "test", "iso_path": "/home/user/<iso>"},
        removed_fields=["iso_path"],
    ),

    # ── Boolean coercion ──────────────────────
    SanitiserTest(
        id="bool_true_string_coerced",
        tags=["type_coercion"],
        description="'true' → True",
        tool="create_vm",
        input_args={"name": "test", "kvm": "true"},
        expect_fields={"kvm": True},
        changed_fields=["kvm"],
    ),
    SanitiserTest(
        id="bool_false_string_coerced",
        tags=["type_coercion"],
        description="'false' → False",
        tool="create_vm",
        input_args={"name": "test", "hugepages": "false"},
        expect_fields={"hugepages": False},
        changed_fields=["hugepages"],
    ),
    SanitiserTest(
        id="bool_yes_string_coerced",
        tags=["type_coercion"],
        description="'yes' → True",
        tool="create_vm",
        input_args={"name": "test", "kvm": "yes"},
        expect_fields={"kvm": True},
        changed_fields=["kvm"],
    ),

    # ── Raspi/ARM ─────────────────────────────
    SanitiserTest(
        id="raspi_kvm_forced_off",
        tags=["raspi","arm","kvm"],
        description="raspi3b machine_type forces kvm=False",
        tool="create_vm",
        input_args={"name": "test", "machine_type": "raspi3b", "kvm": True},
        expect_fields={"kvm": False},
        changed_fields=["kvm"],
    ),
    SanitiserTest(
        id="aarch64_kvm_forced_off",
        tags=["arm","kvm"],
        description="aarch64 arch forces kvm=False",
        tool="create_vm",
        input_args={"name": "test", "machine_arch": "aarch64", "kvm": True},
        expect_fields={"kvm": False},
        changed_fields=["kvm"],
    ),

    # ── Profile auto-detection ────────────────
    SanitiserTest(
        id="profile_valid_machine_type_preserved",
        tags=["profile","machine_type"],
        description="Valid q35 NOT treated as profile",
        tool="create_vm",
        input_args={"name": "test", "machine_type": "q35"},
        expect_fields={"machine_type": "q35"},
        unchanged_fields=["machine_type"],
    ),

    # ── disk_format / disk_bus confusion ─────
    SanitiserTest(
        id="disk_format_bus_name_promoted",
        tags=["disk","hallucination"],
        description="Bus name passed as disk_format is promoted to disk_bus",
        tool="create_vm",
        input_args={"name": "test", "os_type": "linux", "disk_format": "sata"},
        expect_fields={"disk_bus": "sata"},
        removed_fields=["disk_format"],
    ),
    SanitiserTest(
        id="disk_format_nvme_promoted",
        tags=["disk","hallucination"],
        description="'nvme' as disk_format is promoted to disk_bus",
        tool="create_vm",
        input_args={"name": "test", "os_type": "linux", "disk_format": "nvme"},
        expect_fields={"disk_bus": "nvme"},
        removed_fields=["disk_format"],
    ),
    SanitiserTest(
        id="disk_format_qcow2_preserved",
        tags=["disk"],
        description="Legitimate disk_format 'qcow2' is not touched",
        tool="create_vm",
        input_args={"name": "test", "os_type": "linux", "disk_format": "qcow2"},
        expect_fields={"disk_format": "qcow2"},
        unchanged_fields=["disk_format"],
    ),

    # ── Empty field cleanup ───────────────────
    SanitiserTest(
        id="empty_optional_fields_removed",
        tags=["cleanup"],
        description="Empty optional fields removed",
        tool="create_vm",
        input_args={
            "name": "test",
            "manufacturer": "", "product_name": "",
            "mac_address": "", "iso_path": "", "bridge_iface": "",
        },
        removed_fields=["manufacturer","product_name","mac_address","iso_path"],
    ),
]


# ─────────────────────────────────────────────
#  LAYER 1 RUNNER
# ─────────────────────────────────────────────

def run_sanitiser_test(tc: SanitiserTest) -> TestResult:
    start     = time.time()
    issues: List[str] = []
    fixes:  List[str] = []
    original  = json.loads(json.dumps(tc.input_args))
    sanitised = {}
    try:
        sanitised = _sanitise_args(tc.tool, dict(tc.input_args))

        for k, v in tc.expect_fields.items():
            actual = sanitised.get(k)
            if actual != v and str(actual) != str(v):
                issues.append(f"Expected {k}={v!r} got {k}={actual!r}")

        for k in tc.removed_fields:
            if k in sanitised and sanitised[k] not in (None, "", [], {}):
                issues.append(f"'{k}' should be removed but got {sanitised[k]!r}")

        for k in tc.changed_fields:
            if k in original:
                orig_v = original[k]
                san_v  = sanitised.get(k)
                if san_v == orig_v and type(san_v) == type(orig_v):
                    issues.append(f"'{k}' should have changed but stayed as {orig_v!r}")
            elif k not in sanitised:
                issues.append(f"Expected changed field '{k}' not in result")

        for k in tc.unchanged_fields:
            if original.get(k) != sanitised.get(k):
                issues.append(f"'{k}' should not change: {original.get(k)!r} → {sanitised.get(k)!r}")

        for k in original:
            if k in sanitised and original[k] != sanitised[k]:
                fixes.append(f"{k}: {original[k]!r}→{sanitised[k]!r}")
            elif k not in sanitised:
                fixes.append(f"removed {k}: {original[k]!r}")

    except Exception:
        issues.append(f"Exception: {traceback.format_exc()}")

    return TestResult(
        test_id=tc.id, layer=1, passed=len(issues)==0,
        issues=issues, fixes_applied=fixes,
        duration_s=time.time()-start,
        detail={"original": original, "sanitised": sanitised},
    )


# ─────────────────────────────────────────────
#  VM SPEC PREVIEW UNIT TESTS
# ─────────────────────────────────────────────

def run_preview_tests() -> List[TestResult]:
    import time as _time
    from server.ai.cli import _build_vm_spec_rows

    cases = [
        {
            "id":    "preview_disk_bus_from_disk_bus_arg",
            "desc":  "disk_bus='sata' shows 'sata' in preview",
            "args":  {"name": "t", "os_type": "linux", "disk_bus": "sata", "disk_size_gb": 60},
            "check": lambda rows: dict(rows).get("Disk", "").startswith("60 GB (sata)"),
        },
        {
            "id":    "preview_disk_bus_from_disk_format_bus_name",
            "desc":  "disk_format='sata' (AI mistake) still shows 'sata' in preview",
            "args":  {"name": "t", "os_type": "linux", "disk_format": "sata", "disk_size_gb": 60},
            "check": lambda rows: dict(rows).get("Disk", "").startswith("60 GB (sata)"),
        },
        {
            "id":    "preview_hardened_forces_q35",
            "desc":  "hardened=True shows q35 in Machine row even without explicit machine_type",
            "args":  {"name": "t", "os_type": "linux", "hardened": True, "serial_number": "X"},
            "check": lambda rows: dict(rows).get("Machine", "").startswith("q35"),
        },
        {
            "id":    "preview_no_profile_when_smbios_set",
            "desc":  "Profile row absent when serial_number is provided",
            "args":  {"name": "t", "os_type": "linux", "serial_number": "X",
                      "manufacturer": "Dell Inc.", "profile": "dell_g15_5520"},
            "check": lambda rows: "Profile" not in dict(rows),
        },
    ]

    results = []
    for case in cases:
        start = _time.time()
        issues = []
        try:
            rows = _build_vm_spec_rows(case["args"])
            if not case["check"](rows):
                issues.append(f"Preview check failed. Rows: {dict(rows)}")
        except Exception:
            import traceback as _tb
            issues.append(f"Exception: {_tb.format_exc()}")
        results.append(TestResult(
            test_id=case["id"], layer=1, passed=len(issues) == 0,
            issues=issues, fixes_applied=[], duration_s=_time.time() - start,
        ))
    return results


# ─────────────────────────────────────────────
#  ARG BUILDER INVARIANT TESTS
#  Regression tests for crash-causing arg bugs.
# ─────────────────────────────────────────────

def run_arg_builder_tests() -> List[TestResult]:
    import time as _time, traceback as _tb
    from client.api.qemu_config import MachineConfig
    from client.api.qemu_arg_builder import QemuArgBuilder

    results: List[TestResult] = []

    def _build(overrides: dict) -> List[str]:
        defaults = dict(name="argtest", os_type="linux", machine_type="q35",
                        bios="seabios", uefi=False, hardened=False,
                        gpu="none", display="sdl", memory_mb=512,
                        cpu_cores=1, cpu_threads=1)
        defaults.update(overrides)
        cfg = MachineConfig(**defaults)
        return QemuArgBuilder(cfg).build()

    cases = [
        {
            "id":    "argbld_smm_off_absent_in_hardened",
            "desc":  "hardened=True must NOT add smm=off (crashes OVMF/KVM at Linux boot)",
            "build": lambda: _build({"hardened": True, "machine_type": "q35"}),
            "check": lambda cmd: "smm=off" not in " ".join(cmd),
        },
        {
            "id":    "argbld_gpu_none_no_nographic",
            "desc":  "gpu=none + display=sdl must NOT produce -nographic (hides window)",
            "build": lambda: _build({"gpu": "none", "display": "sdl"}),
            "check": lambda cmd: "-nographic" not in cmd,
        },
        {
            "id":    "argbld_no_smbios_type3_duplicate_type",
            "desc":  "-smbios type=3 must NOT be emitted (invalid type= field crashes QEMU)",
            "build": lambda: _build({"smbios_type": "Notebook", "manufacturer": "Dell"}),
            "check": lambda cmd: not any("type=3" in a and a.count("type=") > 1 for a in cmd),
        },
        {
            "id":    "argbld_ovmf_bios_sets_uefi_true",
            "desc":  "MachineConfig with bios=ovmf must coerce uefi=True (prevents missing VARS)",
            "build": lambda: [MachineConfig(name="t", bios="ovmf", uefi=False).uefi],
            "check": lambda result: result == [True],
        },
        {
            "id":    "argbld_kvm_pv_unhalt_suppressed_with_kvm_off",
            "desc":  "kvm_pv_unhalt must not appear when kvm=off is active (conflicts with hidden KVM)",
            "build": lambda: _build({"hardened": True, "kvm_pv_features": True}),
            "check": lambda cmd: not any("kvm_pv_unhalt" in a and "kvm=off" in " ".join(cmd) for a in cmd),
        },
    ]

    for case in cases:
        start = _time.time()
        issues = []
        try:
            result = case["build"]()
            if not case["check"](result):
                issues.append(f"Invariant violated. Built: {result}")
        except Exception:
            issues.append(f"Exception: {_tb.format_exc()}")
        results.append(TestResult(
            test_id=case["id"], layer=1, passed=len(issues) == 0,
            issues=issues, fixes_applied=[], duration_s=_time.time() - start,
        ))
    return results

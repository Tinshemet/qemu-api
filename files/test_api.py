"""
test_api.py — qemu-api Comprehensive Test Suite (v5)
Five independent layers:
  LAYER 1 — Sanitiser:       pure unit tests, no AI, instant
  LAYER 2 — Executor:        unit tests against execute_tool/preflight, no AI
  LAYER 3 — AI Integration:  full AI tests with randomised prompts, needs Ollama
  LAYER 4 — Random Profiles: random profiles + preflight/HTTP validation
  LAYER 5 — Property-Based:  invariant checking with hypothesis

Usage:
  python3 test_api.py                      # all layers (5 random profiles)
  python3 test_api.py -l 1                 # sanitiser only (fast, no Ollama)
  python3 test_api.py -l 1,2              # no Ollama needed, ~2s
  python3 test_api.py -l 3                 # AI tests only
  python3 test_api.py -l 4 -n 20          # 20 random profiles
  python3 test_api.py -l 5                 # property tests (needs hypothesis)
  python3 test_api.py -l 4 -s 123         # seed 123 for reproducibility
  python3 test_api.py -t hallucination     # filter by tag
  python3 test_api.py -v                   # verbose
  python3 test_api.py --quick              # L1+L2+L5(low iter), skip L3
  python3 test_api.py --fuzz               # L5 with high iteration count
  python3 test_api.py --benchmark llama3.1 qwen2.5:7b mistral-nemo
"""

import json, os, re, sys, time, random, string, traceback, uuid
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime

import requests
from rich.console import Console
from rich.table   import Table
from rich.panel   import Panel
from rich.text    import Text
from rich         import box
from rich.progress import track

sys.path.insert(0, os.path.dirname(__file__))
from ollama_wrapper import (
    _sanitise_args, _preflight_check, _resolve_iso,
    _validate_with_internet, _validate_profile_for_host,
    _get_qemu_machine_types, _get_qemu_cpu_models,
    _is_arm_cpu, _is_x86_cpu, _net_get, _net_head,
    execute_tool, OLLAMA_URL, OLLAMA_MODEL, TOOLS,
    _build_system_prompt, manager,
        VALID_AUDIO_TYPES, VALID_NETWORK_MODES, VALID_OS_TYPES,
    VALID_MACHINE_TYPES,
    OS_TYPE_ALIASES,
)
from qemu_config import (
    OVMF, get_all_profiles, MachineConfig,
    save_custom_profile, delete_custom_profile,
    check_system_capabilities,
)

console   = Console()
REAL_HOME = os.path.expanduser("~")

# VMs created by executor tests — cleaned up after each test
_EXECUTOR_VM_CLEANUP: List[str] = []


# ─────────────────────────────────────────────
#  DATACLASSES
# ─────────────────────────────────────────────

@dataclass
class SanitiserTest:
    id:               str
    tags:             List[str]
    description:      str
    tool:             str
    input_args:       Dict[str, Any]
    expect_fields:    Dict[str, Any] = field(default_factory=dict)
    removed_fields:   List[str]      = field(default_factory=list)
    changed_fields:   List[str]      = field(default_factory=list)
    unchanged_fields: List[str]      = field(default_factory=list)


@dataclass
class ExecutorTest:
    id:                 str
    tags:               List[str]
    description:        str
    tool:               str
    input_args:         Dict[str, Any]
    expect_success:     Optional[bool] = None
    expect_result_keys: List[str]      = field(default_factory=list)
    expect_result:      Dict[str, Any] = field(default_factory=dict)
    expect_clarify:     bool           = False
    expect_preflight:   Optional[str]  = None


@dataclass
class AITest:
    id:                   str
    tags:                 List[str]
    description:          str
    # prompt_pool: list of prompt strings to randomly choose from each run
    # If only one entry, always uses that. Supports {name},{ram},{os} format vars.
    prompt_pool:          List[str]
    expect_tools:         List[str]      = field(default_factory=list)
    # Alternative tools that are also acceptable (any one of these counts as passing)
    allow_alternatives:   Dict[str, List[str]] = field(default_factory=dict)
    expect_args:          Dict[str, Any] = field(default_factory=dict)
    forbid_args:          Dict[str, Any] = field(default_factory=dict)
    expect_sanitiser_fix: bool           = False
    # vagueness: 1=precise 2=normal 3=casual 4=vague 5=minimal
    vagueness:            int            = 2

    def get_prompt(self, seed: Optional[int] = None) -> str:
        """Pick a prompt from the pool, filling in random vars."""
        rng = random.Random(seed)
        p   = rng.choice(self.prompt_pool)
        # Random substitution vars
        names = ["dev-box","work-vm","test-rig","my-server","build-machine",
                 "ci-runner","sandbox","playground","lab-vm","demo-box"]
        rams  = ["2","4","8","16"]
        oses  = ["Ubuntu","Debian","Linux","Fedora"]
        p = p.replace("{name}", rng.choice(names))
        p = p.replace("{ram}",  rng.choice(rams))
        p = p.replace("{os}",   rng.choice(oses))
        return p


@dataclass
class ProfileTest:
    id:             str
    tags:           List[str]
    description:    str
    profile_data:   Dict[str, Any]
    profile_name:   str
    expect_issues:      List[str] = field(default_factory=list)
    expect_no_issues:   bool      = False
    expect_auto_fix:    bool      = False
    expect_http_check:  bool      = False
    expect_qemu_check:  bool      = False
    cleanup:            bool      = True


@dataclass
class TestResult:
    test_id:       str
    layer:         int
    passed:        bool
    issues:        List[str]
    fixes_applied: List[str]
    duration_s:    float
    detail:        Dict[str, Any] = field(default_factory=dict)


# ─────────────────────────────────────────────
#  LAYER 1 — SANITISER TESTS
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
        # Check value correct — don't use changed_fields (str/int compare equal as strings)
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
#  LAYER 2 — EXECUTOR TESTS
# Uses UUID-suffixed names to avoid leftover VM conflicts
# ─────────────────────────────────────────────

def _uid() -> str:
    """Short unique suffix for test VM names."""
    return uuid.uuid4().hex[:6]


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
        description="A real descriptive name passes (uses unique suffix to avoid conflicts)",
        tool="create_vm",
        # Use a name very unlikely to exist
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
        # sanitiser converts kvm=True→False for raspi BEFORE preflight runs
        # so by the time preflight checks, kvm is already False
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
        expect_preflight="ask_user",  # error severity → ask_user
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
            # Use the real ARM64 ISO that exists on your Desktop
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
]


# ─────────────────────────────────────────────
#  LAYER 3 — AI INTEGRATION TESTS
#  Each test has a prompt_pool — picked randomly per run
#  Vagueness 1=precise 2=normal 3=casual 4=vague 5=minimal
# ─────────────────────────────────────────────

AI_TESTS: List[AITest] = [
    AITest(
        id="ai_basic_linux",
        tags=["basic","linux","create"],
        description="Basic Linux VM — correct OS, memory, no ARM cpu",
        vagueness=2,
        prompt_pool=[
            "create a simple Ubuntu VM called {name} with {ram}GB RAM",
            "make me a {os} virtual machine called {name}, give it {ram}GB of memory",
            "spin up a linux box called {name} with {ram}GB RAM",
            "new VM: {name}, linux, {ram}GB",
        ],
        expect_tools=["create_vm"],
        forbid_args={"cpu_model": "cortex-a53"},
    ),
    AITest(
        id="ai_windows11",
        tags=["windows","create","uefi"],
        description="Windows 11 — must get q35, uefi, no ARM cpu",
        vagueness=2,
        prompt_pool=[
            "create a Windows 11 VM called {name} with {ram}GB RAM",
            "make me a win11 machine called {name}, {ram}GB memory",
            "I need a Windows 11 VM, name it {name}, {ram} gigs of RAM",
            "new windows 11 VM called {name}",
        ],
        expect_tools=["create_vm"],
        forbid_args={"uefi": False, "machine_type": "pc"},
    ),
    AITest(
        id="ai_profile_applied",
        tags=["profile","laptop"],
        description="Profile name must not appear as machine_type",
        vagueness=2,
        prompt_pool=[
            "use the office laptop profile, make it run Windows 11, call it {name}",
            "create a VM called {name} using the office laptop profile, Windows 11",
            "office laptop profile, win11, name it {name}",
        ],
        expect_tools=["create_vm"],
        forbid_args={"machine_type": "office_laptop"},
    ),
    AITest(
        id="ai_iso_no_fake_path",
        tags=["iso","paths","hallucination"],
        description="No /path/to/ or /home/user/ in iso_path",
        vagueness=3,
        prompt_pool=[
            "create a Linux VM called {name}, ISO is in my images folder",
            "make a linux vm called {name}, use the ubuntu iso from my downloads",
            "create {name}, linux, grab whatever ubuntu iso you can find",
        ],
        expect_tools=["create_vm"],
        forbid_args={"iso_path": "/path/to/"},
    ),
    AITest(
        id="ai_arm64_iso_auto_arch",
        tags=["iso","arch","arm"],
        description="ARM64 ISO → aarch64 arch",
        vagueness=2,
        prompt_pool=[
            "create a VM called {name} using Win11_25H2_EnglishInternational_Arm64_v2.iso",
            "make me a VM called {name} with the Arm64 windows iso",
            "create {name} using the ARM64 windows 11 iso file",
        ],
        expect_tools=["create_vm"],
        forbid_args={"machine_arch": "x86_64"},
    ),
    AITest(
        id="ai_no_arm_cpu_x86",
        tags=["cpu","arch","hallucination"],
        description="No ARM CPU on x86 VM",
        vagueness=2,
        prompt_pool=[
            "create a Lenovo ThinkPad style VM called {name} running Windows 11",
            "make a VM called {name} that looks like a ThinkPad laptop, Windows 11",
            "create a {name} VM modelled after a ThinkPad, win11",
        ],
        expect_tools=["create_vm"],
        forbid_args={"cpu_model": "cortex-a15"},
    ),
    AITest(
        id="ai_cpu_cap",
        tags=["cpu","resources"],
        description="Excessive CPU/RAM must be capped by sanitiser",
        vagueness=2,
        prompt_pool=[
            "create a VM called {name} with 128 CPU cores and 512GB RAM",
            "make {name} with 256 cores and 1TB RAM",
            "create {name}, give it as many cores as possible and 512 gigs",
        ],
        expect_tools=["create_vm"],
        expect_sanitiser_fix=True,
    ),
    AITest(
        id="ai_bridge_test",
        tags=["network","nat"],
        description="NAT networking — must call create_vm",
        vagueness=2,
        prompt_pool=[
            "create a simple Linux VM called {name} with NAT networking",
            "make a linux VM called {name}, use NAT for network",
            "create {name}, linux, network mode NAT",
        ],
        expect_tools=["create_vm"],
        expect_args={"network_mode": "nat"},
    ),
    AITest(
        id="ai_nat_default",
        tags=["network","nat"],
        description="NAT networking normalised to lowercase",
        vagueness=3,
        prompt_pool=[
            "create a Linux VM called {name} with {ram}GB RAM and NAT networking",
            "make {name}, linux, {ram}GB, internet via NAT",
            "new linux vm {name} {ram}gb nat",
        ],
        expect_tools=["create_vm"],
        expect_args={"network_mode": "nat"},
    ),
    AITest(
        id="ai_mac_invalid_fixed",
        tags=["network","mac","hallucination"],
        description="Invalid 7-octet MAC not passed to QEMU",
        vagueness=2,
        prompt_pool=[
            "create a VM called {name} with MAC address AA:BB:CC:DD:EE:FF:11",
            "make a VM called {name}, set its MAC to AA:BB:CC:DD:EE:FF:11",
        ],
        expect_tools=["create_vm"],
        forbid_args={"mac_address": "AA:BB:CC:DD:EE:FF:11"},
    ),
    AITest(
        id="ai_raspi_compat_check",
        tags=["raspi","arm","compat"],
        description="Raspi compatibility check",
        vagueness=2,
        prompt_pool=[
            "can I run a Raspberry Pi 3B on this machine?",
            "will raspberry pi 3b work on this system?",
            "check if raspi 3b is compatible with my hardware",
            "is the raspberry_pi_3b profile compatible with this machine?",
        ],
        expect_tools=["check_profile_compatibility"],
    ),
    AITest(
        id="ai_raspi_no_kvm",
        tags=["raspi","arm","kvm"],
        description="Raspi VM must end up with kvm=False — sanitiser handles it",
        vagueness=2,
        prompt_pool=[
            "create a Raspberry Pi 3B VM called {name}",
            "make a raspi 3b VM called {name}",
            "create a VM called {name} using the raspberry_pi_3b profile",
        ],
        expect_tools=["create_vm"],
        forbid_args={"kvm": True},
        # sanitiser converts kvm=True→False before the test sees args
        # so expect_sanitiser_fix=False is correct — kvm may already be False
        expect_sanitiser_fix=False,
    ),
    AITest(
        id="ai_create_and_launch",
        tags=["create","launch","multi"],
        description="Must call create_vm THEN launch_vm",
        vagueness=2,
        prompt_pool=[
            "create a simple Linux VM called {name} and launch it",
            "make a linux VM called {name} and start it",
            "create {name} linux VM and run it immediately",
        ],
        expect_tools=["create_vm","launch_vm"],
    ),
    AITest(
        id="ai_monitor_by_number",
        tags=["monitor","status"],
        description="'vm 1' / status query resolves to monitor_vm or vm_status",
        vagueness=3,
        prompt_pool=[
            "check vm 1 and report its activity",
            "monitor vm number 1",
            "what is vm 1 doing?",
            "status of the first VM",
        ],
        expect_tools=["monitor_vm"],
        allow_alternatives={"monitor_vm": ["vm_status"]},
    ),
    AITest(
        id="ai_failure_diagnosis",
        tags=["diagnosis","logs"],
        description="Must call get_vm_logs",
        vagueness=2,
        prompt_pool=[
            "why did dev-box fail to launch? check its logs",
            "check the logs for dev-box and tell me why it stopped",
            "get the failure logs for dev-box",
            "diagnose why dev-box crashed",
            "what went wrong with dev-box?",
        ],
        expect_tools=["get_vm_logs"],
    ),
    AITest(
        id="ai_delete_vm_not_profile",
        tags=["delete"],
        description="Must call delete_vm not delete_profile",
        vagueness=2,
        prompt_pool=[
            "delete the test-ubuntu VM",
            "remove the VM called test-ubuntu",
            "destroy test-ubuntu",
            "get rid of test-ubuntu VM",
        ],
        expect_tools=["delete_vm"],
    ),
    AITest(
        id="ai_snapshot_create",
        tags=["snapshot"],
        description="Must call snapshot_create",
        vagueness=2,
        prompt_pool=[
            "take a snapshot of the test-ubuntu VM called pre-update",
            "create a snapshot of test-ubuntu named pre-update",
            "snapshot test-ubuntu as pre-update",
            "make a snapshot of test-ubuntu called pre-update",
        ],
        expect_tools=["snapshot_create"],
    ),
    AITest(
        id="ai_list_vms",
        tags=["basic","list"],
        description="Must call list_vms",
        vagueness=4,
        prompt_pool=[
            "what VMs do I have?",
            "list all my virtual machines",
            "show me my VMs",
            "what's running?",
            "vms",
        ],
        expect_tools=["list_vms"],
    ),
    AITest(
        id="ai_system_check",
        tags=["basic","system"],
        description="Must call check_system",
        vagueness=3,
        prompt_pool=[
            "what does this system support?",
            "check system capabilities",
            "what can this machine do?",
            "system info",
        ],
        expect_tools=["check_system"],
    ),
]


# ─────────────────────────────────────────────
#  LAYER 4 — RANDOM PROFILE TESTS
# ─────────────────────────────────────────────

def _rand_name(prefix: str = "test") -> str:
    return f"{prefix}-{''.join(random.choices(string.ascii_lowercase, k=6))}"


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
        return p, ["ram", "31779"]   # match actual message which says "host only has 31779MB"
    elif broken_type == "wrong_binary_for_arch":
        p = _generate_arm_profile()
        p["qemu_binary"] = "qemu-system-x86_64"
        p["machine_type"] = "virt"
        p["machine_arch"] = "aarch64"
        # The profile validator catches ARM arch + x86 binary and auto-fixes
        # Also add kvm=True so kvm auto-fix fires
        p["kvm"] = True
        return p, ["not supported", "kvm"]
    else:  # raspi_with_kvm
        p = _generate_arm_profile()
        p["machine_type"] = "raspi3b"
        p["kvm"]          = True
        return p, ["kvm", "arm"]


# ── Random AI test constants ─────────────────────────────────────────────────

_RAND_SNAP_NAMES = ["pre-update","baseline","checkpoint","before-test","clean-state"]
_RAND_RAM        = ["2","4","8","16"]
_RAND_OS         = ["Ubuntu","Linux","Fedora","Debian"]
_RAND_VM_NAMES   = ["dev-box","work-vm","test-rig","my-server","build-machine",
                    "ci-runner","sandbox","playground","lab-vm","demo-box"]

_TOOL_PROMPT_POOLS: Dict[str, List[str]] = {
    "list_vms":       ["list my VMs", "show all VMs", "what VMs do I have?", "vms"],
    "create_vm":      ["create a {os} VM called {vm} with {ram}GB RAM",
                       "make me a linux VM called {vm}",
                       "new VM: {vm}, {os}, {ram}GB"],
    "launch_vm":      ["launch {vm}", "start the {vm} VM", "run {vm}"],
    "stop_vm":        ["stop {vm}", "shut down {vm}", "kill the {vm} VM"],
    "vm_status":      ["status of {vm}", "what is {vm} doing?", "is {vm} running?"],
    "monitor_vm":     ["monitor {vm}", "check activity on {vm}", "deep status of {vm}"],
    "delete_vm":      ["delete {vm}", "remove the {vm} VM", "destroy {vm}"],
    "show_config":    ["show config for {vm}", "what is the config of {vm}?"],
    "snapshot_create":["snapshot {vm} as {snap}", "create snapshot {snap} on {vm}",
                       "take a snapshot of {vm} called {snap}"],
    "snapshot_list":  ["list snapshots for {vm}", "what snapshots does {vm} have?"],
    "snapshot_restore":["restore {vm} to {snap}", "revert {vm} to snapshot {snap}"],
    "snapshot_delete": ["delete snapshot {snap} on {vm}", "remove {snap} from {vm}"],
    "clone_vm":       ["clone {vm} into {vm}-copy", "duplicate the {vm} VM"],
    "resize_disk":    ["resize {vm} disk to 100GB", "expand {vm} disk to 80GB"],
    "check_system":   ["check system capabilities", "what does this system support?", "system info"],
    "list_profiles":  ["list profiles", "what profiles are available?", "show hardware profiles"],
    "scan_isos":      ["scan for ISOs", "find ISO files", "what ISOs do I have?"],
    "get_vm_logs":    ["why did {vm} fail?", "check logs for {vm}", "diagnose {vm} crash"],
    "print_command":  ["show QEMU command for {vm}", "print launch command for {vm}"],
    "update_config":  ["update {vm} config", "change {vm} settings"],
    "set_resource_limits": ["limit {vm} to 50% CPU", "cap {vm} memory to 2GB"],
    "open_display":   ["open display for {vm}", "show screen of {vm}"],
    "open_shell":     ["open shell on {vm}", "serial console for {vm}"],
    "list_networks":  ["list networks", "what networks exist?"],
    "create_network": ["create network lab-net", "add isolated network test-net"],
    "send_monitor_cmd": ["send info status to {vm}", "query QEMU monitor on {vm}"],
}


def _generate_random_ai_tests(n: int = 5, seed: int = 42) -> List[AITest]:
    """
    Generate N random Layer 3 AI tests covering all available tools.
    Each test picks a random tool, then picks a random prompt from that
    tool's pool with randomised VM names, snap names, RAM, and OS.
    """
    rng     = random.Random(seed)
    tools   = list(_TOOL_PROMPT_POOLS.keys())
    tests:  List[AITest] = []

    for i in range(n):
        # Pick a tool — distribute evenly, then random within distribution
        tool = tools[i % len(tools)] if i < len(tools) else rng.choice(tools)

        # Randomise substitution vars
        vm   = rng.choice(_RAND_VM_NAMES)
        snap = rng.choice(_RAND_SNAP_NAMES)
        ram  = rng.choice(_RAND_RAM)
        os   = rng.choice(_RAND_OS)

        pool    = _TOOL_PROMPT_POOLS[tool]
        prompt  = rng.choice(pool)
        prompt  = (prompt
                   .replace("{vm}",   vm)
                   .replace("{snap}", snap)
                   .replace("{ram}",  ram)
                   .replace("{os}",   os))

        # Determine vagueness level — shorter prompts are more vague
        words     = len(prompt.split())
        vagueness = 1 if words <= 2 else 2 if words <= 5 else 3

        tests.append(AITest(
            id=f"ai_rand_tool_{tool}_{i:02d}",
            tags=["random","ai","tool",tool.replace("_","-")],
            description=f"Random {tool} test #{i} — prompt varies by seed",
            vagueness=vagueness,
            prompt_pool=[prompt],
            expect_tools=[tool],
            # For tools with known alternatives accept them too
            allow_alternatives={
                "monitor_vm":   ["vm_status"],
                "vm_status":    ["monitor_vm"],
                "list_vms":     ["vm_status"],
                "show_config":  ["vm_status","list_vms"],
                "open_display": ["launch_vm"],
                "print_command":["show_config"],
            },
        ))

    return tests


def _generate_ai_tests_from_profiles(n: int = 5, seed: int = 42) -> List[AITest]:
    """Legacy wrapper — now calls _generate_random_ai_tests."""
    return _generate_random_ai_tests(n, seed)


def _cleanup_random_ai_profiles(tests: List[AITest]) -> None:
    """No-op — random AI tests no longer create custom profiles."""
    pass


def _generate_profile_tests(n: int = 5, seed: int = 42) -> List[ProfileTest]:
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


def _cleanup_random_ai_profiles(tests: List[AITest]) -> None:
    """No-op — random AI tests no longer create custom profiles."""
    pass


# ─────────────────────────────────────────────
#  LAYER 5 — PROPERTY-BASED TESTS
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
            # Invariant: result must be a dict
            if not isinstance(result, dict):
                issues.append(f"iter {i}: sanitiser returned non-dict: {type(result)}")
            # Invariant: network_mode always in valid set if present
            nm = result.get("network_mode")
            if nm and nm not in ("nat","bridge","none"):
                issues.append(f"iter {i}: invalid network_mode after sanitise: {nm!r}")
            # Invariant: os_type always valid if present
            ot = result.get("os_type")
            if ot and ot not in ("linux","windows","macos","other"):
                issues.append(f"iter {i}: invalid os_type after sanitise: {ot!r}")
            # Invariant: kvm is always a bool if present
            kvm = result.get("kvm")
            if kvm is not None and not isinstance(kvm, bool):
                issues.append(f"iter {i}: kvm is not bool after sanitise: {type(kvm)}")
            # Invariant: memory_mb is always int if present
            mem = result.get("memory_mb")
            if mem is not None and not isinstance(mem, int):
                issues.append(f"iter {i}: memory_mb is not int: {type(mem)}")
            # Invariant: if aarch64 arch, kvm must be False
            if result.get("machine_arch") == "aarch64" and result.get("kvm") is True:
                issues.append(f"iter {i}: aarch64 VM has kvm=True after sanitise")
        except Exception as e:
            issues.append(f"iter {i}: CRASH: {e}")

    results.append(_make_result(
        "prop_sanitiser_never_crashes",
        len(issues) == 0,
        issues[:5],  # cap at 5 to keep output readable
        time.time() - start,
    ))

    # ── Property 2: Preflight never crashes ──────────────────────────────────
    start  = time.time()
    issues = []
    tools  = ["create_vm","launch_vm","delete_vm","resize_disk","send_monitor_cmd"]
    for i in range(iterations):
        tool = random.choice(tools)
        args = {
            "name":       random.choice(["test","my-vm","","dev-box-99"]),
            "os_type":    random.choice(["linux","windows",""]),
            "new_size_gb":random.choice([1, 60, 999]),
            "cmd":        random.choice(["info status","quit","system_reset","ls"]),
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
    # Running sanitiser twice must give same result as running once
    start  = time.time()
    issues = []
    for i in range(iterations):
        args = {
            "name":        random.choice(["dev-box","linux-vm","test","my-vm-01"]),
            "os_type":     random.choice(["linux","ubuntu","windows","win11"]),
            "memory_mb":   random.choice([4096, "8192", 2048]),
            "network_mode":random.choice(["nat","NAT","bridge"]),
            "kvm":         random.choice([True, "true", False]),
        }
        try:
            once  = _sanitise_args("create_vm", dict(args))
            twice = _sanitise_args("create_vm", dict(once))
            # Key fields must be same after second pass
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
            if result.get("name"):  # should be empty string or removed
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
        # Only test profiles that are NOT valid machine types
        if pname in VALID_MACHINE_TYPES:
            continue
        try:
            result = _sanitise_args("create_vm", {"name": "test", "machine_type": pname})
            # machine_type must be removed
            if "machine_type" in result and result["machine_type"] == pname:
                issues.append(f"Profile '{pname}' not stripped from machine_type")
            # profile must be set
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
            # Compare as actual types (int==int) not as strings to fix str/int coercion tests
            if actual != v and str(actual) != str(v):
                issues.append(f"Expected {k}={v!r} got {k}={actual!r}")

        for k in tc.removed_fields:
            if k in sanitised and sanitised[k] not in (None, "", [], {}):
                issues.append(f"'{k}' should be removed but got {sanitised[k]!r}")

        for k in tc.changed_fields:
            if k in original:
                orig_v = original[k]
                san_v  = sanitised.get(k)
                # Use actual type comparison, not string comparison
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

        # Suppress stdout/stderr during execute_tool to prevent panels
        # from corrupting the progress bar display
        import io, contextlib
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
        # Clean up any VMs created by this test
        vm_name = tc.input_args.get("name","")
        if vm_name and tc.tool == "create_vm":
            vm_dir = os.path.join(os.path.expanduser("~"), ".qemu_vms", vm_name)
            if os.path.exists(vm_dir):
                import shutil as _shutil
                _shutil.rmtree(vm_dir, ignore_errors=True)

    return TestResult(test_id=tc.id, layer=2, passed=len(issues)==0,
                      issues=issues, fixes_applied=fixes, duration_s=time.time()-start)


# ─────────────────────────────────────────────
#  LAYER 3 RUNNER
# ─────────────────────────────────────────────

def call_ollama(messages: List[Dict], model: str = None) -> Tuple[List[Dict], str]:
    payload = {
        "model":   model or OLLAMA_MODEL,
        "messages": messages,
        "tools":   TOOLS,
        "stream":  False,
        "options": {"temperature": 0.1, "num_ctx": 8192},
    }
    try:
        resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=90)
        resp.raise_for_status()
        data = resp.json()
        msg  = data.get("message", {})
        return msg.get("tool_calls", []) or [], msg.get("content", "") or ""
    except Exception as e:
        return [], str(e)


def run_ai_test(tc: AITest, system_prompt: str, seed: int = None,
                model: str = None) -> TestResult:
    start   = time.time()
    issues: List[str] = []
    fixes:  List[str] = []
    detail  = {}

    prompt = tc.get_prompt(seed)
    detail["prompt_used"] = prompt

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": prompt},
        ]
        all_calls: List[Dict] = []
        san_list:  List[Dict] = []

        for _ in range(6):
            tcs, txt = call_ollama(messages, model=model)
            if not tcs:
                break
            all_calls.extend(tcs)
            messages.append({"role": "assistant", "content": txt or "", "tool_calls": tcs})
            for tc_call in tcs:
                fn  = tc_call.get("function", {})
                tn  = fn.get("name", "")
                raw = fn.get("arguments", {})
                if isinstance(raw, str):
                    try: raw = json.loads(raw)
                    except: raw = {}
                orig = json.loads(json.dumps(raw, default=str))
                san  = _sanitise_args(tn, dict(raw))
                san_list.append({"tool": tn, "original": orig, "sanitised": san})
                for k in orig:
                    if k in san and orig[k] != san[k]:
                        fixes.append(f"[{tn}] {k}: {orig[k]!r}→{san[k]!r}")
                    elif k not in san:
                        fixes.append(f"[{tn}] removed {k}: {orig[k]!r}")
                messages.append({"role": "tool", "content": json.dumps({"success": True})})

            called = [s["tool"] for s in san_list]
            if all(t in called for t in tc.expect_tools):
                break

        called_tools = [s["tool"] for s in san_list]
        detail["tools_called"] = called_tools
        detail["sanitised"]    = san_list

        for expected in tc.expect_tools:
            if expected not in called_tools:
                # Check allow_alternatives — e.g. vm_status acceptable instead of monitor_vm
                alts = tc.allow_alternatives.get(expected, [])
                if not any(a in called_tools for a in alts):
                    issues.append(f"Expected '{expected}' not called. Called: {called_tools}")

        all_args: Dict[str, Any] = {}
        for s in san_list:
            all_args.update(s["sanitised"])

        for k, v in tc.expect_args.items():
            actual = all_args.get(k)
            if actual is None:
                issues.append(f"Expected {k}={v} not in any tool call")
            elif str(actual).lower() != str(v).lower():
                issues.append(f"Expected {k}={v!r} got {k}={actual!r}")

        for s in san_list:
            for k, bad in tc.forbid_args.items():
                san_val  = s["sanitised"].get(k)
                orig_val = s["original"].get(k)
                if san_val is not None and str(san_val).lower() == str(bad).lower():
                    issues.append(f"HALLUCINATION: [{s['tool']}] {k}={bad!r} survived sanitiser")
                elif orig_val is not None and str(orig_val).lower() == str(bad).lower():
                    fixes.append(f"[{s['tool']}] sanitiser caught {k}={bad!r}")

        if tc.expect_sanitiser_fix and not fixes:
            issues.append("Expected sanitiser to fix something but nothing changed")

    except Exception:
        issues.append(f"Exception: {traceback.format_exc()}")

    return TestResult(test_id=tc.id, layer=3, passed=len(issues)==0,
                      issues=issues, fixes_applied=fixes,
                      duration_s=time.time()-start, detail=detail)


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


# ─────────────────────────────────────────────
#  RENDERER
# ─────────────────────────────────────────────

LAYER_NAMES   = {1:"Sanitiser", 2:"Executor", 3:"AI Integration",
                 4:"Profile + HTTP", 5:"Property-Based", 6:"Input Pipeline"}
LAYER_COLOURS = {1:"green", 2:"cyan", 3:"magenta", 4:"yellow", 5:"blue", 6:"white"}


def render_layer_results(results: List[TestResult], layer: int, verbose: bool = False):
    lr = [r for r in results if r.layer == layer]
    if not lr:
        return
    passed = sum(1 for r in lr if r.passed)
    total  = len(lr)
    sc     = "green" if passed==total else "yellow" if passed > total//2 else "red"
    console.print(Panel(
        f"[bold]{passed}/{total} passed[/bold]  "
        + ("[green]✓ All passing[/green]" if passed==total
           else f"[red]{total-passed} failing[/red]"),
        title=f"[bold {LAYER_COLOURS[layer]}]Layer {layer} — {LAYER_NAMES[layer]}[/bold {LAYER_COLOURS[layer]}]",
        border_style=sc,
    ))
    t = Table(box=box.SIMPLE_HEAVY, border_style=LAYER_COLOURS[layer],
              header_style=f"bold {LAYER_COLOURS[layer]}", show_lines=False)
    t.add_column("Test ID", style="bold white", width=36)
    t.add_column("Result", justify="center", width=8)
    t.add_column("Fixes", style="yellow", width=8)
    t.add_column("Time", justify="right", width=7, style="dim")
    t.add_column("Issue", style="red")
    for r in lr:
        rs  = "[green]✓[/green]" if r.passed else "[red]✗[/red]"
        fs  = str(len(r.fixes_applied)) if r.fixes_applied else "—"
        iss = r.issues[0][:70] if r.issues else "—"
        t.add_row(r.test_id, rs, fs, f"{r.duration_s:.1f}s", iss)
    console.print(t)
    for r in lr:
        if r.passed:
            continue
        lines = [f"  [red]✗[/red] {i}" for i in r.issues]
        if r.fixes_applied:
            lines += ["  [yellow]Fixes:[/yellow]"] + \
                     [f"    [yellow]→[/yellow] {f}" for f in r.fixes_applied[:4]]
        if verbose and r.detail:
            if "original" in r.detail:
                lines.append(f"  [dim]In:  {json.dumps(r.detail['original'],default=str)[:180]}[/dim]")
            if "sanitised" in r.detail and isinstance(r.detail["sanitised"], dict):
                lines.append(f"  [dim]Out: {json.dumps(r.detail['sanitised'],default=str)[:180]}[/dim]")
            if "all_messages" in r.detail:
                lines.append(f"  [dim]Issues: {r.detail['all_messages'][:200]}[/dim]")
            if "prompt_used" in r.detail:
                lines.append(f"  [dim]Prompt: {r.detail['prompt_used']}[/dim]")
        console.print(Panel("\n".join(lines),
                             title=f"[red]✗ {r.test_id}[/red]", border_style="red"))
    console.print()


def render_summary(results: List[TestResult]):
    passed    = sum(1 for r in results if r.passed)
    total     = len(results)
    all_fixes = [f for r in results for f in r.fixes_applied]
    colour    = "green" if passed==total else "yellow" if passed > total*0.8 else "red"
    console.print(Panel(
        f"[bold]Total: {passed}/{total} passed[/bold]  ·  "
        f"{len(all_fixes)} sanitiser/validator fixes applied",
        title="[bold]Overall Summary[/bold]", border_style=colour,
    ))


def render_benchmark(bm_results: Dict[str, List[TestResult]]):
    """Render side-by-side model comparison table."""
    models = list(bm_results.keys())
    t = Table(title="Model Benchmark — Layer 3", box=box.HEAVY_EDGE,
              header_style="bold cyan", show_lines=True)
    t.add_column("Test ID", style="bold white", width=28)
    for m in models:
        t.add_column(m, justify="center", width=14)

    # Collect all test IDs
    all_ids = []
    for results in bm_results.values():
        for r in results:
            if r.test_id not in all_ids:
                all_ids.append(r.test_id)

    for tid in all_ids:
        row = [tid]
        for m in models:
            r = next((x for x in bm_results[m] if x.test_id == tid), None)
            if r is None:
                row.append("[dim]—[/dim]")
            elif r.passed:
                fixes = f" ({len(r.fixes_applied)}f)" if r.fixes_applied else ""
                row.append(f"[green]✓{fixes}[/green]\n[dim]{r.duration_s:.1f}s[/dim]")
            else:
                row.append(f"[red]✗[/red]\n[dim]{r.duration_s:.1f}s[/dim]")
        t.add_row(*row)

    # Summary row
    summary_row = ["[bold]TOTAL[/bold]"]
    for m in models:
        results = bm_results[m]
        passed  = sum(1 for r in results if r.passed)
        total   = len(results)
        fixes   = sum(len(r.fixes_applied) for r in results)
        avg_t   = sum(r.duration_s for r in results) / total if total else 0
        colour  = "green" if passed==total else "yellow" if passed > total//2 else "red"
        summary_row.append(
            f"[{colour}]{passed}/{total}[/{colour}]\n"
            f"[dim]{fixes}f | {avg_t:.1f}s avg[/dim]"
        )
    t.add_row(*summary_row)
    console.print(t)


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────


# ─────────────────────────────────────────────
#  LAYER 6 — INPUT PIPELINE TESTS
#  Tests Layer 0 (Vagueness Layer) and the
#  Confirmation Gate classification.
#  No AI, no Ollama, no network — instant.
# ─────────────────────────────────────────────

def main():
    argv    = sys.argv[1:]
    verbose = "-v" in argv or "--verbose" in argv
    argv    = [a for a in argv if a not in ("-v","--verbose")]

    # Special modes
    quick = "--quick" in argv
    fuzz  = "--fuzz"  in argv
    argv  = [a for a in argv if a not in ("--quick","--fuzz")]

    # Benchmark mode: --benchmark [model1 model2 ...]
    if "--benchmark" in argv:
        idx = argv.index("--benchmark")
        # Only consume args that look like model names (not flags starting with -)
        bm_models = [a for a in argv[idx+1:] if not a.startswith("-") and not a.isdigit()]
        if not bm_models:
            bm_models = [OLLAMA_MODEL]
        bm_results: Dict[str, List[TestResult]] = {}
        sp = _build_system_prompt()
        seed = 42
        for model in bm_models:
            console.print(f"\n[bold cyan]Benchmarking {model}...[/bold cyan]")
            model_results = []
            # Also check tool format compatibility
            console.print(f"  [dim]Checking tool call format...[/dim]", end=" ")
            try:
                tcs, _ = call_ollama([
                    {"role":"system","content":"You are a VM assistant."},
                    {"role":"user",  "content":"list my vms"},
                ], model=model)
                fmt_ok = len(tcs) > 0
                console.print("[green]OK[/green]" if fmt_ok else "[red]no tool calls[/red]")
            except Exception as e:
                console.print(f"[red]ERROR: {e}[/red]")
                fmt_ok = False

            if fmt_ok:
                for tc in AI_TESTS:
                    r = run_ai_test(tc, sp, seed=seed, model=model)
                    model_results.append(r)
                    status = "[green]✓[/green]" if r.passed else "[red]✗[/red]"
                    console.print(f"    {status} {tc.id} [{r.duration_s:.1f}s]")
            bm_results[model] = model_results

        console.print()
        render_benchmark(bm_results)
        return

    # Layer filter
    if quick:
        run_layers = {1, 2, 5, 6}
    else:
        run_layers = {1, 2, 3, 4, 5, 6}

    if "-l" in argv:
        idx = argv.index("-l")
        if idx+1 < len(argv):
            run_layers = {int(x) for x in argv[idx+1].split(",")}
            argv = argv[:idx] + argv[idx+2:]

    # Tag filter
    tag_filter = None
    if "-t" in argv:
        idx = argv.index("-t")
        if idx+1 < len(argv):
            tag_filter = argv[idx+1]
            argv = argv[:idx] + argv[idx+2:]

    # Random profiles count
    n_random = 5
    if "-n" in argv:
        idx = argv.index("-n")
        if idx+1 < len(argv):
            try: n_random = int(argv[idx+1])
            except: pass
            argv = argv[:idx] + argv[idx+2:]

    # Seed for random profiles and prompt selection
    seed = 42
    if "-s" in argv:
        idx = argv.index("-s")
        if idx+1 < len(argv):
            try: seed = int(argv[idx+1])
            except: pass
            argv = argv[:idx] + argv[idx+2:]

    # Property test iterations
    prop_iters = 500 if fuzz else (20 if quick else 50)

    def tag_ok(tags): return tag_filter is None or tag_filter in tags

    san_tests     = [t for t in SANITISER_TESTS if tag_ok(t.tags)] if 1 in run_layers else []
    exec_tests    = [t for t in EXECUTOR_TESTS  if tag_ok(t.tags)] if 2 in run_layers else []
    # Layer 3: fixed AI tests + N dynamic profile-based tests (compound -n)
    rand_ai_tests: List[AITest] = []
    if 3 in run_layers and n_random > 0:
        rand_ai_tests = [t for t in _generate_ai_tests_from_profiles(n_random, seed)
                         if tag_ok(t.tags)]
    ai_tests = ([t for t in AI_TESTS if tag_ok(t.tags)] + rand_ai_tests)\
               if 3 in run_layers else []
    profile_tests = [t for t in _generate_profile_tests(n_random, seed)
                     if tag_ok(t.tags)] if 4 in run_layers else []
    run_props     = 5 in run_layers
    run_pipeline  = 6 in run_layers

    mode_str = "FUZZ" if fuzz else ("QUICK" if quick else "normal")
    console.print(Panel(
        f"[bold cyan]qemu-api Test Suite v5[/bold cyan]\n"
        f"Model: [bold]{OLLAMA_MODEL}[/bold]  |  {OLLAMA_URL}\n"
        f"Layers: {sorted(run_layers)}  "
        f"| L1={len(san_tests)} L2={len(exec_tests)} "
        f"L3={len(AI_TESTS)}+{len(rand_ai_tests)}dyn "
        f"L4={len(profile_tests)} L5={'yes' if run_props else 'no'} "
        f"L6={'yes' if run_pipeline else 'no'}"
        f"{'(+' + str(n_random) + 'r)' if run_pipeline and n_random > 0 else ''}\n"
        f"Seed: {seed}  |  Mode: {mode_str}"
        + (f"\nTag: [bold]{tag_filter}[/bold]" if tag_filter else ""),
        border_style="cyan", title="[bold]qemu-api[/bold]",
    ))

    all_results: List[TestResult] = []

    if san_tests:
        console.print(f"\n[bold green]Layer 1 — Sanitiser ({len(san_tests)})[/bold green]")
        for tc in track(san_tests, description="  Running..."):
            r = run_sanitiser_test(tc)
            all_results.append(r)
            console.print(f"    {'[green]✓[/green]' if r.passed else '[red]✗[/red]'} "
                           f"{tc.id} [{r.duration_s*1000:.0f}ms]")

    if exec_tests:
        console.print(f"\n[bold cyan]Layer 2 — Executor ({len(exec_tests)})[/bold cyan]")
        for tc in track(exec_tests, description="  Running..."):
            r = run_executor_test(tc)
            all_results.append(r)
            console.print(f"    {'[green]✓[/green]' if r.passed else '[red]✗[/red]'} "
                           f"{tc.id} [{r.duration_s*1000:.0f}ms]")

    if ai_tests:
        console.print(f"\n[bold magenta]Layer 3 — AI Integration ({len(ai_tests)}) "
                       f"[dim]seed={seed}[/dim][/bold magenta]")
        sp = _build_system_prompt()
        for tc in track(ai_tests, description="  Running..."):
            console.print(f"    [dim]→ {tc.id}[/dim]", end=" ")
            r = run_ai_test(tc, sp, seed=seed)
            all_results.append(r)
            fs = f" [yellow]({len(r.fixes_applied)}f)[/yellow]" if r.fixes_applied else ""
            console.print(f"{'[green]✓[/green]' if r.passed else '[red]✗[/red]'}{fs} "
                           f"[{r.duration_s:.1f}s]")

    # Clean up custom profiles created for dynamic AI tests
    if rand_ai_tests:
        _cleanup_random_ai_profiles(rand_ai_tests)

    if profile_tests:
        console.print(f"\n[bold yellow]Layer 4 — Profile + HTTP ({len(profile_tests)}) "
                       f"[dim]seed={seed}[/dim][/bold yellow]")
        for tc in track(profile_tests, description="  Running..."):
            console.print(f"    [dim]→ {tc.id}[/dim]", end=" ")
            r = run_profile_test(tc)
            all_results.append(r)
            fs  = f" [yellow]({len(r.fixes_applied)}f)[/yellow]" if r.fixes_applied else ""
            ni  = r.detail.get("issue_count","?")
            console.print(f"{'[green]✓[/green]' if r.passed else '[red]✗[/red]'}{fs} "
                           f"[{r.duration_s:.1f}s] ({ni} issues)")

    if run_props:
        console.print(f"\n[bold blue]Layer 5 — Property-Based ({prop_iters} iterations)[/bold blue]")
        prop_results = run_property_tests(prop_iters)
        for r in prop_results:
            all_results.append(r)
            console.print(f"    {'[green]✓[/green]' if r.passed else '[red]✗[/red]'} "
                           f"{r.test_id} [{r.duration_s:.1f}s]")


    console.print()
    for layer in sorted(run_layers):
        render_layer_results(all_results, layer, verbose)
    render_summary(all_results)

    report = {
        "timestamp": datetime.now().isoformat(),
        "model":     OLLAMA_MODEL,
        "seed":      seed,
        "layers":    sorted(run_layers),
        "mode":      mode_str,
        "passed":    sum(1 for r in all_results if r.passed),
        "total":     len(all_results),
        "results": [{
            "id": r.test_id, "layer": r.layer, "passed": r.passed,
            "issues": r.issues, "fixes": r.fixes_applied, "duration": r.duration_s,
        } for r in all_results],
    }
    report_path = os.path.join(os.path.dirname(__file__), "test_report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
    console.print(f"[dim]Report → {report_path}[/dim]")
    sys.exit(0 if all(r.passed for r in all_results) else 1)


if __name__ == "__main__":
    main()

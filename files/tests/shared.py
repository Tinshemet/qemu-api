"""
tests/shared.py — Common imports, dataclasses, and console used across all test layers.
"""

import json, os, re, sys, time, random, string, traceback, uuid
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime

import requests
from rich.console  import Console
from rich.table    import Table
from rich.panel    import Panel
from rich.text     import Text
from rich          import box
from rich.progress import track

# Add files/ to sys.path so layer-module imports resolve correctly
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from shared.executioner.tool_executor import execute_tool, manager
from shared.sanitizer.sanitizer import _sanitise_args, _resolve_iso
from shared.preflight.validator import (
    _preflight_check, _validate_with_internet, _validate_profile_for_host,
)
from server.ai.ollama_client import OLLAMA_URL, OLLAMA_MODEL, TOOLS, _build_system_prompt
from server.ai.context_assistant import check_context
from shared.preflight.validator import (
    _get_qemu_machine_types, _get_qemu_cpu_models,
    _is_arm_cpu, _is_x86_cpu, _net_get, _net_head,
)
from shared.sanitizer.sanitizer import (
    VALID_AUDIO_TYPES, VALID_NETWORK_MODES, VALID_OS_TYPES,
    VALID_MACHINE_TYPES, OS_TYPE_ALIASES,
)
from shared.api.qemu_config import (
    OVMF, get_all_profiles, MachineConfig,
    save_custom_profile, delete_custom_profile,
    check_system_capabilities,
)

console   = Console()
REAL_HOME = os.path.expanduser("~")

_EXECUTOR_VM_CLEANUP: List[str] = []


# ─────────────────────────────────────────────
#  DATACLASSES
# ─────────────────────────────────────────────

@dataclass
class ContextAssistantTest:
    id:           str
    tags:         List[str]
    description:  str
    prompt:       str
    tool_name:    str
    args:         Dict[str, Any]
    expect_fired: bool                # True = hint returned, False = None returned
    expect_type:  Optional[str] = None  # "mismatch" | "hallucinated" | "high_stakes"


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
    # Check fields on the saved MachineConfig after create_vm succeeds.
    expect_cfg:         Dict[str, Any] = field(default_factory=dict)
    # Check that result["command"] contains all of these substrings (print_command / dry_run).
    expect_cmd_contains: List[str]     = field(default_factory=list)


@dataclass
class AITest:
    id:                   str
    tags:                 List[str]
    description:          str
    prompt_pool:          List[str]
    expect_tools:         List[str]            = field(default_factory=list)
    allow_alternatives:   Dict[str, List[str]] = field(default_factory=dict)
    expect_args:          Dict[str, Any]       = field(default_factory=dict)
    forbid_args:          Dict[str, Any]       = field(default_factory=dict)
    expect_sanitiser_fix: bool                 = False
    vagueness:            int                  = 2
    # None = don't assert gate behaviour; True = expect gate to block at least one call;
    # False = expect gate to pass every call (AI provided all required args).
    expect_gate_blocked:  Optional[bool]       = None

    def get_prompt(self, seed: Optional[int] = None) -> str:
        """Pick a prompt from the pool, filling in random vars."""
        rng   = random.Random(seed)
        p     = rng.choice(self.prompt_pool)
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
    id:                 str
    tags:               List[str]
    description:        str
    profile_data:       Dict[str, Any]
    profile_name:       str
    expect_issues:      List[str] = field(default_factory=list)
    expect_no_issues:   bool      = False
    expect_auto_fix:    bool      = False
    expect_http_check:  bool      = False
    expect_qemu_check:  bool      = False
    cleanup:            bool      = True


@dataclass
class PipelineTest:
    id:                 str
    tags:               List[str]
    description:        str
    tool:               str
    input_args:         Dict[str, Any]
    category:           str            = "valid"    # "valid" | "broken" | "missing" | "conflict" | "foreign" | "junk"
    expect_success:     Optional[bool] = None       # None = skip success check
    expect_clarify:     bool           = False      # True = context gate must fire
    expect_layer:       Optional[str]  = None       # "context_gate" | "executor" | "ok"
    expect_result_keys: List[str]      = field(default_factory=list)


@dataclass
class TestResult:
    test_id:       str
    layer:         int
    passed:        bool
    issues:        List[str]
    fixes_applied: List[str]
    duration_s:    float
    detail:        Dict[str, Any] = field(default_factory=dict)

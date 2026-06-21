"""
tests/layer10_pipeline_full.py — Full pipeline: context assistant + gate + executor.

Simulates exactly what the AI loop does after the AI produces a tool call:
    1. check_context(prompt, tool_name, args)   ← context assistant (soft nudge)
    2. execute_tool(tool_name, args)             ← gate → sanitizer → executor

What this catches that Layers 8 and 9 don't:
  · The assistant fires a hint when it shouldn't (false positive — noise for user)
  · The assistant stays silent when it should warn (false negative — silent bug)
  · Two layers conflict: assistant says nothing but gate still blocks a
    field the user DID mention in their prompt (the double-ask bug from the
    AI loop side — assistant didn't warn, gate blocked anyway)
  · Contradictory assistant hint + gate pass (assistant warns but gate doesn't block)

Categories:
  CLEAN     — prompt matches tool and args; assistant silent; gate passes; executor runs
  MISMATCH  — AI picked wrong tool for the prompt; assistant should fire a hint
  HALLUC    — AI made up a field value not mentioned in the prompt; assistant should fire
  CONTRA    — prompt is self-contradictory; assistant should fire
  GATED     — prompt mentions the entity but AI omitted a required arg; gate fires
  COMBO     — mismatch or hallucination AND gate fires — tests both layers together
"""

import time, random as _random, traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .shared import TestResult, execute_tool
from provider.ai.context_assistant import check_context


# ── Full-pipeline test dataclass ──────────────────────────────────────────────

@dataclass
class FullPipelineTest:
    id:                  str
    tags:                List[str]
    description:         str
    prompt:              str        # what the user said
    tool:                str        # what the AI chose to call
    args:                Dict[str, Any]
    category:            str        # clean | mismatch | halluc | contra | gated | combo

    # Context assistant expectations
    expect_hint:         bool  = False    # True = assistant MUST return a hint
    hint_contains:       str   = ""       # substring the hint must include (if expect_hint)

    # Gate / executor expectations (same semantics as Layer 9)
    expect_gate:         bool  = False    # True = gate must fire
    expect_exec_success: Optional[bool] = None   # None = don't assert
    expect_result_keys:  List[str]        = field(default_factory=list)

    # Context for multi-turn scenarios
    recent_context:      str   = ""


# ── Shorthand builders ─────────────────────────────────────────────────────────

def _clean(tool, args, prompt, note="", keys=None):
    """Assistant silent + gate passes + executor runs cleanly."""
    return FullPipelineTest(
        id=f"p10_clean_{tool}_{list(args.values())[0]!s:.12}" if args else f"p10_clean_{tool}",
        tags=["full_pipeline","clean",tool],
        description=f"[clean] {note or tool}",
        prompt=prompt, tool=tool, args=args,
        category="clean",
        expect_hint=False, expect_gate=False,
        expect_exec_success=True,
        expect_result_keys=keys or [],
    )

def _cleans(tool, args, prompt, note=""):
    """Clean but executor result is state-dependent."""
    return FullPipelineTest(
        id=f"p10_clean_{tool}_{list(args.values())[0]!s:.12}" if args else f"p10_clean_{tool}",
        tags=["full_pipeline","clean","state_dep",tool],
        description=f"[clean/state] {note or tool}",
        prompt=prompt, tool=tool, args=args,
        category="clean",
        expect_hint=False, expect_gate=False,
        expect_exec_success=None,
    )

def _cleank(tool, args, prompt, keys, note=""):
    """Clean but tool returns no success key — check keys instead."""
    return FullPipelineTest(
        id=f"p10_clean_{tool}_{list(args.values())[0]!s:.12}" if args else f"p10_clean_{tool}",
        tags=["full_pipeline","clean",tool],
        description=f"[clean/keys] {note or tool}",
        prompt=prompt, tool=tool, args=args,
        category="clean",
        expect_hint=False, expect_gate=False,
        expect_exec_success=None,
        expect_result_keys=keys,
    )

def _mm(tool, args, prompt, hint_frag="", note=""):
    """Tool mismatch — assistant should fire."""
    return FullPipelineTest(
        id=f"p10_mm_{tool}_{prompt[:20].replace(' ','_')}",
        tags=["full_pipeline","mismatch",tool],
        description=f"[mismatch] {note or prompt[:40]}",
        prompt=prompt, tool=tool, args=args,
        category="mismatch",
        expect_hint=True, hint_contains=hint_frag,
        expect_gate=False, expect_exec_success=None,
    )

def _hal(tool, args, prompt, hint_frag="", note=""):
    """Hallucinated value — assistant should fire."""
    return FullPipelineTest(
        id=f"p10_hal_{tool}_{prompt[:20].replace(' ','_')}",
        tags=["full_pipeline","halluc",tool],
        description=f"[halluc] {note or prompt[:40]}",
        prompt=prompt, tool=tool, args=args,
        category="halluc",
        expect_hint=True, hint_contains=hint_frag,
        expect_gate=False, expect_exec_success=None,
    )

def _gated(tool, args, prompt, note=""):
    """User mentioned entity but AI omitted it → gate fires."""
    return FullPipelineTest(
        id=f"p10_gated_{tool}_{prompt[:20].replace(' ','_')}",
        tags=["full_pipeline","gated",tool],
        description=f"[gated] {note or prompt[:40]}",
        prompt=prompt, tool=tool, args=args,
        category="gated",
        expect_hint=False, expect_gate=True,
        expect_exec_success=False,
    )

def _combo(tool, args, prompt, hint_frag="", note=""):
    """Both assistant fires (mismatch/halluc) AND gate fires."""
    return FullPipelineTest(
        id=f"p10_combo_{tool}_{prompt[:20].replace(' ','_')}",
        tags=["full_pipeline","combo",tool],
        description=f"[combo] {note or prompt[:40]}",
        prompt=prompt, tool=tool, args=args,
        category="combo",
        expect_hint=True, hint_contains=hint_frag,
        expect_gate=True,
        expect_exec_success=False,
    )


# ── Test matrix ────────────────────────────────────────────────────────────────

FULL_TESTS: List[FullPipelineTest] = [

    # ── CLEAN — assistant quiet, gate silent, executor runs ───────────────────

    _clean("create_vm",   {"name":"probe10_min","os_type":"linux"},
           "create a vm named probe10_min", "minimal create"),
    _clean("create_vm",   {"name":"probe10_cpu","os_type":"linux","memory_mb":4096,"cpu_cores":4},
           "create a vm named probe10_cpu with 4 cores and 4gb ram"),
    _clean("create_vm",   {"name":"probe10_win","os_type":"windows","memory_mb":8192},
           "create a windows vm named probe10_win with 8gb ram"),
    _clean("create_profile",{"profile_name":"probe10_p","description":"test","memory_mb":2048},
           "create a profile named probe10_p", "profile create"),
    _cleank("check_system",  {}, "what hardware can run vms", ["kvm_available"], "no args — assistant never fires"),
    _cleank("list_vms",      {}, "show me my vms", [],          "no args"),
    _cleank("list_profiles", {}, "list profiles",  [],          "no args"),
    # Safe read-only / state-dependent ops — use probe10_min (created above)
    # Note: hello2/office not referenced here to prevent depending on external VM state.
    # Probe VMs are created earlier in this test list and are safe to query.
    _cleank("vm_status",     {"name":"probe10_min"}, "what is the status of probe10_min", ["name","state"]),
    _clean("show_config",    {"name":"probe10_min"}, "show config for probe10_min"),
    _clean("snapshot_list",  {"name":"probe10_min"}, "list snapshots for probe10_min"),
    _clean("print_command",  {"name":"probe10_min"}, "print the command for probe10_min"),
    _cleans("launch_vm",     {"name":"probe10_min","dry_run":True}, "launch probe10_min in dry run mode"),

    # ── MISMATCH — AI picked the wrong tool ───────────────────────────────────
    # All mismatch tests use safe args: ghost VMs (nonexistent) for destructive ops,
    # or probe10_ VMs for read-only ops. Never use real user VMs in destructive calls.

    # Prompt says "list" but AI called create_vm — safe: creates a probe VM
    _mm("create_vm", {"name":"probe10_err","os_type":"linux"},
        "show me all my vms", "list", "prompt=list vms, tool=create_vm"),

    # Prompt says "status" but AI called delete_vm — SAFE: ghost VM doesn't exist
    _mm("delete_vm", {"name":"ghost_mm_xyz"},
        "what is the status of my vm", "", "prompt=status, tool=delete_vm — ghost VM so no deletion"),

    # Prompt says "snapshot" but AI called clone_vm — SAFE: ghost source fails cleanly
    _mm("clone_vm", {"source_name":"ghost_mm_src","new_name":"probe10_clone"},
        "take a snapshot of my vm", "", "prompt=snapshot, tool=clone_vm — ghost source"),

    # Prompt says "delete" but AI called show_config — mismatch fires; list_vms is a recon
    # tool and intentionally never flagged (AI may call it before deleting to find the VM name)
    _mm("show_config", {"name":"probe10_min"},
        "delete my vm please", "", "prompt=delete, tool=show_config — not recon so mismatch fires"),

    # Prompt says "profile" but AI called create_vm — "make a profile" in cleaned text
    # triggers create_profile specifically, specificity rule removes create_vm from hints
    _mm("create_vm", {"name":"probe10_mm","os_type":"linux"},
        "make a profile named myprofile", "", "prompt=profile, tool=create_vm"),

    # ── HALLUCINATION — AI invented a value the user didn't mention ───────────
    # Halluc tests use probe10_ names so they get cleaned up and don't affect user VMs.
    # The key is that the NAME was not mentioned in the prompt — assistant should flag it.

    # User never said the VM name; AI made one up
    _hal("create_vm", {"name":"probe10_halluc_a","os_type":"linux"},
         "make me a linux vm", "probe10_halluc_a",
         "name not mentioned in prompt — AI hallucinated it"),

    # User gave no VM name; AI sent a hallucinated name — _ext_slots["name"]=None fires it
    _hal("show_config", {"name":"probe10_halluc_b"},
         "show me the vm config", "",
         "no name in prompt — AI hallucinated 'probe10_halluc_b' as name"),

    # Optional field (disk_size_gb) not tracked by assistant — assistant stays silent,
    # name IS in the prompt so no halluc either; this is a clean call
    _clean("create_vm", {"name":"probe10_hal","os_type":"linux","disk_size_gb":500},
           "make me a linux vm named probe10_hal",
           "optional disk_size_gb not tracked — assistant silent, executor runs"),

    # ── GATED — prompt mentions entity but AI omitted required gate field ──────
    # "create a vm" — AI sent only os_type, forgot name → gate asks for name
    _gated("create_vm", {"os_type":"linux"},
           "create a linux vm", "os_type present but name omitted → gate asks for name"),

    # "create a vm named test1" — AI sent only name, forgot os_type
    # This is the original reported bug scenario. Gate must ask for ONLY os_type,
    # not name (which was provided). A double-ask bug would show name in missing list.
    _gated("create_vm", {"name":"probe10_t1"},
           "create a vm named probe10_t1",
           "name provided but os_type missing — gate must ask ONLY os_type"),

    # "show config for probe10_min" — AI forgot to pass name → gate asks for name
    _gated("show_config", {},
           "show config for probe10_min", "name omitted entirely"),

    # "take a snapshot" — AI omitted snap_name → gate asks for snap_name
    _gated("snapshot_create", {"name":"probe10_min"},
           "take a snapshot of probe10_min", "name ok but snap_name missing"),

    # "resize disk" — AI forgot new_size_gb
    _gated("resize_disk", {"name":"probe10_min"},
           "resize the disk for probe10_min", "new_size_gb missing"),

    # "send monitor command" — AI forgot cmd
    _gated("send_monitor_cmd", {"name":"probe10_min"},
           "send a monitor command to probe10_min", "cmd field missing"),

    # ── COMBO — mismatch/halluc AND gate fires ────────────────────────────────
    # Wrong tool + missing required field
    _combo("create_vm", {},
           "show me the status of all my vms", "",
           "list prompt but AI called create_vm with no args"),

    # Hallucinated name + missing os_type
    _combo("create_vm", {"name":"totally_invented_name"},
           "launch office please", "",
           "wrong intent + hallucinated name + missing os_type"),

    # ── The original reported double-ask scenario ─────────────────────────────
    # Full simulation: "create a vm named test1" → AI sends {name: "test1"} only
    # Gate fires for os_type. Test verifies:
    # 1. Gate asks ONLY for os_type (not name — that would be the bug)
    # 2. Assistant does NOT add extra noise (name was clearly in the prompt)
    FullPipelineTest(
        id="p10_double_ask_scenario",
        tags=["full_pipeline","double_ask","create_vm","regression"],
        description="[regression] 'create a vm named test1' — AI sends only name; gate must ask ONLY os_type",
        prompt="create a vm named test1",
        tool="create_vm",
        args={"name":"test1"},
        category="gated",
        expect_hint=False,  # assistant should not fire — name is in the prompt
        expect_gate=True,
        expect_exec_success=False,
        expect_result_keys=["os_type"],  # gate must ask for ONLY this field
    ),

    # Cleanup test
    _clean("delete_profile", {"profile_name":"probe10_p"},
           "delete the probe10_p profile", "cleanup profile created above"),
]


# ── Randomised test generator ─────────────────────────────────────────────────
#
# Generates tests with randomly chosen clean/mismatch/gated scenarios.
# Uses a small set of plausible prompts per tool to keep tests realistic.

_TOOL_PROMPTS: Dict[str, Dict] = {
    "create_vm":   {
        "clean":    "create a vm named {name} with os {os_type}",
        "mismatch": "show me my vms",
        "gated_omit": ["os_type"],   # omit this field to trigger gate
    },
    "show_config": {
        "clean":    "show config for {name}",
        "mismatch": "launch {name}",
        "gated_omit": [],
    },
    "vm_status":   {
        "clean":    "what is the status of {name}",
        "mismatch": "stop {name}",
        "gated_omit": [],
    },
    "snapshot_list":{
        "clean":    "list snapshots for {name}",
        "mismatch": "take a snapshot of {name}",
        "gated_omit": [],
    },
    "list_vms":    {
        "clean":    "list my vms",
        "mismatch": "create a new vm",
        "gated_omit": [],
    },
    "check_system":{
        "clean":    "check system capabilities",
        "mismatch": "create a vm",
        "gated_omit": [],
    },
    "launch_vm":   {
        "clean":    "launch {name}",
        "mismatch": "show the config for {name}",
        "gated_omit": [],
    },
    "stop_vm":     {
        "clean":    "stop {name}",
        "mismatch": "launch {name}",
        "gated_omit": [],
    },
    "create_profile":{
        "clean":    "create a profile named {profile_name} with description {description}",
        "mismatch": "list profiles",
        "gated_omit": ["description"],
    },
    "snapshot_create":{
        "clean":    "take a snapshot of {name} named {snap_name}",
        "mismatch": "list snapshots for {name}",
        "gated_omit": ["snap_name"],
    },
}

# Random tests use probe10r_ VMs (cleaned up) or safe read-only real VMs.
# Never use real user VMs in mismatch/gated tests that could trigger destructive tools.
_RAND_VM_NAMES_SAFE   = ["probe10_min"]  # probe VMs created earlier in the test list
_RAND_VM_NAMES_RO     = ["probe10_min"]  # for read-only ops
_RAND_PROFILE_NAMES   = ["minimal","raspberry_pi_3b"]


def generate_random_full_tests(n: int = 20, seed: Optional[int] = None) -> List[FullPipelineTest]:
    rng   = _random.Random(seed)
    tests: List[FullPipelineTest] = []
    tools = list(_TOOL_PROMPTS.keys())
    uid   = 0

    while len(tests) < n:
        tool   = rng.choice(tools)
        schema = _TOOL_PROMPTS[tool]
        mode   = rng.choice(["clean", "clean", "mismatch", "gated"])
        uid   += 1

        if mode == "clean":
            name    = rng.choice(_RAND_VM_NAMES_SAFE)
            pname   = rng.choice(_RAND_PROFILE_NAMES)
            sname   = f"probe10r_snap_{uid:04d}"
            desc    = "random profile"
            os_type = rng.choice(["linux","windows"])
            args: Dict[str, Any] = {}
            if tool == "create_vm":
                vname  = f"probe10r_{uid:04d}"
                args   = {"name": vname, "os_type": os_type}
                prompt = schema["clean"].format(name=vname, os_type=os_type)
            elif tool in ("show_config","vm_status","snapshot_list"):
                args   = {"name": name}
                prompt = schema["clean"].format(name=name)
            elif tool in ("launch_vm","stop_vm"):
                # state-dep: use probe VM name but don't assert success
                args   = {"name": name, "dry_run": True} if tool == "launch_vm" else {"name": name}
                prompt = schema["clean"].format(name=name)
            elif tool == "create_profile":
                pname2 = f"probe10r_p_{uid:04d}"
                args   = {"profile_name": pname2, "description": desc, "memory_mb": 2048}
                prompt = schema["clean"].format(profile_name=pname2, description=desc)
            elif tool == "snapshot_create":
                args   = {"name": name, "snap_name": sname}
                prompt = schema["clean"].format(name=name, snap_name=sname)
            elif tool in ("list_vms","check_system"):
                args   = {}
                prompt = schema["clean"]
            else:
                args   = {}
                prompt = schema["clean"]

            tests.append(FullPipelineTest(
                id=f"p10_rand_clean_{tool}_{uid:04d}",
                tags=["full_pipeline","random","clean",tool],
                description=f"[random/clean] {tool}",
                prompt=prompt, tool=tool, args=args,
                category="clean",
                expect_hint=False, expect_gate=False,
                expect_exec_success=None,
            ))

        elif mode == "mismatch":
            # Recon tools (list_vms, check_system) are intentionally never flagged by
            # the assistant — skip them for mismatch tests to avoid false failures.
            _RECON = {"list_vms", "check_system"}
            if tool in _RECON:
                continue
            name   = rng.choice(_RAND_VM_NAMES_SAFE)
            prompt = schema["mismatch"].format(name=name) if "{name}" in schema["mismatch"] else schema["mismatch"]
            # Provide valid args so gate doesn't fire (testing mismatch only)
            # Use probe10r_ names for anything that creates state; safe ghost names for destructive ops
            if tool == "create_vm":
                args = {"name": f"probe10r_{uid:04d}", "os_type": "linux"}
            elif tool in ("show_config","vm_status","snapshot_list"):
                args = {"name": name}
            elif tool in ("launch_vm","stop_vm"):
                args = {"name": name}
            elif tool == "create_profile":
                args = {"profile_name": f"probe10r_p_{uid:04d}", "description": "test", "memory_mb": 2048}
            elif tool == "snapshot_create":
                args = {"name": name, "snap_name": f"probe10r_snap_{uid:04d}"}
            else:
                args = {}
            tests.append(FullPipelineTest(
                id=f"p10_rand_mm_{tool}_{uid:04d}",
                tags=["full_pipeline","random","mismatch",tool],
                description=f"[random/mismatch] {tool} — prompt implies different action",
                prompt=prompt, tool=tool, args=args,
                category="mismatch",
                expect_hint=True, hint_contains="",
                expect_gate=False, expect_exec_success=None,
            ))

        elif mode == "gated":
            gated_omit = schema.get("gated_omit", [])
            if not gated_omit:
                # Tool has no multi-field gate requirement — skip
                continue
            name   = rng.choice(_RAND_VM_NAMES_SAFE)
            if tool == "create_vm":
                vname  = f"probe10r_{uid:04d}"
                args   = {"name": vname}
                prompt = f"create a vm named {vname}"
            elif tool == "create_profile":
                pname2 = f"probe10r_p_{uid:04d}"
                args   = {"profile_name": pname2}
                prompt = f"create a profile named {pname2}"
            elif tool == "snapshot_create":
                args   = {"name": name}
                prompt = f"take a snapshot of {name}"
            else:
                args   = {}
                prompt = schema.get("clean", tool)

            tests.append(FullPipelineTest(
                id=f"p10_rand_gated_{tool}_{uid:04d}",
                tags=["full_pipeline","random","gated",tool],
                description=f"[random/gated] {tool} — {gated_omit} omitted",
                prompt=prompt, tool=tool, args=args,
                category="gated",
                expect_hint=False,
                expect_gate=True,
                expect_exec_success=False,
                expect_result_keys=gated_omit,
            ))

    return tests


# ── Cleanup ────────────────────────────────────────────────────────────────────

def cleanup_full_artifacts():
    import shutil, os
    from client.executioner.tool_executor import execute_tool as _et
    from client.api.qemu_config import get_all_profiles, delete_custom_profile

    vm_dir = os.path.expanduser("~/.qemu_vms")
    if os.path.isdir(vm_dir):
        for entry in os.listdir(vm_dir):
            if entry.startswith("probe10"):
                try:
                    _et("delete_vm", {"name": entry}, verbose=False, skip_gate=True)
                except Exception:
                    shutil.rmtree(os.path.join(vm_dir, entry), ignore_errors=True)

    for pname in list(get_all_profiles().keys()):
        if pname.startswith("probe10"):
            try:
                delete_custom_profile(pname)
            except Exception:
                pass


# ── Runner ────────────────────────────────────────────────────────────────────

def run_full_test(tc: FullPipelineTest) -> TestResult:
    """
    1. Run context assistant — check_context(prompt, tool, args)
    2. Run execute_tool with gate ACTIVE (skip_gate=False)
    3. Validate both results against expectations
    """
    start  = time.time()
    issues: List[str] = []

    # Step 1 — context assistant
    hint: Optional[str] = None
    try:
        hint = check_context(tc.prompt, tc.tool, tc.args, tc.recent_context)
    except Exception:
        tb = traceback.format_exc()
        issues.append(f"context_assistant raised exception: {tb[:150]}")

    # Step 2 — execute_tool (gate active)
    result: Dict[str, Any] = {}
    try:
        raw = execute_tool(tc.tool, dict(tc.args), verbose=True, skip_gate=False)
        if isinstance(raw, list):
            result = {"success": True, "_list_len": len(raw)}
        else:
            result = raw
    except Exception:
        tb = traceback.format_exc()
        issues.append(f"execute_tool raised exception: {tb[:150]}")
        result = {"success": False, "error": tb[:150]}

    # ── Classify executor result ──────────────────────────────────────────────
    gate_fired    = bool(result.get("clarify") and "missing" in result)
    actual_success = result.get("success")
    actual_missing = [m.get("field") for m in result.get("missing", [])] if gate_fired else []
    double_ask     = False

    # ── Validate assistant ────────────────────────────────────────────────────
    if tc.expect_hint and hint is None:
        issues.append(f"Expected assistant hint but got None (category={tc.category})")
    if not tc.expect_hint and hint is not None:
        # Only flag as hard failure for clean tests — mismatch/halluc may
        # legitimately produce hints we didn't predict
        if tc.category == "clean":
            issues.append(f"Assistant fired unexpectedly on clean call: {hint[:80]}")
    if tc.expect_hint and tc.hint_contains and hint and tc.hint_contains not in hint:
        issues.append(f"Hint missing expected fragment '{tc.hint_contains}': {hint[:80]}")

    # ── Validate gate ─────────────────────────────────────────────────────────
    if tc.category == "gated":
        if not gate_fired:
            issues.append(f"Expected gate to fire but it didn't (success={actual_success})")
        else:
            # Double-ask: gate asked for a field that was actually provided in args
            provided_fields = {k for k, v in tc.args.items() if v not in (None, "", "  ")}
            double_asked    = [f for f in actual_missing if f in provided_fields]
            expected_missing = tc.expect_result_keys  # fields we expect gate to ask for
            not_asked        = [f for f in expected_missing if f not in actual_missing]
            if double_asked:
                double_ask = True
                issues.append(f"DOUBLE-ASK BUG: gate asked for field already in args: {double_asked}")
            if not_asked:
                issues.append(f"Gate missed expected fields: {not_asked}")

    elif tc.category == "clean":
        if gate_fired:
            provided = [f for f in actual_missing if tc.args.get(f) not in (None, "")]
            double_ask = bool(provided)
            msg = (f"DOUBLE-ASK BUG: gate blocked provided fields: {provided}"
                   if double_ask else
                   f"Gate blocked on clean call — missing: {actual_missing}")
            issues.append(msg)
        elif tc.expect_exec_success is True and actual_success is not True:
            issues.append(f"Expected executor success=True got {actual_success} — {result.get('error','')}")
        for key in tc.expect_result_keys:
            if key not in result:
                issues.append(f"Result missing key '{key}'")

    elif tc.category in ("mismatch", "halluc", "contra", "combo"):
        if tc.expect_gate and not gate_fired:
            issues.append("Expected gate to fire but it didn't")
        # For mismatch/halluc we don't hard-fail on executor result — assistant is the main check

    return TestResult(
        test_id=tc.id, layer=10,
        passed=len(issues) == 0,
        issues=issues, fixes_applied=[],
        duration_s=time.time() - start,
        detail={
            "category":        tc.category,
            "tool":            tc.tool,
            "prompt":          tc.prompt,
            "args":            tc.args,
            "hint":            hint,
            "gate_fired":      gate_fired,
            "actual_missing":  actual_missing,
            "double_ask":      double_ask,
            "actual_success":  actual_success,
            "error":           result.get("error"),
            "state_dep":       tc.expect_exec_success is None,
        },
    )

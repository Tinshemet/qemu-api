"""
tests/layer7_context_assistant.py — Layer 7: Context Assistant unit tests.

Tests check_context() in isolation — pure function, no AI, no network, instant.
Covers fixed cases for all three check types plus randomised tests generated
from the context assistant config by varying prompts, tools, and args.

Four randomised categories (mirrors layer 2 and layer 6 patterns):
  mismatch    — prompt hints at tool A, AI called tool B → must fire
  hallucinated — required field provided by AI but not mentioned in prompt → must fire
  high_stakes  — high-stakes optional field set without user mention → must fire
  clean        — all required fields grounded in prompt → must pass silently
"""

import json, pathlib, random, time, traceback
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field

from .shared import ContextAssistantTest, TestResult, check_context

_CFG_PATH = pathlib.Path(__file__).parents[1] / "provider" / "ai" / "context_assistant_config.json"
with _CFG_PATH.open() as _f:
    _CA_CFG = json.load(_f)

_TOOL_HINTS:      Dict[str, List[str]] = _CA_CFG["tool_hints"]
_REQUIRED_FIELDS: Dict[str, List[str]] = _CA_CFG["required_fields"]
_HIGH_STAKES:     Dict[str, List[str]] = _CA_CFG["high_stakes_optional"]

# Distinctive substrings from each message template in context_assistant_config.json
_TYPE_SIGNATURES: Dict[str, str] = {
    "mismatch":       "hints at",
    "hallucinated":   "never mentioned it",
    "high_stakes":    "high-stakes",
    "contradictory":  "contradictory",
}


# ─────────────────────────────────────────────
#  FIXED TEST CASES
# ─────────────────────────────────────────────

CA_TESTS: List[ContextAssistantTest] = [

    # ── Clean passes ──────────────────────────
    ContextAssistantTest(
        id="ca_clean_create_vm_grounded",
        tags=["context_assistant", "clean"],
        description="All required fields explicitly in prompt — no hint",
        prompt="create a linux vm called dev-box",
        tool_name="create_vm",
        args={"name": "dev-box", "os_type": "linux", "memory_mb": 4096},
        expect_fired=False,
    ),
    ContextAssistantTest(
        id="ca_clean_launch_vm_grounded",
        tags=["context_assistant", "clean"],
        description="Name from prompt matches AI arg — no hint",
        prompt="start dev-box please",
        tool_name="launch_vm",
        args={"name": "dev-box"},
        expect_fired=False,
    ),
    ContextAssistantTest(
        id="ca_clean_stop_vm_grounded",
        tags=["context_assistant", "clean"],
        description="'shut down dev-box' — name grounded via multi-word action pattern",
        prompt="shut down dev-box",
        tool_name="stop_vm",
        args={"name": "dev-box"},
        expect_fired=False,
    ),
    ContextAssistantTest(
        id="ca_clean_snapshot_delete_grounded",
        tags=["context_assistant", "clean", "snapshot"],
        description="Snapshot and VM name both grounded in prompt",
        prompt="delete snapshot pre-update on dev-box",
        tool_name="snapshot_delete",
        args={"name": "dev-box", "snap_name": "pre-update"},
        expect_fired=False,
    ),
    ContextAssistantTest(
        id="ca_clean_no_hints_ambiguous",
        tags=["context_assistant", "clean", "no_hints"],
        description="Ambiguous prompt with no tool hints — assistant stays silent",
        prompt="do something with dev-box",
        tool_name="vm_status",
        args={"name": "dev-box"},
        expect_fired=False,
    ),
    ContextAssistantTest(
        id="ca_clean_compound_create_then_launch",
        tags=["context_assistant", "clean", "compound"],
        description="Compound prompt — create_vm is valid for first tool call",
        prompt="create a linux vm called staging and then launch it",
        tool_name="create_vm",
        args={"name": "staging", "os_type": "linux"},
        expect_fired=False,
    ),
    ContextAssistantTest(
        id="ca_clean_compound_launch_second_call",
        tags=["context_assistant", "clean", "compound"],
        description="Compound prompt — launch_vm is valid for second tool call",
        prompt="create a linux vm called staging and then launch it",
        tool_name="launch_vm",
        args={"name": "staging"},
        expect_fired=False,
    ),
    ContextAssistantTest(
        id="ca_clean_safe_defaults_ignored",
        tags=["context_assistant", "clean"],
        description="Untracked optional fields (memory, cores) pass through silently",
        prompt="make a vm called work-box",
        tool_name="create_vm",
        args={"name": "work-box", "memory_mb": 4096, "cpu_cores": 2, "disk_size_gb": 40},
        expect_fired=False,
    ),

    # ── Tool mismatch ─────────────────────────
    ContextAssistantTest(
        id="ca_mismatch_create_prompt_delete_called",
        tags=["context_assistant", "mismatch"],
        description="Prompt says create, AI calls delete_vm — must fire",
        prompt="create a vm called dev-box",
        tool_name="delete_vm",
        args={"name": "dev-box"},
        expect_fired=True,
        expect_type="mismatch",
    ),
    ContextAssistantTest(
        id="ca_mismatch_launch_prompt_create_called",
        tags=["context_assistant", "mismatch"],
        description="Prompt says launch/start, AI calls create_vm — must fire",
        prompt="launch my vm called test-1",
        tool_name="create_vm",
        args={"name": "test-1"},
        expect_fired=True,
        expect_type="mismatch",
    ),
    ContextAssistantTest(
        id="ca_mismatch_delete_snapshot_delete_vm_called",
        tags=["context_assistant", "mismatch", "snapshot"],
        description="'delete snapshot' specifically — calling delete_vm is wrong",
        prompt="delete the snapshot called pre-update on dev-box",
        tool_name="delete_vm",
        args={"name": "dev-box"},
        expect_fired=True,
        expect_type="mismatch",
    ),
    ContextAssistantTest(
        id="ca_mismatch_kill_maps_to_stop_not_delete",
        tags=["context_assistant", "mismatch"],
        description="'kill prod-vm' should hint stop_vm, not delete_vm",
        prompt="kill prod-vm now",
        tool_name="delete_vm",
        args={"name": "prod-vm"},
        expect_fired=True,
        expect_type="mismatch",
    ),

    # ── Specificity rules (should NOT mismatch) ──
    ContextAssistantTest(
        id="ca_specificity_delete_snapshot_suppresses_delete_vm",
        tags=["context_assistant", "specificity"],
        description="snapshot_delete suppresses delete_vm hint — no mismatch",
        prompt="delete snapshot snap1 on dev-box",
        tool_name="snapshot_delete",
        args={"name": "dev-box", "snap_name": "snap1"},
        expect_fired=False,
    ),

    # ── Hallucinated required fields ──────────
    ContextAssistantTest(
        id="ca_hallucinated_name_create_vm",
        tags=["context_assistant", "hallucinated"],
        description="AI invents VM name when user said 'create a vm' — must fire",
        prompt="create a vm",
        tool_name="create_vm",
        args={"name": "my-linux-vm", "os_type": "linux"},
        expect_fired=True,
        expect_type="hallucinated",
    ),
    ContextAssistantTest(
        id="ca_hallucinated_name_placeholder",
        tags=["context_assistant", "hallucinated"],
        description="AI uses placeholder name like 'windows-vm' — must fire",
        prompt="create me a windows machine",
        tool_name="create_vm",
        args={"name": "windows-vm", "os_type": "windows"},
        expect_fired=True,
        expect_type="hallucinated",
    ),
    ContextAssistantTest(
        id="ca_hallucinated_name_vague_stop",
        tags=["context_assistant", "hallucinated"],
        description="'shut it down' — no VM name in prompt, AI guesses one",
        prompt="shut it down",
        tool_name="stop_vm",
        args={"name": "work-box"},
        expect_fired=True,
        expect_type="hallucinated",
    ),
    ContextAssistantTest(
        id="ca_hallucinated_new_name_clone",
        tags=["context_assistant", "hallucinated"],
        description="'clone dev-box' — AI invents new_name not in prompt",
        prompt="clone dev-box",
        tool_name="clone_vm",
        args={"source_name": "dev-box", "new_name": "dev-box-copy"},
        expect_fired=True,
        expect_type="hallucinated",
    ),

    # ── Contradictory intent ──────────────────
    ContextAssistantTest(
        id="ca_contra_create_then_delete_same_vm",
        tags=["context_assistant", "contradictory"],
        description="Create and delete the same VM in one prompt — must fire",
        prompt="create dev-box and then delete dev-box",
        tool_name="create_vm",
        args={"name": "dev-box"},
        expect_fired=True,
        expect_type="contradictory",
    ),
    ContextAssistantTest(
        id="ca_contra_start_then_stop_same_vm",
        tags=["context_assistant", "contradictory"],
        description="Start and stop the same VM in one prompt — must fire",
        prompt="start dev-box and then stop dev-box",
        tool_name="launch_vm",
        args={"name": "dev-box"},
        expect_fired=True,
        expect_type="contradictory",
    ),
    ContextAssistantTest(
        id="ca_contra_create_then_delete_different_vms",
        tags=["context_assistant", "contradictory", "clean"],
        description="Create dev-box and delete staging — different targets, no fire",
        prompt="create dev-box and delete staging",
        tool_name="create_vm",
        args={"name": "dev-box"},
        expect_fired=False,
    ),
    ContextAssistantTest(
        id="ca_contra_snap_create_then_delete",
        tags=["context_assistant", "contradictory", "snapshot"],
        description="Take a snapshot then delete the same snapshot — must fire",
        prompt="take snapshot snap-1 on dev-box and delete snapshot snap-1",
        tool_name="snapshot_create",
        args={"name": "dev-box", "snap_name": "snap-1"},
        expect_fired=True,
        expect_type="contradictory",
    ),

    # ── Recon tools never fire ────────────────
    # Recon/query tools are always valid precursors — the AI legitimately calls
    # list_vms before launching, scan_isos before creating, etc. They must
    # never be flagged as mismatches regardless of what the prompt hinted at.
    ContextAssistantTest(
        id="ca_recon_list_vms_never_fires",
        tags=["context_assistant", "recon", "clean"],
        description="Prompt hints at create_vm, AI calls list_vms first — no hint",
        prompt="create a linux vm called dev-box",
        tool_name="list_vms",
        args={},
        expect_fired=False,
    ),
    ContextAssistantTest(
        id="ca_recon_scan_isos_never_fires",
        tags=["context_assistant", "recon", "clean"],
        description="Prompt hints at delete, AI calls scan_isos first — no hint",
        prompt="delete vm dev-box",
        tool_name="scan_isos",
        args={},
        expect_fired=False,
    ),
    ContextAssistantTest(
        id="ca_recon_monitor_vm_never_fires",
        tags=["context_assistant", "recon", "clean"],
        description="Prompt hints at create, AI calls monitor_vm first — no hint",
        prompt="create dev-box linux",
        tool_name="monitor_vm",
        args={"name": "dev-box"},
        expect_fired=False,
    ),
    ContextAssistantTest(
        id="ca_recon_check_system_never_fires",
        tags=["context_assistant", "recon", "clean"],
        description="Prompt hints at launch, AI calls check_system first — no hint",
        prompt="launch dev-box",
        tool_name="check_system",
        args={},
        expect_fired=False,
    ),
    ContextAssistantTest(
        id="ca_recon_list_profiles_never_fires",
        tags=["context_assistant", "recon", "clean"],
        description="Prompt hints at stop_vm, AI calls list_profiles — no hint",
        prompt="stop dev-box",
        tool_name="list_profiles",
        args={},
        expect_fired=False,
    ),

    # ── High-stakes optional fields ───────────
    ContextAssistantTest(
        id="ca_high_stakes_delete_disks_not_mentioned",
        tags=["context_assistant", "high_stakes"],
        description="AI sets delete_disks=True but user only said 'remove dev-box'",
        prompt="remove dev-box",
        tool_name="delete_vm",
        args={"name": "dev-box", "delete_disks": True},
        expect_fired=True,
        expect_type="high_stakes",
    ),
    ContextAssistantTest(
        id="ca_high_stakes_force_stop_not_mentioned",
        tags=["context_assistant", "high_stakes"],
        description="AI sets force=True on stop but user did not mention it",
        prompt="stop dev-box",
        tool_name="stop_vm",
        args={"name": "dev-box", "force": True},
        expect_fired=True,
        expect_type="high_stakes",
    ),
    ContextAssistantTest(
        id="ca_high_stakes_delete_disks_false_passes",
        tags=["context_assistant", "high_stakes", "clean"],
        description="delete_disks=False is not dangerous — must pass silently",
        prompt="delete vm dev-box",
        tool_name="delete_vm",
        args={"name": "dev-box", "delete_disks": False},
        expect_fired=False,
    ),
]


# ─────────────────────────────────────────────
#  RANDOMISED TEST GENERATORS
# ─────────────────────────────────────────────

_RAND_VM_NAMES   = ["dev-box", "work-vm", "test-rig", "prod-server",
                    "build-box", "ci-runner", "sandbox-1", "lab-vm"]
_RAND_SNAP_NAMES = ["pre-update", "baseline", "checkpoint-1",
                    "backup-snap", "snap-jan", "rollback-point"]
_RAND_CATEGORIES = ["mismatch", "hallucinated", "high_stakes", "clean"]

# Recon tools are exempt from mismatch checks — they must never appear as
# the called_tool in a mismatch test or the CA will return None instead of firing.
_RECON_TOOLS: set = {
    "list_vms", "scan_isos", "check_system",
    "list_profiles", "list_networks", "monitor_all", "monitor_vm",
}


def generate_random_ca_tests(n: int = 25, seed: int = 42) -> List[ContextAssistantTest]:
    """
    Generate N randomised context assistant tests across four categories.

    mismatch    — prompt hints at tool A, AI called a different tool → must fire
    hallucinated — required field set by AI but absent from prompt → must fire
    high_stakes  — high-stakes optional field set without user mention → must fire
    clean        — grounded prompt + matching args → must pass silently
    """
    rng   = random.Random(seed)
    tests: List[ContextAssistantTest] = []

    # Build lookups used across categories
    # Tools that have at least one trigger word
    hintable_tools   = [t for t, triggers in _TOOL_HINTS.items() if triggers]
    # Tools that have at least one required field tracked by slot_patterns
    hallucination_tools = [
        t for t, fields in _REQUIRED_FIELDS.items()
        if fields and t in _TOOL_HINTS
    ]
    high_stakes_tools = list(_HIGH_STAKES.keys())

    for i in range(n):
        category = _RAND_CATEGORIES[i % len(_RAND_CATEGORIES)]
        vm       = rng.choice(_RAND_VM_NAMES)
        snap     = rng.choice(_RAND_SNAP_NAMES)

        # ── mismatch ──────────────────────────────────────────────────────────
        if category == "mismatch":
            # Pick a tool to hint at, then pick a DIFFERENT tool to call
            hint_tool   = rng.choice(hintable_tools)
            trigger     = rng.choice(_TOOL_HINTS[hint_tool])
            prompt      = f"{trigger} {vm}"

            # Pick a called tool that isn't the hinted one, isn't suppressed,
            # and isn't a recon tool (recon tools are always exempt from CA checks).
            other_tools = [t for t in hintable_tools if t != hint_tool and t not in _RECON_TOOLS]
            called_tool = rng.choice(other_tools) if other_tools else hint_tool

            args: Dict[str, Any] = {"name": vm}
            if "snap_name" in _REQUIRED_FIELDS.get(called_tool, []):
                args["snap_name"] = snap

            tests.append(ContextAssistantTest(
                id=f"ca_rand_mismatch_{i:03d}",
                tags=["random", "context_assistant", "mismatch"],
                description=f"Prompt hints '{hint_tool}' via '{trigger}', AI called '{called_tool}'",
                prompt=prompt,
                tool_name=called_tool,
                args=args,
                expect_fired=True,
                expect_type="mismatch",
            ))

        # ── hallucinated ──────────────────────────────────────────────────────
        elif category == "hallucinated":
            tool   = rng.choice(hallucination_tools)
            # Use a vague prompt that doesn't mention any slot values
            trigger = rng.choice(_TOOL_HINTS.get(tool, ["do something"]))
            prompt  = f"{trigger} please"      # no vm name, no snap name

            # Set EVERY required field the tool actually tracks — not just "name".
            # clone_vm needs source_name/new_name (no "name" field at all), so
            # hardcoding {"name": vm} silently skipped the hallucination check
            # for it (the check only looks at fields in _REQUIRED_FIELDS[tool]).
            args: Dict[str, Any] = {}
            for field in _REQUIRED_FIELDS.get(tool, ["name"]):
                if field == "snap_name":
                    args[field] = snap
                elif field == "new_name":
                    args[field] = f"{vm}-copy"
                else:
                    args[field] = vm

            tests.append(ContextAssistantTest(
                id=f"ca_rand_hallucinated_{i:03d}",
                tags=["random", "context_assistant", "hallucinated"],
                description=f"'{tool}' — AI sets name={vm!r} but prompt has no VM name",
                prompt=prompt,
                tool_name=tool,
                args=args,
                expect_fired=True,
                expect_type="hallucinated",
            ))

        # ── high_stakes ───────────────────────────────────────────────────────
        elif category == "high_stakes":
            tool        = rng.choice(high_stakes_tools)
            hs_fields   = _HIGH_STAKES[tool]
            hs_field    = rng.choice(hs_fields)
            trigger     = rng.choice(_TOOL_HINTS.get(tool, ["do something"]))
            prompt      = f"{trigger} {vm}"     # no mention of the high-stakes field

            args = {"name": vm, hs_field: True}

            tests.append(ContextAssistantTest(
                id=f"ca_rand_high_stakes_{i:03d}",
                tags=["random", "context_assistant", "high_stakes"],
                description=f"'{tool}' — AI sets {hs_field}=True not mentioned in prompt",
                prompt=prompt,
                tool_name=tool,
                args=args,
                expect_fired=True,
                expect_type="high_stakes",
            ))

        # ── clean ─────────────────────────────────────────────────────────────
        else:
            tool    = rng.choice(hallucination_tools)
            trigger = rng.choice(_TOOL_HINTS.get(tool, ["do something"]))
            prompt  = f"{trigger} called {vm}"  # name is explicit

            args = {"name": vm}
            if "snap_name" in _REQUIRED_FIELDS.get(tool, []):
                prompt += f" snapshot {snap}"
                args["snap_name"] = snap

            tests.append(ContextAssistantTest(
                id=f"ca_rand_clean_{i:03d}",
                tags=["random", "context_assistant", "clean"],
                description=f"'{tool}' — all required fields grounded in prompt",
                prompt=prompt,
                tool_name=tool,
                args=args,
                expect_fired=False,
            ))

    return tests


# ─────────────────────────────────────────────
#  LAYER 7 RUNNER
# ─────────────────────────────────────────────

def run_ca_test(tc: ContextAssistantTest) -> TestResult:
    start  = time.time()
    issues: List[str] = []
    detail: Dict[str, Any] = {}
    try:
        result = check_context(tc.prompt, tc.tool_name, tc.args)
        detail["hint"] = result

        if tc.expect_fired:
            if result is None:
                issues.append(
                    f"Expected hint to fire (type={tc.expect_type!r}) "
                    f"but check_context returned None"
                )
            elif tc.expect_type:
                sig = _TYPE_SIGNATURES.get(tc.expect_type, "")
                if sig and sig not in result:
                    issues.append(
                        f"Expected hint type {tc.expect_type!r} "
                        f"but got: {result!r}"
                    )
        else:
            if result is not None:
                issues.append(
                    f"Expected check_context to return None "
                    f"but got hint: {result!r}"
                )

    except Exception:
        issues.append(f"Exception: {traceback.format_exc()}")

    return TestResult(
        test_id=tc.id, layer=7, passed=len(issues) == 0,
        issues=issues, fixes_applied=[],
        duration_s=time.time() - start, detail=detail,
    )

"""
tests/layer3_ai.py — Layer 3: AI integration tests (require Ollama).
"""

import json, time, traceback, random
from typing import Any, Dict, List, Optional, Tuple

import requests

from .shared import (
    AITest, TestResult,
    _sanitise_args, _build_system_prompt,
    OLLAMA_URL, OLLAMA_MODEL, TOOLS,
)
from shared.sanitizer.context_gate import gate_check


# ─────────────────────────────────────────────
#  RANDOM PROMPT SUBSTITUTION CONSTANTS
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
#  FIXED AI TEST CASES
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

    # ── Gate-aware tests ───────────────────────────────────────────────────────
    # These verify the AI provides all contextually-required args so the gate
    # never needs to fire. expect_gate_blocked=False means every tool call
    # from the AI must pass gate_check without a clarify response.

    AITest(
        id="ai_gate_create_explicit_args",
        tags=["gate","create_vm","basic"],
        description="AI provides name + os_type → gate passes (no clarification needed)",
        vagueness=1,
        prompt_pool=[
            "create a linux VM called {name}",
            "make a {os} VM named {name}",
            "new VM: {name}, os={os}",
        ],
        expect_tools=["create_vm"],
        expect_gate_blocked=False,
    ),
    AITest(
        id="ai_gate_snapshot_explicit_args",
        tags=["gate","snapshot"],
        description="AI provides name + snap_name → gate passes for snapshot_create",
        vagueness=1,
        prompt_pool=[
            "take a snapshot of the {name} VM called {snap}",
            "create snapshot {snap} on {name}",
            "snapshot {name} as {snap}",
        ],
        expect_tools=["snapshot_create"],
        expect_gate_blocked=False,
    ),
    AITest(
        id="ai_gate_monitor_cmd_explicit",
        tags=["gate","send_monitor_cmd"],
        description="AI provides name + cmd → gate passes for send_monitor_cmd",
        vagueness=1,
        prompt_pool=[
            "send 'info status' to the {name} VM monitor",
            "run monitor command 'info kvm' on {name}",
            "query QEMU monitor on {name} with info status",
        ],
        expect_tools=["send_monitor_cmd"],
        expect_gate_blocked=False,
    ),
    AITest(
        id="ai_gate_vague_no_vm_name",
        tags=["gate","create_vm","vague"],
        description="Vague create prompt without VM name — gate expected to block or AI asks",
        vagueness=5,
        prompt_pool=[
            "create a VM",
            "make me a linux vm",
            "I need a new virtual machine",
        ],
        expect_tools=["create_vm"],
        expect_gate_blocked=True,
    ),
]


# ─────────────────────────────────────────────
#  DYNAMIC TEST GENERATORS
# ─────────────────────────────────────────────

def _generate_random_ai_tests(n: int = 5, seed: Optional[int] = None) -> List[AITest]:
    """Generate N random AI tests covering all available tools."""
    rng   = random.Random(seed)
    tools = list(_TOOL_PROMPT_POOLS.keys())
    tests: List[AITest] = []

    for i in range(n):
        tool = tools[i % len(tools)] if i < len(tools) else rng.choice(tools)

        vm   = rng.choice(_RAND_VM_NAMES)
        snap = rng.choice(_RAND_SNAP_NAMES)
        ram  = rng.choice(_RAND_RAM)
        os   = rng.choice(_RAND_OS)

        pool   = _TOOL_PROMPT_POOLS[tool]
        prompt = rng.choice(pool)
        prompt = (prompt
                  .replace("{vm}",   vm)
                  .replace("{snap}", snap)
                  .replace("{ram}",  ram)
                  .replace("{os}",   os))

        words     = len(prompt.split())
        vagueness = 1 if words <= 2 else 2 if words <= 5 else 3

        tests.append(AITest(
            id=f"ai_rand_tool_{tool}_{i:02d}",
            tags=["random","ai","tool",tool.replace("_","-")],
            description=f"Random {tool} test #{i} — prompt varies by seed",
            vagueness=vagueness,
            prompt_pool=[prompt],
            expect_tools=[tool],
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


def _generate_ai_tests_from_profiles(n: int = 5, seed: Optional[int] = None) -> List[AITest]:
    """Legacy wrapper — delegates to _generate_random_ai_tests."""
    return _generate_random_ai_tests(n, seed)


def _cleanup_random_ai_profiles(tests: List[AITest]) -> None:
    """No-op — random AI tests no longer create custom profiles."""
    pass


# ─────────────────────────────────────────────
#  LAYER 3 RUNNER
# ─────────────────────────────────────────────

def call_ollama(messages: List[Dict], model: str = None) -> Tuple[List[Dict], str]:
    payload = {
        "model":    model or OLLAMA_MODEL,
        "messages": messages,
        "tools":    TOOLS,
        "stream":   False,
        "options":  {"temperature": 0.1, "num_ctx": 8192},
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
        all_calls:   List[Dict] = []
        san_list:    List[Dict] = []
        gate_checks: List[Dict] = []

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

                # Run gate_check on raw (pre-sanitiser) args so we can record
                # whether the context gate would have intercepted this call.
                gate_result = gate_check(tn, dict(raw))
                gate_checks.append({"tool": tn, "blocked": gate_result is not None,
                                     "result": gate_result})

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
        detail["gate_checks"]  = gate_checks

        any_gate_blocked = any(g["blocked"] for g in gate_checks)
        if tc.expect_gate_blocked is True and not any_gate_blocked:
            issues.append(
                "Expected gate to block at least one tool call "
                f"(all calls passed gate): {[g['tool'] for g in gate_checks]}"
            )
        elif tc.expect_gate_blocked is False and any_gate_blocked:
            blocked = [g for g in gate_checks if g["blocked"]]
            for b in blocked:
                missing = [m["field"] for m in (b["result"] or {}).get("missing", [])]
                issues.append(
                    f"Gate unexpectedly blocked [{b['tool']}]: missing fields {missing}"
                )

        for expected in tc.expect_tools:
            if expected not in called_tools:
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

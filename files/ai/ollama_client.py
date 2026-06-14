"""
ollama_client.py — Ollama AI Client Layer

HTTP communication with Ollama: system prompt construction and the
blocking chat API call. OLLAMA_URL and OLLAMA_MODEL are the two
tuneable globals for this layer.
"""

import json
import os
import sys
from typing import Dict, List

import requests

from api.qemu_config  import OVMF, list_profiles
from .tools        import TOOLS
from .display      import console
import preflight.validator as _validator

_CFG = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_OLLAMA = _CFG["ollama"]

OLLAMA_URL   = os.environ.get("OLLAMA_URL",   _OLLAMA["url"])
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", _OLLAMA["model"])


# Assembles the LLM system prompt from live OVMF/profile state and custom-mode flag.
# In: nothing → Out: str
def _build_system_prompt() -> str:
    profiles    = [p["name"] for p in list_profiles()]
    ovmf_status = "AVAILABLE" if OVMF["available"] else "NOT FOUND (SeaBIOS fallback active)"
    custom_note = (
        "\nCUSTOM MODE ACTIVE (-cu): product_name and manufacturer can be any fictional values. "
        "Skip all warnings about unverifiable hardware."
    ) if _validator._CUSTOM_MODE else ""

    return f"""You are an expert KVM/QEMU virtual machine assistant.
You manage virtual machines using QEMU/KVM. Respond concisely and use tools immediately.{custom_note}
You help the user create, launch, monitor, and manage QEMU/KVM virtual machines.

SYSTEM: OVMF={ovmf_status} | Profiles={profiles}

═══ CRITICAL: ACT vs ASK ═══
For clear requests, call the tool IMMEDIATELY. Do not ask for confirmation or missing optional info.
Examples of when to ACT without asking:
  "create a Windows 11 VM called win11" → call create_vm right now
  "create a VM called X with NAT"       → call create_vm right now
  "list my VMs"                         → call list_vms right now
  "launch X"                            → call launch_vm right now
Only use the clarify tool if the VM NAME is completely absent from the user's message.

═══ DEFAULTS (never ask for these) ═══
display=sdl | disk=60GB qcow2 | network=nat | kvm=true | cpu=host
Windows → uefi=true + bios=ovmf + machine_type=q35 (always)
Linux   → machine_type=q35
ARM/Pi  → kvm=false + qemu_binary=qemu-system-aarch64 + machine_type=virt

═══ RULES ═══
1. NAME: Only use a name the user explicitly said. Never invent "windows-vm", "linux-vm" etc.
   If name is missing, call clarify ONCE. If name is given, call create_vm immediately.

2. MACHINE TYPE: Only valid values: q35, pc, pc-i440fx, microvm, virt, raspi3b.
   Profile names (office_laptop, dell_g15_5520) go in the "profile" field, NOT machine_type.

3. CPU: x86_64 VMs: host/kvm64/Haswell/Skylake/IceLake/EPYC only. NEVER cortex-*/arm*.
   aarch64 VMs: cortex-a72/cortex-a53 etc.

4. ISO: call scan_isos FIRST when user mentions any ISO or OS to install.
   Use exact path from scan_isos. NEVER construct /home/user/... or /path/to/... paths.
   ARM64 ISO filename (arm64/Arm64/aarch64) → auto-set machine_arch=aarch64.

5. MULTI-STEP: "create and launch" → call create_vm then launch_vm (two tool calls, no pause).

6. FAILURE: "why did it fail" or VM stopped → call get_vm_logs immediately.

7. DELETE: "delete/kill/remove VM" → call delete_vm with delete_disks=true.

8. BRIDGE: bridge_iface must be a bridge (virbr0, br0). Never use eth0/ens33/wlan0.

9. RESPONSES: 1-2 sentences max. UI already shows tables. Say "Done — X is running."

10. PROFILES: Match real device names to profiles (Dell G15 → dell_g15_5520).
    Raspi3b → serial console only, no display, kvm=false.
    Always check_profile_compatibility for ARM/raspi before creating.
"""


# POSTs the full chat payload (with tools) to the Ollama API and returns the parsed JSON response.
# In: List[dict] messages → Out: dict response
def _call_ollama(messages: List[Dict]) -> Dict:
    payload = {
        "model":    OLLAMA_MODEL,
        "messages": [{"role": "system", "content": _build_system_prompt()}] + messages,
        "tools":    TOOLS,
        "stream":   False,
        "options":  {"temperature": _OLLAMA["temperature"], "num_ctx": _OLLAMA["num_ctx"]},
    }
    try:
        resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=_OLLAMA["timeout"])
        resp.raise_for_status()
        return resp.json()
    except requests.ConnectionError:
        console.print(
            f"[error]Cannot connect to Ollama at {OLLAMA_URL}[/error]\n"
            f"  → Start: [bold]ollama serve[/bold]\n"
            f"  → Pull:  [bold]ollama pull {OLLAMA_MODEL}[/bold]"
        )
        sys.exit(1)

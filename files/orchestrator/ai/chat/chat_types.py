"""
chat_types.py — Per-turn base types and pure transforms.

The gate-independent foundation of the chat turn pipeline: the TurnState /
GateOutcome types, the critical-tool check, the create_vm spec-preview builder,
and the pure arg transforms (custom-mode detection, os_type resolution,
pre-gate clarify build). chat_turn.py (the interactive gates + dispatch) and
cli.py import from here; this module references none of them, so the edge is
one-directional and there is no cycle.
"""

import json
import os
from typing import List, Optional
from dataclasses import dataclass, field
from enum import Enum, auto

from shared.display import console
from orchestrator.sanitizer.sanitizer import OS_TYPE_ALIASES
from orchestrator.sanitizer.context_gate import _REQUIRED as _GATE_REQUIRED
from orchestrator.executor_client import get_all_profiles
from orchestrator.preflight.validator import set_custom_mode
from .context_assistant import extract_slots
from ..agent.contract import is_critical as contract_is_critical

_MC = {"os_type": "linux", "cpu_cores": 2, "memory_mb": 2048, "machine_type": "q35", "uefi": False}
try:
    from executor.tool_dispatch.tool_executor import _VM_DEFS
except ImportError:
    _VM_DEFS = {"disk_size_gb": 60, "network_mode": "nat", "disk_bus": "virtio"}

_CFG = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_OS_KEYWORDS = set(_CFG["os_keywords_gate"])
_RECENT_CONTEXT_WINDOW = _CFG["chat"].get("recent_context_window", 6)


def _is_critical(tool_name: str, args: dict) -> bool:
    """Return True when the operation requires double confirmation.

    Thin wrapper kept for callers (chat_turn, http_chat, cli); the decision now
    lives in the Doorman contract — ``double`` tier == critical. See
    ``contract.is_critical``.

    Example::

        _is_critical("delete_vm",  {"name": "myvm"})  # → True
        _is_critical("launch_vm",  {"name": "myvm"})  # → False
        _is_critical("stop_vm",    {"name": "myvm"})  # → False
    """
    return contract_is_critical(tool_name, args)


# Builds (label, value) rows describing the specs create_vm is about to use,
# falling back to the same defaults the executor applies so the preview
# matches what will actually be created.
# In: dict args → Out: List[tuple[str, str]]
def _build_vm_spec_rows(args: dict) -> list:
    """Build the (label, value) rows previewing the specs create_vm will use."""
    name    = args.get("name") or "?"
    os_type = args.get("os_type") or _MC["os_type"]
    os_name = args.get("os_name") or ""

    # Suppress profile when user explicitly set SMBIOS fingerprinting fields —
    # mirrors the same logic in tool_executor so the preview matches what gets created.
    _manual_smbios = any(args.get(f) for f in ("serial_number", "bios_vendor", "chassis_type", "smbios_type"))
    profile = "" if _manual_smbios else (args.get("profile") or "")
    if not profile and not _manual_smbios:
        product = (args.get("product_name", "") + " " + args.get("manufacturer", "")).lower()
        for pname, pdata in get_all_profiles().items():
            pp = (pdata.get("product_name", "") + " " + pdata.get("manufacturer", "")).lower()
            if any(kw in pp for kw in product.split() if len(kw) > 3):
                profile = pname
                break

    _pdata = get_all_profiles().get(profile, {}) if profile else {}

    cpu_cores    = args.get("cpu_cores")    or _pdata.get("cpu_cores")    or _MC["cpu_cores"]
    memory_mb    = int(args.get("memory_mb") or _pdata.get("memory_mb")    or _MC["memory_mb"])
    machine_type = args.get("machine_type") or _pdata.get("machine_type") or _MC["machine_type"]
    if args.get("hardened"):
        machine_type = "q35"
    disk_gb      = args.get("disk_size_gb") or _VM_DEFS["disk_size_gb"]
    _DISK_BUS_VALUES = {"sata", "nvme", "scsi", "ide", "virtio"}
    _raw_fmt     = args.get("disk_format") or ""
    disk_bus_preview = (
        args.get("disk_bus")
        or (_raw_fmt if _raw_fmt.lower() in _DISK_BUS_VALUES else "")
        or _VM_DEFS.get("disk_bus", "virtio")
    )
    disk_fmt     = disk_bus_preview
    net_mode     = args.get("network_mode") or _VM_DEFS["network_mode"]
    iso_path     = args.get("iso_path") or ""

    is_windows = "windows" in str(os_type).lower() or "windows" in str(os_name).lower()
    uefi       = True if is_windows else bool(args.get("uefi", _pdata.get("uefi", _MC["uefi"])))
    if is_windows:
        machine_type = "q35"

    rows = [
        ("Name", name),
        ("OS",   f"{os_type}" + (f" ({os_name})" if os_name else "")),
    ]
    if profile:
        rows.append(("Profile", f"{profile}" + (f"  ({_pdata.get('description')})" if _pdata.get("description") else "")))
    rows += [
        ("CPU Cores", str(cpu_cores)),
        ("Memory",    f"{memory_mb // 1024} GB" if memory_mb >= 1024 else f"{memory_mb} MB"),
        ("Disk",      f"{disk_gb} GB ({disk_fmt})"),
        ("Network",   net_mode),
        ("Machine",   f"{machine_type}  {'UEFI' if uefi else 'BIOS'}"),
        ("ISO",       iso_path if iso_path else "[dim]auto-detect / none[/dim]"),
    ]
    return rows


# Runs the interactive Ollama chat REPL: reads input, drives the agentic tool loop (up to 15 rounds), handles clarifications, and saves session.
# In: bool verbose → Out: nothing (blocks until exit)
@dataclass
class TurnState:
    """Mutable per-user-turn state for the chat REPL.

    Bundles the flags and sets the tool-processing stages (context assistant,
    pre-flight, safety gate, clarify) read and write, so they pass as one object
    instead of a dozen loose locals. Constructed once per user message;
    reset_iteration() clears the per-agentic-round flags.

    Example::

        st = TurnState(user_wants_action=True)
        st.confirmed_tool_types.add("create_vm")
        st.reset_iteration()          # clears clarify_* / op_cancelled
    """
    user_wants_action:       bool = False
    tools_called:            bool = False   # a tool_call was seen this turn
    tool_executed:           bool = False   # execute_tool actually ran this turn
    last_had_tools:          bool = False   # last Ollama response had tool_calls
    context_assistant_fired: bool = False
    clarified_fields:        set  = field(default_factory=set)   # fields answered via clarify
    clarified_values:        set  = field(default_factory=set)   # (field, value) answered
    confirmed_values:        set  = field(default_factory=set)   # (field, value) safety-confirmed
    confirmed_tool_types:    set  = field(default_factory=set)   # tool types batch-confirmed
    # reset every agentic round:
    op_cancelled:            bool = False
    clarify_happened:        bool = False
    clarify_answer:          str  = ""
    clarify_field:           str  = ""

    def reset_iteration(self) -> None:
        """Clear the flags that live for a single agentic round.

        Example::

            st = TurnState(); st.op_cancelled = True
            st.reset_iteration()      # st.op_cancelled is now False again
        """
        self.op_cancelled     = False
        self.clarify_happened = False
        self.clarify_answer   = ""
        self.clarify_field    = ""


class GateOutcome(Enum):
    """What a per-tool stage tells the chat tool-loop to do next.

    Replaces the scattered break/continue/return in chat_loop's tool loop with an
    explicit signal each stage returns, so stages can be extracted into functions
    (which can't break/continue/return the caller's loops).
    """
    PROCEED   = auto()   # fall through to the next stage
    SKIP_TOOL = auto()   # stop this tool call, keep processing the loop
    REPLAN    = auto()   # re-prompt the AI (an _INTERNAL_ nudge was appended)
    CANCELLED = auto()   # user declined; state.op_cancelled set — drop to the REPL
    EXIT      = auto()   # user hit Ctrl-C / EOF mid-prompt — leave chat entirely


def _maybe_enable_custom_mode(tool_name: str, user_input_lower: str,
                              messages: List[dict]) -> None:
    """Enable custom mode for create_profile when the user said 'custom'.

    Custom mode disables the product-name verification HTTP check. Looks at the
    current message plus the last few user messages for the word 'custom'.

    Example::

        _maybe_enable_custom_mode("create_profile", "custom dell box", msgs)
        # → calls set_custom_mode(True)
    """
    if tool_name != "create_profile":
        return
    ctx = user_input_lower + " " + " ".join(
        m.get("content", "").lower() for m in messages[-_RECENT_CONTEXT_WINDOW:] if m.get("role") == "user"
    )
    if "custom" in ctx:
        set_custom_mode(True)
        console.print("[dim]Custom mode active — product verification disabled[/dim]")


def _resolve_os_type(tool_name: str, raw_args: dict, user_input_lower: str,
                     state: "TurnState") -> dict:
    """Pin create_vm's os_type from an OS word the user actually typed, or strip
    an AI-inferred one so the gate can ask.

    When the user named an OS ("mint"), resolve it to the canonical type and mark
    it clarified; when they didn't, drop any os_type the AI guessed. Returns the
    (possibly new) raw_args.

    Example::

        _resolve_os_type("create_vm", {"name": "v"}, "make a mint vm", state)
        # → {"name": "v", "os_type": "linux"}   (and state.clarified_fields += os_type)
    """
    if tool_name != "create_vm" or "os_type" in state.clarified_fields:
        return raw_args
    ui_tokens = {t.strip('.,!?;:') for t in user_input_lower.split()}
    matched = next(iter(_OS_KEYWORDS & ui_tokens), None)
    if matched:
        canonical = OS_TYPE_ALIASES.get(matched, matched)
        raw_args = dict(raw_args)
        raw_args["os_type"] = canonical
        state.clarified_fields.add("os_type")
        state.clarified_values.add(("os_type", canonical))
    elif "os_type" in raw_args:
        raw_args.pop("os_type")
    return raw_args


def _build_pre_gate_result(tool_name: str, raw_args: dict, user_input: str,
                           recent_context: str, state: "TurnState") -> Optional[dict]:
    """Return a clarify result if a gate-required field is missing from what the
    user actually said, else None.

    Checks required trackable fields against extract_slots(user_input) — not the
    AI's args — so hallucinated values can't bypass the gate. A value the AI put
    in args is accepted if it's grounded in recent conversation.

    Example::

        _build_pre_gate_result("create_vm", {"name": "v"}, "make a vm", "", state)
        # → {"success": False, "clarify": True, "needs_clarification": "os_type", ...}
    """
    gate_required = _GATE_REQUIRED.get(tool_name, [])
    if not gate_required or tool_name == "clarify":
        return None
    user_slots = extract_slots(user_input)
    for clf in state.clarified_fields:
        if clf in raw_args and raw_args[clf]:
            user_slots[clf] = raw_args[clf]
    missing = [
        {"field": f, "question": q, "options": opts}
        for f, q, opts in gate_required
        if f in user_slots and user_slots[f] is None
        and not (
            raw_args.get(f)
            and isinstance(raw_args.get(f), str)
            and (
                raw_args[f].lower() in recent_context
                or raw_args[f].lower().replace(" ", "") in recent_context.replace(" ", "")
            )
        )
    ]
    if not missing:
        return None
    return {
        "success":             False,
        "clarify":             True,
        "missing":             missing,
        "question":            missing[0]["question"],
        "options":             missing[0]["options"],
        "needs_clarification": missing[0]["field"],
        "error":               (
            f"Missing required arguments for {tool_name}: "
            f"{[m['field'] for m in missing]}"
        ),
    }


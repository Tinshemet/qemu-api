"""
executioner/context.py — shared foundation for the executor tool layer.

Owns the QemuManager singleton, the parsed config constants, the revert-tracking
state, and the executor/display imports every tool handler needs. The tool
handlers (and create_vm) import what they need from here, so no orchestrator
imports leak in and there's one place that holds "the executor's tool runtime".

Names keep their original spellings (`manager`, `_VM_DEFS`, `_set_revert`, …) so
the extracted code and the external importers (orchestrator.pipeline,
executor.server, the tests) read unchanged.
"""

from typing import Any, Dict

from shared.executioner import config as _config

# ── config constants (from executioner/config) ─────────────────────────────────
_VM_BASE             = _config.VM_BASE
_VM_DEFS             = _config.VM_DEFS
_TOOL_DEFS           = _config.TOOL_DEFS
_VALID_MACHINE_TYPES = _config.VALID_MACHINE_TYPES
_ARM_CPU_PREFIXES    = _config.ARM_CPU_PREFIXES
_GENERIC_OS_NAMES    = _config.GENERIC_OS_NAMES
_ISO_ARM_KEYWORDS    = _config.ISO_ARM_KEYWORDS
_ISO_X86_KEYWORDS    = _config.ISO_X86_KEYWORDS

# Reserved snapshot-tag prefix for checkpoint savepoints. rollback discovers a
# checkpoint's member VMs by this tag, so no separate manifest has to be persisted.
_CKPT_TAG_PREFIX = "ckpt__"

# Corporate suffixes / filler that must never be treated as a product-match token
# (otherwise every "…Inc." request collides with every "…Inc." profile).
_IDENTITY_STOPWORDS = {
    "inc", "inc.", "corp", "corp.", "corporation", "ltd", "ltd.", "llc",
    "co", "co.", "company", "the", "international", "gmbh", "technologies",
}

# ── executor / display imports ─────────────────────────────────────────────────
from executor.api.qemu_config import (
    MachineConfig, DiskConfig, NetworkConfig,
    OVMF, apply_profile, check_profile_compatibility,
    check_system_capabilities, delete_custom_profile,
    get_all_profiles, list_profiles, save_custom_profile,
)
from executor.api.qemu_manager import QemuManager
from executor.api.label_registry import register_label
from executor.fingerprint import tf_report

# This module has no orchestrator imports — it runs on executor-only machines.
from shared.display import (
    console,
    render_compat, render_fleet, render_monitor, render_profiles, render_templates,
    render_snapshots, render_status, render_system,
    render_vm_failure, render_vm_list,
)
from rich.panel import Panel

# Tools that manage _last_revert_action themselves (set it on success, or
# explicitly clear it) — excluded from the blanket clear in the dispatcher so a
# failed attempt doesn't wipe out a still-valid revert from an earlier success.
# Derived from the canonical tool registry (single source of truth).
from executor.command_catalog import REVERT_TOOLS as _REVERT_AWARE_TOOLS

manager = QemuManager()

# ── revert tracking ────────────────────────────────────────────────────────────
# The inverse action for the last reversible tool call. Empty means nothing to
# revert. Mutated IN-PLACE (never rebound) so every module that imported the name
# — the dispatcher's `revert` handler, create_vm, the tests — sees the same dict.
_last_revert_action: Dict[str, Any] = {}


def _set_revert(tool: str, args: dict, description: str) -> None:
    """Record an inverse action so the next 'revert' call can undo the current tool."""
    _last_revert_action.clear()
    _last_revert_action.update({"tool": tool, "args": args, "description": description})


def _clear_revert() -> None:
    """Clear any pending revert action (called before irreversible operations)."""
    _last_revert_action.clear()

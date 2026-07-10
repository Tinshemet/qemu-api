"""
pipeline.py — Orchestrator execution pipeline.

Sanitise → gate → name resolution → dispatch. This is the full-pipeline
entry point used in local mode (single-machine) and by the AI/CLI layer
in all modes. The actual tool dispatch runs in
shared.executioner.tool_executor._run; this module owns only the
orchestrator-side concerns that precede it.
"""

from orchestrator.sanitizer.sanitizer import (
    PLACEHOLDER_VM_NAMES,
    _resolve_iso,
    _resolve_vm_name,
    _sanitise_args,
)
from orchestrator.sanitizer.context_gate import gate_check
from orchestrator.preflight.validator import _preflight_check, _show_preflight_warning
from shared.executioner.tool_executor import manager, _run


_NAME_SKIP = frozenset({"create_vm", "create_profile", "clone_vm", "create_network"})


def execute_tool(tool_name: str, args: dict, verbose: bool = False, skip_gate: bool = False):
    """Sanitise args, resolve VM names, then dispatch to the executor layer.

    Args:
        tool_name: Name of the tool to execute (e.g. ``"create_vm"``).
        args:      Raw argument dict from the AI or CLI caller.
        verbose:   When True, suppress Rich console output (caller handles display).
        skip_gate: Skip sanitize/gate/name-resolution (call came back from a
                   clarification loop — args are already clean).

    Returns:
        Tool result dict, or a ``{"clarify": True, ...}`` gate/preflight response.

    Example::

        execute_tool("list_vms", {})
        # → [{"name": "my-linux", "status": "stopped", ...}]
    """
    raw_os_type = args.get("os_type", "")  # capture before sanitizer may alias it

    if not skip_gate:
        args = _sanitise_args(tool_name, args)

        gate_result = gate_check(tool_name, args)
        if gate_result:
            return gate_result

        if "name" in args and tool_name not in _NAME_SKIP:
            vms      = manager.list_vms()
            resolved = _resolve_vm_name(vms, str(args["name"]))
            if resolved:
                args["name"] = resolved

    return _run(
        tool_name, args, verbose,
        raw_os_type=raw_os_type,
        placeholder_vm_names=PLACEHOLDER_VM_NAMES,
        resolve_iso=_resolve_iso,
        preflight_check=_preflight_check,
        show_preflight_warning=_show_preflight_warning,
    )

#!/usr/bin/env python3
"""
test_tool_registry.py — the drift guard for the tool regime's single source of truth.

Asserts the canonical TOOL_SPECS registry (executor/command_catalog.py) stays in
lockstep with tool_executor._run's dispatch, and that every consumer really DERIVES
from it (so no hand-maintained copy can silently drift back in). This is the test
that makes "add a tool in ONE place" enforceable.

Run:  PYTHONPATH=files python3 files/tests/test_tool_registry.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from executor.command_catalog import (
    TOOL_SPECS, KNOWN_TOOLS, VM_SCOPED_TOOLS, TOOL_EFFECTS, REVERT_TOOLS,
)

_PASS = 0
_FAIL = 0

def check(label, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  \033[32mok\033[0m   {label}")
    else:
        _FAIL += 1
        print(f"  \033[31mFAIL\033[0m {label}")


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # The dispatch table is the auto-registered tool handlers (one per file under
    # executor/tool_dispatch/tools/); its keys are the tools that actually dispatch.
    from executor.tool_dispatch.tools import _REGISTRY as _TOOL_REGISTRY
    dispatched = set(_TOOL_REGISTRY.keys())

    print("registry ↔ dispatch (the drift guard)")
    check("every dispatched tool is in the registry", not (dispatched - set(KNOWN_TOOLS)))
    check("every registry tool is actually dispatched", not (set(KNOWN_TOOLS) - dispatched))
    check("51 tools", len(KNOWN_TOOLS) == 51)

    print("\nconsumers DERIVE from the registry (same object, not a copy)")
    import executor.server as srv
    import orchestrator.executor_client as ec
    import executor.tool_dispatch.tool_executor as te
    import orchestrator.ai.active_library as al
    check("server _KNOWN_TOOLS is the registry set", srv._KNOWN_TOOLS is KNOWN_TOOLS)
    check("executor_client _VM_TOOLS is the registry set", ec._VM_TOOLS is VM_SCOPED_TOOLS)
    check("tool_executor _REVERT_AWARE_TOOLS is the registry set", te._REVERT_AWARE_TOOLS is REVERT_TOOLS)
    check("active_library _TOOL_EFFECTS is the registry map", al._TOOL_EFFECTS is TOOL_EFFECTS)

    print("\nregression: previously-drifted tools now covered")
    for t in ("fleet", "run_guest_command", "guest_ping", "add_label", "list_labels", "mark_as_template"):
        check(f"{t} in _KNOWN_TOOLS (was failing over HTTP)", t in KNOWN_TOOLS)
    check("fleet has an effect (digest no longer goes stale)", "fleet" in TOOL_EFFECTS)
    check("no stale snapshot names in the VM-scoped set (were bypassing allowlist)",
          not ({"create_snapshot", "restore_snapshot", "delete_snapshot", "list_snapshots"} & set(VM_SCOPED_TOOLS)))
    check("real snapshot names ARE vm-scoped",
          {"snapshot_create", "snapshot_restore", "snapshot_delete"} <= set(VM_SCOPED_TOOLS))

    print("\nrequired-fields single-source (registry is the authority)")
    import json
    from executor.command_catalog import REQUIRED_FIELDS
    tj = {f["function"]["name"]: set(f["function"]["parameters"].get("required", []))
          for f in json.load(open(os.path.join(root, "orchestrator/ai/tools.json")))
          if f["function"]["parameters"].get("required")}
    mism = {t for t in set(tj) | set(REQUIRED_FIELDS)
            if tj.get(t, set()) != set(REQUIRED_FIELDS.get(t, []))}
    check("tools.json required == registry (no drift)", not mism)
    check("create_vm requires name+os_type in the registry", set(REQUIRED_FIELDS.get("create_vm", [])) == {"name", "os_type"})
    # NOTE: the context-assistant's grounding-required list is intentionally a
    # DIFFERENT concept (must-be-literally-present, excludes os_type's default),
    # so it is NOT derived from / asserted against the registry.

    print("\nspec shape")
    well_formed = all(
        set(s) == {"req", "vm", "effect", "rev"}
        and isinstance(s["req"], list) and isinstance(s["vm"], bool) and isinstance(s["rev"], bool)
        and (s["effect"] is None or isinstance(s["effect"], tuple))
        for s in TOOL_SPECS.values())
    check("all specs well-formed", well_formed)

    print(f"\n{'='*48}\n  {_PASS}/{_PASS + _FAIL} passed\n{'='*48}")
    sys.exit(1 if _FAIL else 0)


if __name__ == "__main__":
    main()

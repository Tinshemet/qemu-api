"""local_probe tool — read-only host predicate, workspace-scoped.

The host-side twin of guest_probe: independently verifies an effect of run_command (a file
exists, a dir exists, a file contains text, …) so the ledger books on REALITY, not on the
command's exit code. Path assertions are confined to the workspace (~/.gorgon/workspace) —
a target that escapes it is `success:False` (unverifiable), matching the write-confinement
run_command enforces. `command_available` checks PATH (not a path). Read-only; never mutates.
See docs/design/general-command-primitive.md.
"""
import os
import re
import shutil

from executor.tool_dispatch.tools.base import Tool
from executor.api._vm_constants import WORKSPACE_DIR

_PATH_ASSERTIONS = ("file_exists", "dir_exists", "file_contains", "file_matches", "is_writable")


def _resolve_in_workspace(target: str):
    """Resolve *target* under the workspace; return its real path, or None if it escapes
    (absolute paths outside the workspace, or `..`/symlink traversal out of it)."""
    p  = target if os.path.isabs(target) else os.path.join(WORKSPACE_DIR, target)
    rp = os.path.realpath(p)
    ws = os.path.realpath(WORKSPACE_DIR)
    return rp if (rp == ws or rp.startswith(ws + os.sep)) else None


def _read(path: str) -> str:
    with open(path, "r", errors="replace") as f:
        return f.read()


class LocalProbeTool(Tool):
    names = ("local_probe",)

    def run(self, args, ctx):
        assertion = str(args.get("assertion", "")).strip()
        target    = str(args.get("target", "")).strip()
        value     = args.get("value")
        if not assertion or not target:
            return {"success": False, "error": "local_probe requires 'assertion' and 'target'."}

        # command_available takes a command NAME, not a path — no workspace scoping.
        if assertion == "command_available":
            return {"success": True, "assertion": assertion, "target": target,
                    "holds": shutil.which(target) is not None}

        if assertion not in _PATH_ASSERTIONS:
            return {"success": False, "error": f"Unknown assertion {assertion!r}."}
        if assertion in ("file_contains", "file_matches") and not value:
            return {"success": False, "error": f"{assertion} requires a 'value'."}

        path = _resolve_in_workspace(target)
        if path is None:
            return {"success": False,
                    "error": f"Target {target!r} is outside the workspace — not readable."}

        try:
            if assertion == "file_exists":
                holds = os.path.isfile(path)
            elif assertion == "dir_exists":
                holds = os.path.isdir(path)
            elif assertion == "is_writable":
                holds = os.access(path, os.W_OK)
            elif assertion == "file_contains":
                holds = os.path.isfile(path) and value in _read(path)
            else:  # file_matches
                holds = os.path.isfile(path) and re.search(value, _read(path)) is not None
        except Exception as e:
            return {"success": False, "error": str(e)}

        return {"success": True, "assertion": assertion, "target": target, "holds": bool(holds)}

"""run_command_grants.py — operator-controlled grants that widen run_command's sandbox.

run_command is bubblewrap-confined by default: it writes only in the workspace, has no network,
and can't read the auth secrets. The OPERATOR (never the AI) can widen that for the current
server session — grant a read path ("read my Desktop CSV") or the network — via a chat command
(see chat_endpoint). Grants are:
  • process-global (single-operator model) and in-memory (a server restart clears them);
  • injected into run_command at dispatch by executor_client.execute_tool, so the model can
    never set them itself — anything the AI puts in the args is stripped first;
  • refused for the gorgon home (secrets stay unreadable even via a grant).
See docs/design/general-command-primitive.md §4.2.
"""
import os

from executor.api._vm_constants import VM_BASE_DIR

_read_paths: set = set()   # realpath'd absolute paths the operator allows reading
_net: bool = False


def _would_expose_secrets(rp: str) -> bool:
    """True if granting read of *rp* would expose the gorgon home: rp is the home, is inside it
    (operators.json, keys, state…), OR is an ancestor of it (transitively exposes it)."""
    home = os.path.realpath(VM_BASE_DIR)
    return rp == home or rp.startswith(home + os.sep) or home.startswith(rp + os.sep)


def add_read(path: str) -> dict:
    """Operator grants read access to *path* for this session. Refuses missing paths and any
    path that would expose the gorgon home."""
    rp = os.path.realpath(os.path.expanduser(path))
    if not os.path.exists(rp):
        return {"success": False, "error": f"Path not found: {path}"}
    if _would_expose_secrets(rp):
        return {"success": False,
                "error": "Refusing to grant read of the gorgon home (would expose secrets)."}
    _read_paths.add(rp)
    return {"success": True, "path": rp}


def set_net(on: bool) -> dict:
    """Operator toggles network access for run_command this session."""
    global _net
    _net = bool(on)
    return {"success": True, "net": _net}


def clear() -> None:
    """Revoke all grants (read paths + network)."""
    global _net
    _read_paths.clear()
    _net = False


def snapshot() -> dict:
    """The current grants, injected into run_command at dispatch."""
    return {"read_paths": sorted(_read_paths), "net": _net}


def describe() -> str:
    """A human-readable summary for the operator."""
    if not _read_paths and not _net:
        return "No run_command grants active — workspace-only, no network."
    parts = []
    if _read_paths:
        parts.append("read: " + ", ".join(sorted(_read_paths)))
    parts.append("network: " + ("ON" if _net else "off"))
    return "run_command grants — " + " · ".join(parts)

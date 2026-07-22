"""run_command tool — general host operations, bubblewrap-confined to the workspace.

The host sibling of run_guest_command (which runs *inside* a VM). Runs an ordinary shell or
python command AS the operator, but jailed by bubblewrap so:
  - writes land ONLY in the workspace (~/.gorgon/workspace) — writing anywhere else fails at
    the syscall level;
  - the auth secrets under ~/.gorgon (operators.json, keys, toolstats) are NOT mounted, so a
    confined command can't read them at all;
  - the network is off (no --share-net).

If bubblewrap is missing the tool refuses to run rather than execute unconfined — a false
guarantee is worse than an unavailable one. See docs/design/general-command-primitive.md.

Success here means "the command ran", NOT "the goal is achieved" — achievement is decided by a
local_probe post-condition (like run_guest_command + guest_probe), never by the exit code alone.
"""
import json
import os
import shutil
import subprocess

from executor.tool_dispatch.tools.base import Tool
from executor.api._vm_constants import WORKSPACE_DIR

_CFG = json.load(open(os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config.json")))

# WORKSPACE_DIR (the only writable path in the jail) is the SSOT in _vm_constants.
_TIMEOUT_S    = _CFG.get("run_command_timeout_s", 60)
_MAX_OUTPUT   = _CFG.get("run_command_max_output_bytes", 1_048_576)

_INTERP = {
    "shell":  ["/bin/sh", "-c"],
    "python": ["python3", "-c"],
}

# Minimal read-only system view so ordinary tools/python run inside the jail. The gorgon
# secrets are NOT here (they live under ~/.gorgon, which is never bound), so read access to
# these is harmless.
_RO_SYSTEM = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc", "/opt")


def _bwrap_argv(inner: list) -> list:
    """Wrap the interpreter argv in a bubblewrap jail: workspace read-write, a minimal
    read-only system view, no network, everything else absent."""
    argv = [
        "bwrap",
        "--unshare-net",                       # no network by default
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
        "--new-session",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--bind", WORKSPACE_DIR, WORKSPACE_DIR,  # the ONLY writable path
        "--chdir", WORKSPACE_DIR,
        "--setenv", "HOME", WORKSPACE_DIR,       # ~ resolves into the workspace
    ]
    for p in _RO_SYSTEM:
        if os.path.exists(p):
            argv += ["--ro-bind", p, p]
    return argv + ["--"] + inner


class RunCommandTool(Tool):
    names = ("run_command",)

    def run(self, args, ctx):
        code = args.get("code", "")
        lang = args.get("lang", "shell")
        if not code:
            return {"success": False, "error": "run_command requires 'code'."}
        interp = _INTERP.get(lang)
        if interp is None:
            return {"success": False,
                    "error": f"Unknown lang {lang!r} — use 'shell' or 'python'."}
        if not shutil.which("bwrap"):
            return {"success": False,
                    "error": ("run_command is unavailable: bubblewrap (bwrap) is not installed "
                              "and this tool will not run commands unconfined. "
                              "Install it with: sudo apt install bubblewrap")}

        os.makedirs(WORKSPACE_DIR, exist_ok=True)
        argv = _bwrap_argv(interp + [code])
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return {"success": False, "workspace": WORKSPACE_DIR,
                    "error": f"Command timed out after {_TIMEOUT_S}s."}

        return {
            "success":    proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout":     proc.stdout[:_MAX_OUTPUT],
            "stderr":     proc.stderr[:_MAX_OUTPUT],
            "workspace":  WORKSPACE_DIR,
        }

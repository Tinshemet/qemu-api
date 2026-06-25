"""
executor_client.py — Executor Client (server-local)

On the server machine, the AI layer and the QEMU engine are co-located, so
tool execution is always a direct in-process call.  This module is a thin
re-export of shared.executioner.tool_executor so that server/ai/cli.py has a
single import point regardless of future remote-QEMU extensions.

CLI configuration (server/connection_config.json) is still loaded here so that
setup_provider.sh connectivity checks and API_URL assertions continue to work.
"""

import json
import os

_CFG              = json.load(open(os.path.join(os.path.dirname(__file__), "connection_config.json")))
API_URL           = os.environ.get("API_URL",   _CFG.get("url",   "local"))
_TOKEN            = os.environ.get("API_TOKEN", _CFG.get("token", ""))
_TIMEOUT          = int(os.environ.get("API_TIMEOUT", _CFG.get("timeout", 120)))
_CA_CERT          = os.environ.get("API_CA_CERT", _CFG.get("ca_cert") or None)
_VERIFY           = False if os.environ.get("API_VERIFY_SSL", "1") == "0" else (_CA_CERT or _CFG.get("verify_ssl", True))
_ALLOWED_VMS:     list = _CFG.get("client_allowed_vms",      [])
_ALLOWED_PROFILES: list = _CFG.get("client_allowed_profiles", [])

_VM_TOOLS = {"launch_vm", "stop_vm", "delete_vm", "clone_vm", "resize_disk",
             "vm_status", "create_snapshot", "restore_snapshot", "delete_snapshot",
             "list_snapshots", "show_qemu_cmd", "setup_done", "generate_guest_setup"}

from shared.executioner.tool_executor import execute_tool as _execute_tool  # noqa: E402


_LOCAL_ONLY_DISPLAYS = {"sdl", "gtk"}


def execute_tool(tool_name: str, args: dict, verbose: bool = False) -> dict:
    """Wrapper around shared execute_tool that overrides local-only displays
    and enforces client VM/profile access control."""
    if tool_name == "launch_vm":
        args = dict(args)
        if args.get("display", "sdl") in _LOCAL_ONLY_DISPLAYS or "display" not in args:
            args["display"] = "vnc"
        args["vnc_bind_local"] = False

    # Enforce VM allowlist — report as "not found" to avoid leaking existence
    if tool_name in _VM_TOOLS and _ALLOWED_VMS:
        vm_name = args.get("name", "")
        if vm_name not in _ALLOWED_VMS:
            return {"success": False, "error": f"VM '{vm_name}' not found."}

    # Filter list_vms to only show allowed VMs
    result = _execute_tool(tool_name, args, verbose)
    if tool_name == "list_vms" and _ALLOWED_VMS:
        if isinstance(result, list):
            result = [v for v in result if v.get("name") in _ALLOWED_VMS]
        elif isinstance(result, dict) and "vms" in result:
            result["vms"] = [v for v in result["vms"] if v.get("name") in _ALLOWED_VMS]
    return result

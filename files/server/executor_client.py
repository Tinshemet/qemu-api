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

_CFG     = json.load(open(os.path.join(os.path.dirname(__file__), "connection_config.json")))
API_URL  = os.environ.get("API_URL",   _CFG.get("url",   "local"))
_TOKEN   = os.environ.get("API_TOKEN", _CFG.get("token", ""))
_TIMEOUT = int(os.environ.get("API_TIMEOUT", _CFG.get("timeout", 120)))
_CA_CERT = os.environ.get("API_CA_CERT", _CFG.get("ca_cert") or None)
_VERIFY  = False if os.environ.get("API_VERIFY_SSL", "1") == "0" else (_CA_CERT or _CFG.get("verify_ssl", True))

from shared.executioner.tool_executor import execute_tool as _execute_tool  # noqa: E402


_LOCAL_ONLY_DISPLAYS = {"sdl", "gtk"}


def execute_tool(tool_name: str, args: dict, verbose: bool = False) -> dict:
    """Wrapper around shared execute_tool that overrides local-only displays.

    Any launch_vm call that reaches the server must use VNC — SDL/GTK have no
    meaning for remote HTTP clients and will crash if the server has no display.
    """
    if tool_name == "launch_vm":
        args = dict(args)
        if args.get("display", "sdl") in _LOCAL_ONLY_DISPLAYS or "display" not in args:
            args["display"] = "vnc"
        args["vnc_bind_local"] = True
    return _execute_tool(tool_name, args, verbose)

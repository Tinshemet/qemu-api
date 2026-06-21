"""
executor_client.py — Executor Client

The single seam between the AI provider (cli.py / orchestration) and the
execution layer (tool_executor.py / QemuManager) on the client machine.

Stage 0 (local):  direct in-process call to execute_tool.
Stage 1 (remote): POST {tool_name, args, verbose} to API_URL/execute.

Set API_URL to switch to remote mode (AI provider → client):
    export API_URL=http://192.168.1.10:8080
    export API_TOKEN=<same secret as the client machine>

The AI layer imports execute_tool only from here — never from tool_executor directly.
Everything else in executioner/ belongs to the client machine side.
"""

import json
import os
import sys

import requests

_CFG     = json.load(open(os.path.join(os.path.dirname(__file__), "connection_config.json")))
API_URL   = os.environ.get("API_URL",      _CFG.get("url",     "local"))
_TOKEN    = os.environ.get("API_TOKEN",    _CFG.get("token",   ""))
_TIMEOUT  = int(os.environ.get("API_TIMEOUT", _CFG.get("timeout", 120)))
# TLS verification: set API_CA_CERT=/path/to/ca.pem for self-signed certs,
# or API_VERIFY_SSL=0 to disable (not recommended — only for development).
_CA_CERT  = os.environ.get("API_CA_CERT", _CFG.get("ca_cert") or None)
_VERIFY   = False if os.environ.get("API_VERIFY_SSL", "1") == "0" else (_CA_CERT or _CFG.get("verify_ssl", True))


def execute_tool(tool_name: str, args: dict, verbose: bool = False) -> dict:
    if API_URL == "local":
        from client.executioner.tool_executor import execute_tool as _local
        return _local(tool_name, args, verbose)

    headers = {"Authorization": f"Bearer {_TOKEN}"} if _TOKEN else {}
    payload = {"tool_name": tool_name, "args": args, "verbose": verbose}

    try:
        resp = requests.post(
            f"{API_URL}/execute",
            json=payload,
            headers=headers,
            timeout=_TIMEOUT,
            verify=_VERIFY,
        )
    except requests.ConnectionError:
        from shared.display import console
        console.print(
            f"[bold red]Cannot connect to client machine at {API_URL}[/bold red]\n"
            f"  → On the client machine run: [bold]qemu-api serve[/bold]\n"
            f"  → Check that API_URL is correct and the port is reachable"
        )
        sys.exit(1)

    if resp.status_code == 401:
        from shared.display import console
        console.print(
            f"[bold red]API server rejected the token (401)[/bold red]\n"
            f"  → Make sure API_TOKEN matches on both machines"
        )
        sys.exit(1)

    if not resp.ok:
        return {"success": False, "error": f"API server error {resp.status_code}: {resp.text}"}

    result = resp.json().get("result", {})

    # Attach SSH tunnel + vncviewer commands when the client ran a VNC launch.
    # The AI provider knows the host (from API_URL); the client knows the port + password.
    if isinstance(result, dict) and result.get("display") == "vnc" and result.get("vnc_port"):
        from urllib.parse import urlparse
        host      = urlparse(API_URL).hostname or API_URL
        port      = result["vnc_port"]
        password  = result.get("vnc_password")
        pw_note   = f"  password: {password}" if password else "  (no password — localhost tunnel provides auth)"
        result["ssh_tunnel_cmd"]  = f"ssh -L {port}:localhost:{port} <your-username>@{host}"
        result["vnc_connect_cmd"] = f"vncviewer localhost:{port}"
        result["vnc_note"]        = pw_note

    return result

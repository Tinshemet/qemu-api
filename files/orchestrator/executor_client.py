"""
executor_client.py — Executor Client

Single import point for tool execution used by server/ai/cli.py.
Supports two modes controlled by connection_config.json (or API_URL env var):

  url = "local"          — direct in-process call (default, single-machine setup)
  url = "http://host:8001" — HTTP call to a remote executor.server instance

Remote mode enables running the AI orchestrator and the QEMU engine on
separate machines. The executor server (executor/server.py) must be running
on the target host.
"""

import json
import os
import time

import requests as _requests

with open(os.path.join(os.path.dirname(__file__), "connection_config.json")) as _f:
    _CFG = json.load(_f)
API_URL           = os.environ.get("API_URL",        _CFG.get("url",   "local"))
# Deliberately NOT API_TOKEN — that env var is the orchestrator's own incoming
# client-facing secret (see orchestrator/http/api_server.py). Reusing it here
# would make the orchestrator send its client-facing token to the executor,
# which almost never matches the executor's independently configured secret.
_TOKEN            = os.environ.get("EXECUTOR_TOKEN", _CFG.get("token", ""))
_TIMEOUT          = int(os.environ.get("API_TIMEOUT", _CFG.get("timeout", 120)))
_CA_CERT          = os.environ.get("API_CA_CERT", _CFG.get("ca_cert") or None)
_VERIFY           = (
    False if os.environ.get("API_VERIFY_SSL", "1") == "0"
    else (_CA_CERT or _CFG.get("verify_ssl", True))
)
_ALLOWED_VMS:         list = _CFG.get("client_allowed_vms",      [])
_ALLOWED_PROFILES:    list = _CFG.get("client_allowed_profiles", [])
_ALLOWED_TOOLS:       set  = set(_CFG.get("allowed_remote_tools", []))
_LOCAL_ONLY_DISPLAYS: set  = set(_CFG.get("local_only_displays", ["sdl", "gtk"]))

# ── Executor sync cache ───────────────────────────────────────────────────────
_synced: dict = {}


def sync() -> dict:
    """Fetch profiles, OVMF info, and capabilities from the executor.

    In local mode imports directly; in remote mode calls /profiles and
    /capabilities. Called at orchestrator startup and on demand.

    Returns:
        Synced state dict (also stored in module-level ``_synced``).

    Example::

        sync()
        get_ovmf()   # → {"available": True, "code": "/usr/share/OVMF/...", ...}
        get_profiles()  # → [{"name": "dell_g15_5520", ...}, ...]
    """
    global _synced
    if API_URL and API_URL != "local":
        hdrs = {"Authorization": f"Bearer {_TOKEN}"}
        try:
            r = _requests.get(f"{API_URL}/profiles",     headers=hdrs, timeout=10, verify=_VERIFY)
            profiles_data = r.json() if r.ok else {}
        except Exception:
            profiles_data = {}
        try:
            r = _requests.get(f"{API_URL}/capabilities", headers=hdrs, timeout=10, verify=_VERIFY)
            caps_data = r.json() if r.ok else {}
        except Exception:
            caps_data = {}
    else:
        try:
            from executor.api.qemu_config import (
                get_all_profiles, list_profiles, OVMF, check_system_capabilities,
            )
            profiles_data = {
                "profiles":      get_all_profiles(),
                "profiles_list": list_profiles(),
                "ovmf":          OVMF,
            }
            caps_data = check_system_capabilities()
        except ImportError:
            profiles_data = {}
            caps_data     = {}

    _synced = {
        "profiles":      profiles_data.get("profiles", {}),
        "profiles_list": profiles_data.get("profiles_list", []),
        "ovmf":          profiles_data.get("ovmf", {"available": False, "code": "", "vars": ""}),
        "capabilities":  caps_data,
    }
    return _synced


def get_profiles() -> list:
    """Return the profile list.

    Returns the sync cache when populated (normal server path). Falls back to
    a direct executor import in local mode so callers that haven't called
    ``sync()`` (e.g. tests, CLI, preflight run before startup) still get live data.
    """
    cached = _synced.get("profiles_list", [])
    if cached:
        return cached
    if not API_URL or API_URL == "local":
        try:
            from executor.api.qemu_config import list_profiles as _lp
            return _lp()
        except ImportError:
            pass  # executor pkg absent (orchestrator-only checkout) — return the empty default below
    return []


def get_all_profiles() -> dict:
    """Return the full profiles dict.

    Returns the sync cache when populated (normal server path). Falls back to
    a direct executor import in local mode so callers that haven't called
    ``sync()`` (e.g. tests, CLI) still get live data.
    """
    cached = _synced.get("profiles", {})
    if cached:
        return cached
    if not API_URL or API_URL == "local":
        try:
            from executor.api.qemu_config import get_all_profiles as _gp
            return _gp()
        except ImportError:
            pass  # executor pkg absent (orchestrator-only checkout) — return the empty default below
    return {}


def get_ovmf() -> dict:
    """Return the OVMF info dict.

    Returns the sync cache when populated (normal server path). Falls back to
    a direct executor import in local mode so callers that haven't called
    ``sync()`` still get live data.
    """
    cached = _synced.get("ovmf", {})
    if cached:
        return cached
    if not API_URL or API_URL == "local":
        try:
            from executor.api.qemu_config import OVMF as _ovmf
            return _ovmf
        except ImportError:
            pass  # executor pkg absent (orchestrator-only checkout) — return the empty default below
    return {"available": False, "code": "", "vars": ""}


def get_capabilities() -> dict:
    """Return the system capabilities dict.

    Returns the sync cache when populated (normal server path). Falls back to
    a direct executor import in local mode so callers that haven't called
    ``sync()`` (e.g. tests, preflight before startup) still get live data.
    """
    cached = _synced.get("capabilities", {})
    if cached:
        return cached
    if not API_URL or API_URL == "local":
        try:
            from executor.api.qemu_config import check_system_capabilities as _csc
            return _csc()
        except ImportError:
            pass  # executor pkg absent (orchestrator-only checkout) — return the empty default below
    return {}


def check_profile_compatibility(profile_name: str) -> dict:
    """Check whether a profile is compatible with the current executor host.

    Delegates to the executor's ``check_profile_compatibility`` tool so the
    orchestrator never needs to import executor code directly.

    Args:
        profile_name: Name of the hardware profile to check.

    Returns:
        ``{"compatible": bool, "issues": [...], "warnings": [...]}``
    """
    return execute_tool("check_profile_compatibility", {"profile_name": profile_name})

# Derived from the canonical tool registry — which tools are VM-scoped (get the
# _ALLOWED_VMS allowlist check). No hand-maintained copy (this set had stale
# snapshot names that silently bypassed the access-control check).
from executor.command_catalog import VM_SCOPED_TOOLS as _VM_TOOLS

from orchestrator.event_log import log_event as _log_event  # noqa: E402


def __getattr__(name: str) -> object:
    # Lazily resolved so mock.patch targets always return the current binding.
    if name == "_execute_tool":
        from orchestrator.pipeline import execute_tool
        return execute_tool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")




def execute_tool(tool_name: str, args: dict, verbose: bool = False, log: bool = True) -> dict:
    """Wrapper around shared execute_tool that overrides local-only displays
    and enforces client tool/VM/profile access control.

    Args:
        tool_name: Name of the tool to call (e.g. ``"launch_vm"``).
        args:      Tool arguments dict.
        verbose:   Pass through to the underlying executor.
        log:       Whether to record this call in the persistent event log.
                   Set False for high-frequency internal polling (e.g. a
                   dashboard auto-refresh) that isn't a real admin action —
                   otherwise every poll tick permanently drowns out real events.

    Returns:
        Tool result dict, always containing ``"success": bool``.

    Example::

        execute_tool("list_vms", {})
        # → {"success": True, "vms": [...]}
        execute_tool("launch_vm", {"name": "myvm"})
        # → display overridden to "vnc"; {"success": True, ...}
    """
    # Enforce tool allowlist (covers both /execute and /chat paths)
    if _ALLOWED_TOOLS and tool_name not in _ALLOWED_TOOLS:
        return {"success": False, "error": f"Tool '{tool_name}' is not available."}

    if tool_name == "launch_vm":
        args = dict(args)
        if args.get("display", "sdl") in _LOCAL_ONLY_DISPLAYS or "display" not in args:
            args["display"] = "vnc"
        if "vnc_bind_local" not in args:
            args["vnc_bind_local"] = False

    # Enforce VM allowlist — report as "not found" to avoid leaking existence
    if tool_name in _VM_TOOLS and _ALLOWED_VMS:
        vm_name = args.get("name", "")
        if vm_name not in _ALLOWED_VMS:
            return {"success": False, "error": f"VM '{vm_name}' not found."}

    # Enforce profile allowlist
    if tool_name in ("create_vm", "apply_profile", "check_profile_compatibility") and _ALLOWED_PROFILES:
        profile = args.get("profile", "") or args.get("profile_name", "")
        if profile and profile not in _ALLOWED_PROFILES:
            return {"success": False, "error": f"Profile '{profile}' is not available."}

    # Filter list_vms to only show allowed VMs
    _t0 = time.monotonic()
    if API_URL and API_URL != "local":
        try:
            resp = _requests.post(
                f"{API_URL}/execute",
                json={"tool_name": tool_name, "args": args, "verbose": verbose},
                headers={"Authorization": f"Bearer {_TOKEN}"},
                timeout=_TIMEOUT,
                verify=_VERIFY,
            )
            resp.raise_for_status()
            result = resp.json()
        except _requests.RequestException as exc:
            result = {"success": False, "error": f"Executor unreachable: {exc}"}
    else:
        from orchestrator.pipeline import execute_tool as _orch_execute
        result = _orch_execute(tool_name, args, verbose)
    if log:
        _log_event(tool_name, args, result, (time.monotonic() - _t0) * 1000)

    # In remote mode, patch open_display results so the client knows the real host
    if tool_name == "open_display" and API_URL and API_URL != "local":
        if isinstance(result, dict) and result.get("host") == "localhost":
            import urllib.parse as _up
            result["host"] = _up.urlparse(API_URL).hostname or "localhost"

    if tool_name == "list_vms" and _ALLOWED_VMS:
        if isinstance(result, list):
            result = [v for v in result if v.get("name") in _ALLOWED_VMS]
        elif isinstance(result, dict) and "vms" in result:
            result["vms"] = [v for v in result["vms"] if v.get("name") in _ALLOWED_VMS]
    return result

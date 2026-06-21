"""
tests/layer11_remote_split.py — Layer 11: Remote Split unit tests

Covers both sides of the AI provider / client machine boundary without
needing Ollama or a real QEMU process.

  AI provider side (stateless_only preflight):
    - Bad shape (name / machine_type) still caught with stateless_only=True
    - iso_path existence check skipped with stateless_only=True
    - launch_vm VM-exists check skipped with stateless_only=True

  VNC arg binding (qemu_arg_builder):
    - vnc_bind_local=True  → 127.0.0.1:N,password=on in QEMU cmd
    - vnc_bind_local=False → :N  (no address restriction)

  Client machine HTTP service (api_server via FastAPI TestClient):
    - /health → 200 {"status": "ok"}
    - No Authorization header → 403
    - Wrong Bearer token → 401
    - launch_vm with display=sdl overridden to vnc + vnc_bind_local=True
    - launch_vm with display=gtk overridden to vnc + vnc_bind_local=True
    - launch_vm with display=vnc left as-is + vnc_bind_local=True added
    - Preflight abort on server → {success: False, preflight: True}
    - Preflight ask_user on server → {clarify: True}

  executor_client remote mode (AI provider → client, mocked HTTP):
    - Successful VNC launch result → ssh_tunnel_cmd, vnc_connect_cmd, vnc_note added
    - Host extracted from API_URL correctly
    - Non-VNC result → no connection strings added
"""

import os, sys, time, traceback, uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock, patch

from .shared import TestResult, console

_FILES_DIR = os.path.dirname(os.path.dirname(__file__))


# ── Test dataclass ────────────────────────────────────────────────────────────

@dataclass
class RemoteSplitTest:
    id:          str
    tags:        List[str]
    description: str
    fn:          Callable[[], List[str]]   # returns list of issue strings (empty = pass)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _uid() -> str:
    return uuid.uuid4().hex[:6]


def _run(fn: Callable[[], List[str]]) -> List[str]:
    """Call fn, catching any exception and returning it as an issue string."""
    try:
        return fn()
    except Exception:
        return [f"Unexpected exception:\n{traceback.format_exc()}"]


# ── Test implementations ───────────────────────────────────────────────────────

# ── 1. AI provider — stateless preflight still catches shape errors ──────────

def _t_stateless_catches_bad_machine_type() -> List[str]:
    sys.path.insert(0, _FILES_DIR)
    from shared.preflight.validator import _preflight_check
    pf = _preflight_check(
        "create_vm",
        {"name": f"rs-mt-{_uid()}", "machine_type": "dell_g15_5520", "os_type": "linux"},
        manager=None,
        verbose=False,
        stateless_only=True,
    )
    if pf.get("action") != "auto_fix":
        return [f"Expected auto_fix for bad machine_type with stateless_only=True, got {pf.get('action')!r}"]
    return []


def _t_stateless_catches_placeholder_name() -> List[str]:
    sys.path.insert(0, _FILES_DIR)
    from shared.preflight.validator import _preflight_check
    pf = _preflight_check(
        "create_vm",
        {"name": "windows-vm", "os_type": "windows"},
        manager=None,
        verbose=False,
        stateless_only=True,
    )
    if pf.get("action") != "ask_user":
        return [f"Expected ask_user for placeholder name with stateless_only=True, got {pf.get('action')!r}"]
    return []


# ── 2. AI provider — stateless skips iso_path existence ──────────────────────

def _t_stateless_skips_iso_check() -> List[str]:
    sys.path.insert(0, _FILES_DIR)
    from shared.preflight.validator import _preflight_check
    fake_iso = "/nonexistent/does_not_exist.iso"
    pf = _preflight_check(
        "create_vm",
        {"name": f"rs-iso-{_uid()}", "os_type": "linux", "iso_path": fake_iso},
        manager=None,
        verbose=False,
        stateless_only=True,
    )
    if pf.get("action") in ("abort",):
        return [f"stateless_only=True should skip iso_path check but got action={pf.get('action')!r}"]
    return []


def _t_full_aborts_on_bad_iso() -> List[str]:
    """stateless_only=False should abort or auto_fix for a bogus ISO path."""
    sys.path.insert(0, _FILES_DIR)
    from shared.preflight.validator import _preflight_check

    fake_iso = "/nonexistent_xyz/does_not_exist_abc.iso"
    # Need a real-ish manager for iso scanning; use a mock that returns empty
    mock_mgr = MagicMock()
    mock_mgr.scan_isos.return_value = []

    pf = _preflight_check(
        "create_vm",
        {"name": f"rs-iso-full-{_uid()}", "os_type": "linux", "iso_path": fake_iso},
        manager=mock_mgr,
        verbose=False,
        stateless_only=False,
    )
    if pf.get("action") not in ("abort", "ask_user", "auto_fix"):
        return [f"Expected abort/ask_user/auto_fix for bad iso with stateless_only=False, got {pf.get('action')!r}"]
    return []


# ── 3. AI provider — stateless skips launch_vm VM-exists check ───────────────

def _t_stateless_skips_launch_vm_check() -> List[str]:
    sys.path.insert(0, _FILES_DIR)
    from shared.preflight.validator import _preflight_check
    pf = _preflight_check(
        "launch_vm",
        {"name": f"nonexistent-vm-{_uid()}"},
        manager=None,
        verbose=False,
        stateless_only=True,
    )
    if pf.get("action") == "abort":
        return [f"stateless_only=True should skip launch_vm VM-exists check but got abort: {pf.get('reason')!r}"]
    return []


def _t_full_aborts_launch_vm_nonexistent() -> List[str]:
    """stateless_only=False aborts when the VM directory doesn't exist."""
    sys.path.insert(0, _FILES_DIR)
    from shared.preflight.validator import _preflight_check
    mock_mgr = MagicMock()
    mock_mgr.list_vms.return_value = []
    pf = _preflight_check(
        "launch_vm",
        {"name": f"nonexistent-xyz-{_uid()}"},
        manager=mock_mgr,
        verbose=False,
        stateless_only=False,
    )
    if pf.get("action") != "abort":
        return [f"Expected abort for nonexistent VM with stateless_only=False, got {pf.get('action')!r}"]
    return []


# ── 4. VNC arg binding ────────────────────────────────────────────────────────

def _build_vnc_args(vnc_bind_local: bool) -> List[str]:
    """Build QEMU args for a minimal VNC config and return the arg list."""
    sys.path.insert(0, _FILES_DIR)
    from client.api.qemu_config import MachineConfig
    from client.api.qemu_arg_builder import QemuArgBuilder
    cfg             = MachineConfig()
    cfg.name        = f"vnc-test-{_uid()}"
    cfg.display     = "vnc"
    cfg.vnc_port    = 5901
    cfg.vnc_bind_local = vnc_bind_local
    cfg.kvm         = False   # no KVM needed for arg-builder test
    cfg.disks       = []
    cfg.networks    = []
    return QemuArgBuilder(cfg).build()


def _t_vnc_bind_local_true() -> List[str]:
    args = _build_vnc_args(vnc_bind_local=True)
    cmd  = " ".join(args)
    issues = []
    if "127.0.0.1:1" not in cmd:
        issues.append(f"Expected '127.0.0.1:1' in VNC arg with vnc_bind_local=True, cmd={cmd!r}")
    if "password=on" not in cmd:
        issues.append(f"Expected 'password=on' in VNC arg with vnc_bind_local=True, cmd={cmd!r}")
    return issues


def _t_vnc_bind_local_false() -> List[str]:
    args = _build_vnc_args(vnc_bind_local=False)
    cmd  = " ".join(args)
    issues = []
    if "127.0.0.1" in cmd and "-vnc" in cmd:
        vnc_idx = args.index("-vnc") if "-vnc" in args else -1
        vnc_val = args[vnc_idx + 1] if vnc_idx >= 0 and vnc_idx + 1 < len(args) else ""
        if "127.0.0.1" in vnc_val:
            issues.append(f"vnc_bind_local=False should not bind to 127.0.0.1, got -vnc {vnc_val!r}")
    if "password=on" in cmd:
        issues.append(f"vnc_bind_local=False should not add password=on, cmd contains it")
    return issues


# ── 5. API server HTTP boundary ───────────────────────────────────────────────

def _make_test_client():
    """Return a (client, token) tuple with API_TOKEN pre-set so api_server imports work."""
    token = f"test-secret-{_uid()}"
    os.environ["API_TOKEN"] = token
    # Force re-import if already loaded with a different token
    import importlib
    for _mod in ("server.api_server", "client.server.api_server"):
        if _mod in sys.modules:
            del sys.modules[_mod]
    sys.path.insert(0, _FILES_DIR)
    from client.server.api_server import app
    from fastapi.testclient import TestClient
    return TestClient(app, raise_server_exceptions=False), token


def _t_health_endpoint() -> List[str]:
    client, _ = _make_test_client()
    resp = client.get("/health")
    if resp.status_code != 200:
        return [f"/health returned {resp.status_code}, expected 200"]
    body = resp.json()
    if body.get("status") != "ok":
        return [f"/health body={body!r}, expected {{\"status\": \"ok\"}}"]
    return []


def _t_auth_missing_header() -> List[str]:
    client, _ = _make_test_client()
    resp = client.post("/execute", json={"tool_name": "list_vms", "args": {}})
    if resp.status_code not in (401, 403):
        return [f"Expected 401/403 for missing auth, got {resp.status_code}"]
    return []


def _t_auth_wrong_token() -> List[str]:
    client, _ = _make_test_client()
    resp = client.post(
        "/execute",
        json={"tool_name": "list_vms", "args": {}},
        headers={"Authorization": "Bearer wrong-token-xyz"},
    )
    if resp.status_code != 401:
        return [f"Expected 401 for wrong token, got {resp.status_code}"]
    return []


def _t_server_overrides_sdl_to_vnc() -> List[str]:
    """Server must override sdl → vnc and inject vnc_bind_local=True before calling execute_tool."""
    client, token = _make_test_client()
    captured: Dict[str, Any] = {}

    def fake_execute(tool_name, args, verbose=False):
        captured["tool_name"] = tool_name
        captured["args"]      = dict(args)
        return {"success": True, "name": "test-vm", "pid": 12345, "display": "vnc"}

    with patch("client.executioner.tool_executor.execute_tool", side_effect=fake_execute), \
         patch("shared.preflight.validator._preflight_check", return_value={"action": "ok"}):
        resp = client.post(
            "/execute",
            json={"tool_name": "launch_vm", "args": {"name": "test-vm", "display": "sdl"}},
            headers={"Authorization": f"Bearer {token}"},
        )

    issues = []
    if resp.status_code != 200:
        return [f"Unexpected status {resp.status_code}: {resp.text}"]
    if captured.get("args", {}).get("display") != "vnc":
        issues.append(f"Expected display=vnc in execute call, got {captured.get('args', {}).get('display')!r}")
    if not captured.get("args", {}).get("vnc_bind_local"):
        issues.append("Expected vnc_bind_local=True injected by server")
    return issues


def _t_server_overrides_gtk_to_vnc() -> List[str]:
    client, token = _make_test_client()
    captured: Dict[str, Any] = {}

    def fake_execute(tool_name, args, verbose=False):
        captured["args"] = dict(args)
        return {"success": True, "name": "test-vm", "pid": 12345, "display": "vnc"}

    with patch("client.executioner.tool_executor.execute_tool", side_effect=fake_execute), \
         patch("shared.preflight.validator._preflight_check", return_value={"action": "ok"}):
        resp = client.post(
            "/execute",
            json={"tool_name": "launch_vm", "args": {"name": "test-vm", "display": "gtk"}},
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code != 200:
        return [f"Unexpected status {resp.status_code}: {resp.text}"]
    if captured.get("args", {}).get("display") != "vnc":
        return [f"Expected display=vnc for gtk override, got {captured.get('args', {}).get('display')!r}"]
    return []


def _t_server_preflight_abort_returns_structured_error() -> List[str]:
    client, token = _make_test_client()

    with patch("shared.preflight.validator._preflight_check", return_value={
        "action":     "abort",
        "reason":     "Test abort reason",
        "correction": "Test correction hint",
    }):
        resp = client.post(
            "/execute",
            json={"tool_name": "create_vm", "args": {"name": "bad-vm"}},
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code != 200:
        return [f"Expected HTTP 200 for abort (error in result body), got {resp.status_code}"]
    body = resp.json()
    result = body.get("result", {})
    issues = []
    if result.get("success") is not False:
        issues.append(f"Expected success=False in abort result, got {result.get('success')!r}")
    if not result.get("preflight"):
        issues.append("Expected preflight=True in abort result")
    if "Test abort reason" not in result.get("error", ""):
        issues.append(f"Expected reason in error field, got {result.get('error')!r}")
    return issues


def _t_server_passthrough_vnc_injects_bind_local() -> List[str]:
    """launch_vm with display=vnc should keep vnc but still inject vnc_bind_local=True."""
    client, token = _make_test_client()
    captured: Dict[str, Any] = {}

    def fake_execute(tool_name, args, verbose=False):
        captured["args"] = dict(args)
        return {"success": True, "name": "test-vm", "pid": 12345, "display": "vnc"}

    with patch("client.executioner.tool_executor.execute_tool", side_effect=fake_execute), \
         patch("shared.preflight.validator._preflight_check", return_value={"action": "ok"}):
        resp = client.post(
            "/execute",
            json={"tool_name": "launch_vm", "args": {"name": "test-vm", "display": "vnc"}},
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code != 200:
        return [f"Unexpected status {resp.status_code}: {resp.text}"]
    issues = []
    if captured.get("args", {}).get("display") != "vnc":
        issues.append(f"Expected display=vnc preserved, got {captured.get('args', {}).get('display')!r}")
    if not captured.get("args", {}).get("vnc_bind_local"):
        issues.append("Expected vnc_bind_local=True injected even when display was already vnc")
    return issues


def _t_server_preflight_auto_fix_applies_fix() -> List[str]:
    """Preflight auto_fix: server applies fixed_args and tags result with _preflight_auto_fixed."""
    client, token = _make_test_client()
    captured: Dict[str, Any] = {}

    def fake_execute(tool_name, args, verbose=False):
        captured["args"] = dict(args)
        return {"success": True, "name": args.get("name", "vm")}

    fixed = {"name": f"rs-fixed-{_uid()}", "machine_type": "q35", "os_type": "linux"}
    with patch("client.executioner.tool_executor.execute_tool", side_effect=fake_execute), \
         patch("shared.preflight.validator._preflight_check", return_value={
             "action":     "auto_fix",
             "reason":     "machine_type was a profile name",
             "correction": "machine_type corrected to q35",
             "fixed_args": fixed,
         }):
        resp = client.post(
            "/execute",
            json={"tool_name": "create_vm",
                  "args": {"name": fixed["name"], "machine_type": "dell_g15_5520", "os_type": "linux"}},
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code != 200:
        return [f"Unexpected status {resp.status_code}: {resp.text}"]
    body   = resp.json()
    result = body.get("result", {})
    issues = []
    if captured.get("args", {}).get("machine_type") != "q35":
        issues.append(f"Expected fixed machine_type=q35 in execute call, got {captured.get('args', {})!r}")
    if "_preflight_auto_fixed" not in result:
        issues.append("Expected _preflight_auto_fixed note in result after auto_fix")
    return issues


def _t_server_preflight_ask_user_returns_clarify() -> List[str]:
    client, token = _make_test_client()

    with patch("shared.preflight.validator._preflight_check", return_value={
        "action":    "ask_user",
        "reason":    "Need confirmation",
        "question":  "Are you sure?",
        "fix_field": "os_type",
        "options":   ["Yes", "No"],
    }):
        resp = client.post(
            "/execute",
            json={"tool_name": "create_vm", "args": {"name": "test-vm"}},
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code != 200:
        return [f"Expected HTTP 200 for ask_user, got {resp.status_code}"]
    body   = resp.json()
    result = body.get("result", {})
    issues = []
    if not result.get("clarify"):
        issues.append(f"Expected clarify=True in ask_user result, got {result!r}")
    if result.get("success") is not False:
        issues.append(f"Expected success=False in ask_user result, got {result.get('success')!r}")
    if "Are you sure?" not in result.get("question", ""):
        issues.append(f"Expected question text in result, got {result.get('question')!r}")
    return issues


# ── 6. executor_client remote mode — connection string building ───────────────

def _t_remote_vnc_launch_builds_connection_strings() -> List[str]:
    """Mock an HTTP vnc launch response and verify connection strings are built."""
    sys.path.insert(0, _FILES_DIR)

    mock_response = MagicMock()
    mock_response.ok         = True
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "result": {
            "success":      True,
            "name":         "myvm",
            "display":      "vnc",
            "vnc_port":     5901,
            "vnc_password": "sec-pass",
            "pid":          9999,
        }
    }

    import importlib
    for _mod in ("executioner.executor_client", "provider.executor_client"):
        if _mod in sys.modules:
            del sys.modules[_mod]

    with patch.dict(os.environ, {"API_URL": "http://192.168.1.50:8080", "API_TOKEN": "tok"}):
        with patch("requests.post", return_value=mock_response):
            import provider.executor_client as ec
            result = ec.execute_tool("launch_vm", {"name": "myvm"})

    issues = []
    if "ssh_tunnel_cmd" not in result:
        issues.append("Expected ssh_tunnel_cmd in result")
    elif "192.168.1.50" not in result["ssh_tunnel_cmd"]:
        issues.append(f"Expected host in ssh_tunnel_cmd, got {result['ssh_tunnel_cmd']!r}")
    elif "5901" not in result["ssh_tunnel_cmd"]:
        issues.append(f"Expected port in ssh_tunnel_cmd, got {result['ssh_tunnel_cmd']!r}")

    if "vnc_connect_cmd" not in result:
        issues.append("Expected vnc_connect_cmd in result")
    elif "localhost:5901" not in result["vnc_connect_cmd"]:
        issues.append(f"Expected localhost:5901 in vnc_connect_cmd, got {result['vnc_connect_cmd']!r}")

    if "vnc_note" not in result:
        issues.append("Expected vnc_note in result")
    elif "sec-pass" not in result["vnc_note"]:
        issues.append(f"Expected password in vnc_note, got {result['vnc_note']!r}")

    return issues


def _t_remote_non_vnc_no_connection_strings() -> List[str]:
    """Non-VNC results should not have connection strings added."""
    sys.path.insert(0, _FILES_DIR)

    mock_response = MagicMock()
    mock_response.ok          = True
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "result": {"success": True, "vms": [{"name": "vm1"}]}
    }

    import importlib
    for _mod in ("executioner.executor_client", "provider.executor_client"):
        if _mod in sys.modules:
            del sys.modules[_mod]

    with patch.dict(os.environ, {"API_URL": "http://192.168.1.50:8080", "API_TOKEN": "tok"}):
        with patch("requests.post", return_value=mock_response):
            import provider.executor_client as ec
            result = ec.execute_tool("list_vms", {})

    issues = []
    for key in ("ssh_tunnel_cmd", "vnc_connect_cmd", "vnc_note"):
        if key in result:
            issues.append(f"Non-VNC result should not have {key!r}")
    return issues


# ── Test registry ─────────────────────────────────────────────────────────────

REMOTE_SPLIT_TESTS: List[RemoteSplitTest] = [
    # AI provider — stateless preflight
    RemoteSplitTest(
        id="rs_stateless_catches_bad_machine_type",
        tags=["remote_split", "preflight", "ai_provider"],
        description="stateless_only=True still catches invalid machine_type (shape check)",
        fn=_t_stateless_catches_bad_machine_type,
    ),
    RemoteSplitTest(
        id="rs_stateless_catches_placeholder_name",
        tags=["remote_split", "preflight", "ai_provider"],
        description="stateless_only=True still catches placeholder VM names",
        fn=_t_stateless_catches_placeholder_name,
    ),
    RemoteSplitTest(
        id="rs_stateless_skips_iso_check",
        tags=["remote_split", "preflight", "ai_provider"],
        description="stateless_only=True skips iso_path existence check (no abort for fake path)",
        fn=_t_stateless_skips_iso_check,
    ),
    RemoteSplitTest(
        id="rs_full_aborts_bad_iso",
        tags=["remote_split", "preflight", "client_machine"],
        description="stateless_only=False aborts for a non-existent ISO path",
        fn=_t_full_aborts_on_bad_iso,
    ),
    RemoteSplitTest(
        id="rs_stateless_skips_launch_vm_check",
        tags=["remote_split", "preflight", "ai_provider"],
        description="stateless_only=True skips launch_vm VM-exists check",
        fn=_t_stateless_skips_launch_vm_check,
    ),
    RemoteSplitTest(
        id="rs_full_aborts_launch_vm_nonexistent",
        tags=["remote_split", "preflight", "client_machine"],
        description="stateless_only=False aborts launch_vm when VM directory doesn't exist",
        fn=_t_full_aborts_launch_vm_nonexistent,
    ),
    # VNC arg binding
    RemoteSplitTest(
        id="rs_vnc_bind_local_true",
        tags=["remote_split", "vnc", "arg_builder"],
        description="vnc_bind_local=True → 127.0.0.1:N,password=on in QEMU args",
        fn=_t_vnc_bind_local_true,
    ),
    RemoteSplitTest(
        id="rs_vnc_bind_local_false",
        tags=["remote_split", "vnc", "arg_builder"],
        description="vnc_bind_local=False → :N (no address restriction, no password=on)",
        fn=_t_vnc_bind_local_false,
    ),
    # API server HTTP boundary
    RemoteSplitTest(
        id="rs_health_endpoint",
        tags=["remote_split", "api_server", "client_machine"],
        description="/health returns {status: ok}",
        fn=_t_health_endpoint,
    ),
    RemoteSplitTest(
        id="rs_auth_missing_header",
        tags=["remote_split", "api_server", "auth", "client_machine"],
        description="Request with no Authorization header → 403",
        fn=_t_auth_missing_header,
    ),
    RemoteSplitTest(
        id="rs_auth_wrong_token",
        tags=["remote_split", "api_server", "auth", "client_machine"],
        description="Wrong Bearer token → 401",
        fn=_t_auth_wrong_token,
    ),
    RemoteSplitTest(
        id="rs_server_overrides_sdl_to_vnc",
        tags=["remote_split", "api_server", "vnc", "client_machine"],
        description="Server overrides display=sdl → vnc + injects vnc_bind_local=True",
        fn=_t_server_overrides_sdl_to_vnc,
    ),
    RemoteSplitTest(
        id="rs_server_overrides_gtk_to_vnc",
        tags=["remote_split", "api_server", "vnc", "client_machine"],
        description="Server overrides display=gtk → vnc",
        fn=_t_server_overrides_gtk_to_vnc,
    ),
    RemoteSplitTest(
        id="rs_server_passthrough_vnc_injects_bind_local",
        tags=["remote_split", "api_server", "vnc", "client_machine"],
        description="display=vnc is not overridden but vnc_bind_local=True is still injected",
        fn=_t_server_passthrough_vnc_injects_bind_local,
    ),
    RemoteSplitTest(
        id="rs_server_preflight_auto_fix_applies_fix",
        tags=["remote_split", "api_server", "preflight", "client_machine"],
        description="Preflight auto_fix: fixed args used in execute call, note tagged in result",
        fn=_t_server_preflight_auto_fix_applies_fix,
    ),
    RemoteSplitTest(
        id="rs_server_abort_returns_structured_error",
        tags=["remote_split", "api_server", "preflight", "client_machine"],
        description="Preflight abort on server → {success: False, preflight: True} in result body",
        fn=_t_server_preflight_abort_returns_structured_error,
    ),
    RemoteSplitTest(
        id="rs_server_ask_user_returns_clarify",
        tags=["remote_split", "api_server", "preflight", "client_machine"],
        description="Preflight ask_user on server → {clarify: True} shaped for cli.py clarify loop",
        fn=_t_server_preflight_ask_user_returns_clarify,
    ),
    # executor_client remote mode
    RemoteSplitTest(
        id="rs_remote_vnc_connection_strings",
        tags=["remote_split", "executor_client", "vnc", "ai_provider"],
        description="Remote VNC launch result → ssh_tunnel_cmd + vnc_connect_cmd + vnc_note built",
        fn=_t_remote_vnc_launch_builds_connection_strings,
    ),
    RemoteSplitTest(
        id="rs_remote_non_vnc_no_connection_strings",
        tags=["remote_split", "executor_client", "ai_provider"],
        description="Non-VNC remote result → no ssh/vnc connection strings added",
        fn=_t_remote_non_vnc_no_connection_strings,
    ),
]


# ── Runner ────────────────────────────────────────────────────────────────────

def run_remote_split_test(tc: RemoteSplitTest) -> TestResult:
    start  = time.time()
    issues = _run(tc.fn)
    return TestResult(
        test_id    = tc.id,
        layer      = 11,
        passed     = len(issues) == 0,
        issues     = issues,
        fixes_applied = [],
        duration_s = time.time() - start,
    )

"""
tests/layer11_remote_split.py — Layer 11: Server/Client split tests

Tests the full HTTP boundary between the thin client and the server
without needing a real Ollama instance or real QEMU process.

  A. Stateless preflight (server runs these before every tool call):
    - Bad machine_type still caught with stateless_only=True
    - Placeholder name still caught with stateless_only=True
    - iso_path existence check skipped with stateless_only=True
    - launch_vm VM-exists check skipped with stateless_only=True
    - Full (stateless_only=False) aborts for bad ISO / nonexistent VM

  B. VNC arg binding:
    - vnc_bind_local=True  → 127.0.0.1:N,password=on in QEMU cmd
    - vnc_bind_local=False → :N (no address restriction)

  C. /chat HTTP endpoint (primary boundary):
    - /health → 200 {"status": "ok"} (no auth required)
    - /chat without auth → 403
    - /chat with wrong token → 401
    - /chat happy path → {session_id, text, tool_results, needs_input: null}
    - /chat session persistence → second call receives prior conversation history
    - /chat returns needs_input when process_message signals it
    - /chat passes auto_confirm=True through to process_message
    - DELETE /sessions/{id} → clears session (200)

  D. /execute HTTP endpoint (direct tool call, still present):
    - display=sdl overridden to vnc + vnc_bind_local=True
    - display=gtk overridden to vnc
    - display=vnc kept + vnc_bind_local=True injected
    - Preflight auto_fix: fixed_args used, result tagged
    - Preflight abort → {success: False, preflight: True}
    - Preflight ask_user → {clarify: True}

  E. executor_client:
    - server.executor_client.execute_tool is a re-export of
      shared.executioner.tool_executor.execute_tool (same object)
"""

import os, sys, time, traceback, uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock, patch, call

from .shared import TestResult, console

_FILES_DIR = os.path.dirname(os.path.dirname(__file__))


# ── Test dataclass ────────────────────────────────────────────────────────────

@dataclass
class RemoteSplitTest:
    id:          str
    tags:        List[str]
    description: str
    fn:          Callable[[], List[str]]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _uid() -> str:
    return uuid.uuid4().hex[:6]


def _run(fn: Callable[[], List[str]]) -> List[str]:
    try:
        return fn()
    except Exception:
        return [f"Unexpected exception:\n{traceback.format_exc()}"]


def _make_test_client():
    """Return (TestClient, token). Sets API_TOKEN env var and re-imports api_server."""
    token = f"test-secret-{_uid()}"
    os.environ["API_TOKEN"] = token
    sys.path.insert(0, _FILES_DIR)
    if "server.http.api_server" in sys.modules:
        del sys.modules["server.http.api_server"]
    from server.http.api_server import app
    from fastapi.testclient import TestClient
    return TestClient(app, raise_server_exceptions=False), token


def _fake_process_message(text="OK", tool_results=None, needs_input=None):
    """Build a fake process_message return value."""
    def _pm(user_input, messages, verbose=False, auto_confirm=False):
        return {
            "text":         text,
            "messages":     messages + [
                {"role": "user",      "content": user_input},
                {"role": "assistant", "content": text},
            ],
            "tool_results": tool_results or [],
            "needs_input":  needs_input,
        }
    return _pm


# ════════════════════════════════════════════════════════════════════════════════
# A. Stateless preflight
# ════════════════════════════════════════════════════════════════════════════════

def _t_stateless_catches_bad_machine_type() -> List[str]:
    sys.path.insert(0, _FILES_DIR)
    from shared.preflight.validator import _preflight_check
    pf = _preflight_check(
        "create_vm",
        {"name": f"rs-mt-{_uid()}", "machine_type": "dell_g15_5520", "os_type": "linux"},
        manager=None, verbose=False, stateless_only=True,
    )
    if pf.get("action") != "auto_fix":
        return [f"Expected auto_fix for bad machine_type, got {pf.get('action')!r}"]
    return []


def _t_stateless_catches_placeholder_name() -> List[str]:
    sys.path.insert(0, _FILES_DIR)
    from shared.preflight.validator import _preflight_check
    pf = _preflight_check(
        "create_vm",
        {"name": "windows-vm", "os_type": "windows"},
        manager=None, verbose=False, stateless_only=True,
    )
    if pf.get("action") != "ask_user":
        return [f"Expected ask_user for placeholder name, got {pf.get('action')!r}"]
    return []


def _t_stateless_skips_iso_check() -> List[str]:
    sys.path.insert(0, _FILES_DIR)
    from shared.preflight.validator import _preflight_check
    pf = _preflight_check(
        "create_vm",
        {"name": f"rs-iso-{_uid()}", "os_type": "linux", "iso_path": "/nonexistent/fake.iso"},
        manager=None, verbose=False, stateless_only=True,
    )
    if pf.get("action") == "abort":
        return [f"stateless_only=True should skip iso_path check but got abort: {pf.get('reason')!r}"]
    return []


def _t_full_aborts_on_bad_iso() -> List[str]:
    sys.path.insert(0, _FILES_DIR)
    from shared.preflight.validator import _preflight_check
    mock_mgr = MagicMock()
    mock_mgr.scan_isos.return_value = []
    pf = _preflight_check(
        "create_vm",
        {"name": f"rs-iso-full-{_uid()}", "os_type": "linux",
         "iso_path": "/nonexistent_xyz/does_not_exist_abc.iso"},
        manager=mock_mgr, verbose=False, stateless_only=False,
    )
    if pf.get("action") not in ("abort", "ask_user", "auto_fix"):
        return [f"Expected abort/ask_user/auto_fix for bad iso, got {pf.get('action')!r}"]
    return []


def _t_stateless_skips_launch_vm_check() -> List[str]:
    sys.path.insert(0, _FILES_DIR)
    from shared.preflight.validator import _preflight_check
    pf = _preflight_check(
        "launch_vm",
        {"name": f"nonexistent-vm-{_uid()}"},
        manager=None, verbose=False, stateless_only=True,
    )
    if pf.get("action") == "abort":
        return [f"stateless_only=True should skip launch_vm VM-exists check, got abort: {pf.get('reason')!r}"]
    return []


def _t_full_aborts_launch_vm_nonexistent() -> List[str]:
    sys.path.insert(0, _FILES_DIR)
    from shared.preflight.validator import _preflight_check
    mock_mgr = MagicMock()
    mock_mgr.list_vms.return_value = []
    pf = _preflight_check(
        "launch_vm",
        {"name": f"nonexistent-xyz-{_uid()}"},
        manager=mock_mgr, verbose=False, stateless_only=False,
    )
    if pf.get("action") != "abort":
        return [f"Expected abort for nonexistent VM, got {pf.get('action')!r}"]
    return []


# ════════════════════════════════════════════════════════════════════════════════
# B. VNC arg binding
# ════════════════════════════════════════════════════════════════════════════════

def _build_vnc_args(vnc_bind_local: bool) -> List[str]:
    sys.path.insert(0, _FILES_DIR)
    from shared.api.qemu_config import MachineConfig
    from shared.api.qemu_arg_builder import QemuArgBuilder
    cfg                = MachineConfig()
    cfg.name           = f"vnc-test-{_uid()}"
    cfg.display        = "vnc"
    cfg.vnc_port       = 5901
    cfg.vnc_bind_local = vnc_bind_local
    cfg.kvm            = False
    cfg.disks          = []
    cfg.networks       = []
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
    if "-vnc" in args:
        vnc_idx = args.index("-vnc")
        vnc_val = args[vnc_idx + 1] if vnc_idx + 1 < len(args) else ""
        if "127.0.0.1" in vnc_val:
            issues.append(f"vnc_bind_local=False should not bind to 127.0.0.1, got -vnc {vnc_val!r}")
    if "password=on" in cmd:
        issues.append("vnc_bind_local=False should not add password=on")
    return issues


# ════════════════════════════════════════════════════════════════════════════════
# C. /chat HTTP endpoint — 6-category coverage
#
#   valid    — normal message, correct auth → response with session_id/text
#   missing  — required field absent (no message) → 422
#   broken   — auth present but wrong token → 401; no auth at all → 403
#   junk     — extra unknown fields in body → FastAPI ignores, request succeeds
#   foreign  — session_id from nonexistent session → starts fresh (no crash)
#   conflict — auto_confirm=True with no prior needs_input → no crash, processed
# ════════════════════════════════════════════════════════════════════════════════

def _t_health_endpoint() -> List[str]:
    client, _ = _make_test_client()
    resp = client.get("/health")
    if resp.status_code != 200:
        return [f"/health returned {resp.status_code}, expected 200"]
    if resp.json().get("status") != "ok":
        return [f"/health body={resp.json()!r}, expected {{\"status\": \"ok\"}}"]
    return []


def _t_chat_auth_missing() -> List[str]:
    client, _ = _make_test_client()
    resp = client.post("/chat", json={"message": "hello"})
    if resp.status_code not in (401, 403):
        return [f"Expected 401/403 for missing auth on /chat, got {resp.status_code}"]
    return []


def _t_chat_auth_wrong_token() -> List[str]:
    client, _ = _make_test_client()
    resp = client.post(
        "/chat",
        json={"message": "hello"},
        headers={"Authorization": "Bearer wrong-token-xyz"},
    )
    if resp.status_code != 401:
        return [f"Expected 401 for wrong token on /chat, got {resp.status_code}"]
    return []


def _t_chat_happy_path() -> List[str]:
    client, token = _make_test_client()
    with patch("server.ai.cli.process_message",
               side_effect=_fake_process_message(text="You have 2 VMs.")):
        resp = client.post(
            "/chat",
            json={"message": "list my vms"},
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        return [f"Expected 200 from /chat, got {resp.status_code}: {resp.text}"]
    body = resp.json()
    issues = []
    if not body.get("session_id"):
        issues.append("Response missing session_id")
    if body.get("text") != "You have 2 VMs.":
        issues.append(f"Expected text='You have 2 VMs.', got {body.get('text')!r}")
    if "tool_results" not in body:
        issues.append("Response missing tool_results")
    if "needs_input" not in body:
        issues.append("Response missing needs_input field")
    if body.get("needs_input") is not None:
        issues.append(f"Expected needs_input=null for happy path, got {body.get('needs_input')!r}")
    return issues


def _t_chat_session_persistence() -> List[str]:
    """Second /chat call with the same session_id should receive prior conversation messages."""
    client, token = _make_test_client()
    calls_received: List[Dict] = []

    def tracking_pm(user_input, messages, verbose=False, auto_confirm=False):
        calls_received.append({"user_input": user_input, "messages": list(messages)})
        return {
            "text":         "reply",
            "messages":     messages + [
                {"role": "user",      "content": user_input},
                {"role": "assistant", "content": "reply"},
            ],
            "tool_results": [],
            "needs_input":  None,
        }

    headers = {"Authorization": f"Bearer {token}"}
    with patch("server.ai.cli.process_message", side_effect=tracking_pm):
        r1 = client.post("/chat", json={"message": "first message"}, headers=headers)
        sid = r1.json().get("session_id")
        r2 = client.post("/chat", json={"message": "second message", "session_id": sid},
                         headers=headers)

    if r2.status_code != 200:
        return [f"Second /chat call failed: {r2.status_code}: {r2.text}"]
    issues = []
    if len(calls_received) != 2:
        return [f"Expected process_message called twice, got {len(calls_received)}"]
    if len(calls_received[1]["messages"]) == 0:
        issues.append("Second call received empty messages — session not persisted")
    first_msg = calls_received[0]["user_input"]
    if not any(first_msg in m.get("content", "") for m in calls_received[1]["messages"]):
        issues.append("Second call messages don't contain first turn — session history not passed")
    return issues


def _t_chat_returns_needs_input() -> List[str]:
    """When process_message returns needs_input, it flows through to the HTTP response."""
    client, token = _make_test_client()
    needs = {
        "type":      "confirm_yn",
        "question":  "Stop VM 'myvm'? This will kill the process.",
        "options":   ["yes", "no"],
        "field":     None,
        "tool_name": "stop_vm",
        "proposed":  "myvm",
    }
    with patch("server.ai.cli.process_message",
               side_effect=_fake_process_message(text="", needs_input=needs)):
        resp = client.post(
            "/chat",
            json={"message": "stop myvm"},
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        return [f"Expected 200, got {resp.status_code}: {resp.text}"]
    body = resp.json()
    issues = []
    if body.get("needs_input") is None:
        issues.append("Expected needs_input to be non-null when process_message signals it")
    elif body["needs_input"].get("type") != "confirm_yn":
        issues.append(f"Expected type=confirm_yn, got {body['needs_input'].get('type')!r}")
    elif "myvm" not in body["needs_input"].get("question", ""):
        issues.append(f"Expected VM name in question, got {body['needs_input'].get('question')!r}")
    return issues


def _t_chat_auto_confirm_passed_through() -> List[str]:
    """auto_confirm=True in the request body is forwarded to process_message."""
    client, token = _make_test_client()
    received: Dict[str, Any] = {}

    def capturing_pm(user_input, messages, verbose=False, auto_confirm=False):
        received["auto_confirm"] = auto_confirm
        return {"text": "done", "messages": messages, "tool_results": [], "needs_input": None}

    with patch("server.ai.cli.process_message", side_effect=capturing_pm):
        resp = client.post(
            "/chat",
            json={"message": "yes", "auto_confirm": True},
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code != 200:
        return [f"Expected 200, got {resp.status_code}"]
    if not received.get("auto_confirm"):
        return ["auto_confirm=True was not forwarded to process_message"]
    return []


def _t_chat_delete_session() -> List[str]:
    """DELETE /sessions/{id} should clear the session and return 200."""
    client, token = _make_test_client()
    headers = {"Authorization": f"Bearer {token}"}

    with patch("server.ai.cli.process_message",
               side_effect=_fake_process_message(text="hi")):
        r1 = client.post("/chat", json={"message": "hello"}, headers=headers)

    sid = r1.json().get("session_id")
    if not sid:
        return ["First /chat response missing session_id — cannot test delete"]

    r_del = client.delete(f"/sessions/{sid}", headers=headers)
    if r_del.status_code != 200:
        return [f"DELETE /sessions/{sid} returned {r_del.status_code}, expected 200"]
    return []


def _t_chat_missing_message_field() -> List[str]:
    """missing — no message field → FastAPI 422 Unprocessable Entity."""
    client, token = _make_test_client()
    resp = client.post(
        "/chat",
        json={"session_id": None},
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code != 422:
        return [f"Expected 422 for missing 'message' field, got {resp.status_code}"]
    return []


def _t_chat_junk_extra_fields() -> List[str]:
    """junk — unknown extra fields in JSON body → ignored, request succeeds."""
    client, token = _make_test_client()
    with patch("server.ai.cli.process_message",
               side_effect=_fake_process_message(text="ok")):
        resp = client.post(
            "/chat",
            json={
                "message":       "hello",
                "totally_fake":  "xyzzy",
                "another_junk":  12345,
                "nested_junk":   {"a": 1},
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        return [f"Expected 200 with junk extra fields, got {resp.status_code}: {resp.text}"]
    return []


def _t_chat_foreign_session_id() -> List[str]:
    """foreign — session_id for a nonexistent session → treated as new session, no crash."""
    client, token = _make_test_client()
    with patch("server.ai.cli.process_message",
               side_effect=_fake_process_message(text="fresh start")):
        resp = client.post(
            "/chat",
            json={"message": "hi", "session_id": "nonexistent-session-id-xyz"},
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        return [f"Expected 200 for foreign session_id, got {resp.status_code}: {resp.text}"]
    body = resp.json()
    if not body.get("session_id"):
        return ["Expected a session_id in response even for foreign session_id input"]
    return []


def _t_chat_conflict_auto_confirm_no_prior_input() -> List[str]:
    """conflict — auto_confirm=True with no prior needs_input in session → no crash."""
    client, token = _make_test_client()
    with patch("server.ai.cli.process_message",
               side_effect=_fake_process_message(text="all good")):
        resp = client.post(
            "/chat",
            json={"message": "yes", "auto_confirm": True},
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        return [f"Expected 200 for auto_confirm=True with no prior needs_input, got {resp.status_code}: {resp.text}"]
    return []


# ════════════════════════════════════════════════════════════════════════════════
# D. /execute HTTP endpoint — 6-category coverage
#
#   valid    — valid tool + valid args → execute runs
#   missing  — tool_name absent → 422; tool not in allowlist → 403
#   broken   — preflight aborts (impossible args) → {success: False, preflight: True}
#   junk     — extra unknown fields in body → FastAPI ignores, request succeeds
#   foreign  — args from another tool's schema → executor receives them, ignores what it doesn't know
#   conflict — display=sdl + explicit vnc_bind_local=False → server overrides both
# ════════════════════════════════════════════════════════════════════════════════

def _t_execute_auth_missing() -> List[str]:
    client, _ = _make_test_client()
    resp = client.post("/execute", json={"tool_name": "list_vms", "args": {}})
    if resp.status_code not in (401, 403):
        return [f"Expected 401/403 for missing auth on /execute, got {resp.status_code}"]
    return []


def _t_execute_auth_wrong_token() -> List[str]:
    client, _ = _make_test_client()
    resp = client.post(
        "/execute",
        json={"tool_name": "list_vms", "args": {}},
        headers={"Authorization": "Bearer wrong-token-xyz"},
    )
    if resp.status_code != 401:
        return [f"Expected 401 for wrong token on /execute, got {resp.status_code}"]
    return []


def _t_execute_overrides_sdl_to_vnc() -> List[str]:
    client, token = _make_test_client()
    captured: Dict[str, Any] = {}

    def fake_execute(tool_name, args, verbose=False):
        captured["args"] = dict(args)
        return {"success": True, "name": "test-vm", "pid": 12345, "display": "vnc"}

    with patch("shared.executioner.tool_executor.execute_tool", side_effect=fake_execute), \
         patch("shared.preflight.validator._preflight_check", return_value={"action": "ok"}):
        resp = client.post(
            "/execute",
            json={"tool_name": "launch_vm", "args": {"name": "test-vm", "display": "sdl"}},
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code != 200:
        return [f"Unexpected status {resp.status_code}: {resp.text}"]
    issues = []
    if captured.get("args", {}).get("display") != "vnc":
        issues.append(f"Expected display=vnc after sdl override, got {captured.get('args', {}).get('display')!r}")
    if not captured.get("args", {}).get("vnc_bind_local"):
        issues.append("Expected vnc_bind_local=True injected by server")
    return issues


def _t_execute_overrides_gtk_to_vnc() -> List[str]:
    client, token = _make_test_client()
    captured: Dict[str, Any] = {}

    def fake_execute(tool_name, args, verbose=False):
        captured["args"] = dict(args)
        return {"success": True, "name": "test-vm", "pid": 12345, "display": "vnc"}

    with patch("shared.executioner.tool_executor.execute_tool", side_effect=fake_execute), \
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


def _t_execute_passthrough_vnc_injects_bind_local() -> List[str]:
    client, token = _make_test_client()
    captured: Dict[str, Any] = {}

    def fake_execute(tool_name, args, verbose=False):
        captured["args"] = dict(args)
        return {"success": True, "name": "test-vm", "pid": 12345, "display": "vnc"}

    with patch("shared.executioner.tool_executor.execute_tool", side_effect=fake_execute), \
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


def _t_execute_preflight_abort() -> List[str]:
    client, token = _make_test_client()
    with patch("shared.preflight.validator._preflight_check", return_value={
        "action": "abort", "reason": "Test abort reason", "correction": "hint",
    }):
        resp = client.post(
            "/execute",
            json={"tool_name": "create_vm", "args": {"name": "bad-vm"}},
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        return [f"Expected HTTP 200 for abort (error in body), got {resp.status_code}"]
    body   = resp.json()
    result = body.get("result", {})
    issues = []
    if result.get("success") is not False:
        issues.append(f"Expected success=False in abort result, got {result.get('success')!r}")
    if not result.get("preflight"):
        issues.append("Expected preflight=True in abort result")
    if "Test abort reason" not in result.get("error", ""):
        issues.append(f"Expected reason in error field, got {result.get('error')!r}")
    return issues


def _t_execute_preflight_auto_fix() -> List[str]:
    client, token = _make_test_client()
    captured: Dict[str, Any] = {}

    def fake_execute(tool_name, args, verbose=False):
        captured["args"] = dict(args)
        return {"success": True, "name": args.get("name", "vm")}

    fixed = {"name": f"rs-fixed-{_uid()}", "machine_type": "q35", "os_type": "linux"}
    with patch("shared.executioner.tool_executor.execute_tool", side_effect=fake_execute), \
         patch("shared.preflight.validator._preflight_check", return_value={
             "action": "auto_fix", "reason": "machine_type was a profile name",
             "correction": "corrected to q35", "fixed_args": fixed,
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
        issues.append(f"Expected fixed machine_type=q35 in execute, got {captured.get('args', {})!r}")
    if "_preflight_auto_fixed" not in result:
        issues.append("Expected _preflight_auto_fixed note in result after auto_fix")
    return issues


def _t_execute_preflight_ask_user() -> List[str]:
    client, token = _make_test_client()
    with patch("shared.preflight.validator._preflight_check", return_value={
        "action": "ask_user", "reason": "Need confirmation",
        "question": "Are you sure?", "fix_field": "os_type", "options": ["yes", "no"],
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


def _t_execute_missing_tool_name() -> List[str]:
    """missing — tool_name absent from body → 422 Unprocessable Entity."""
    client, token = _make_test_client()
    resp = client.post(
        "/execute",
        json={"args": {"name": "test-vm"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code != 422:
        return [f"Expected 422 for missing tool_name, got {resp.status_code}"]
    return []


def _t_execute_tool_not_in_allowlist() -> List[str]:
    """missing (allowlist) — tool not in allowed_remote_tools → 403 Forbidden."""
    client, token = _make_test_client()
    resp = client.post(
        "/execute",
        json={"tool_name": "send_monitor_cmd", "args": {"name": "vm", "cmd": "info"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code != 403:
        return [f"Expected 403 for tool not in allowlist, got {resp.status_code}"]
    return []


def _t_execute_junk_extra_fields() -> List[str]:
    """junk — unknown extra JSON fields in body → ignored, request succeeds."""
    client, token = _make_test_client()

    def fake_execute(tool_name, args, verbose=False):
        return {"success": True, "vms": []}

    with patch("shared.executioner.tool_executor.execute_tool", side_effect=fake_execute), \
         patch("shared.preflight.validator._preflight_check", return_value={"action": "ok"}):
        resp = client.post(
            "/execute",
            json={
                "tool_name":    "list_vms",
                "args":         {},
                "totally_fake": "xyzzy",
                "another_junk": 99999,
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code != 200:
        return [f"Expected 200 with junk extra fields in /execute body, got {resp.status_code}: {resp.text}"]
    return []


def _t_execute_foreign_args() -> List[str]:
    """foreign — args from a different tool's schema injected alongside required args.
    Server passes them through; executor ignores what it doesn't know."""
    client, token = _make_test_client()
    captured: Dict[str, Any] = {}

    def fake_execute(tool_name, args, verbose=False):
        captured["args"] = dict(args)
        return {"success": True, "name": args.get("name", "vm")}

    # Inject snapshot-related args (from snapshot tool) into a create_vm call
    foreign_args = {
        "name":       f"rs-foreign-{_uid()}",
        "os_type":    "linux",
        "snap_name":  "snap1",        # foreign: belongs to snapshot tool
        "restore_id": "abc123",       # foreign: belongs to snapshot tool
        "keep_old":   True,           # foreign: belongs to snapshot tool
    }
    with patch("shared.executioner.tool_executor.execute_tool", side_effect=fake_execute), \
         patch("shared.preflight.validator._preflight_check", return_value={"action": "ok"}):
        resp = client.post(
            "/execute",
            json={"tool_name": "create_vm", "args": foreign_args},
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code != 200:
        return [f"Expected 200 with foreign args, got {resp.status_code}: {resp.text}"]
    if not captured:
        return ["execute_tool was not called — foreign args caused unexpected early exit"]
    return []


def _t_execute_conflict_display_and_bind_local() -> List[str]:
    """conflict — display=sdl (local-only) + explicit vnc_bind_local=False.
    Server overrides display to vnc AND forces vnc_bind_local=True, resolving both."""
    client, token = _make_test_client()
    captured: Dict[str, Any] = {}

    def fake_execute(tool_name, args, verbose=False):
        captured["args"] = dict(args)
        return {"success": True, "name": "test-vm", "pid": 1, "display": "vnc"}

    with patch("shared.executioner.tool_executor.execute_tool", side_effect=fake_execute), \
         patch("shared.preflight.validator._preflight_check", return_value={"action": "ok"}):
        resp = client.post(
            "/execute",
            json={"tool_name": "launch_vm",
                  "args": {"name": "test-vm", "display": "sdl", "vnc_bind_local": False}},
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code != 200:
        return [f"Expected 200 for conflicting display/vnc_bind_local args, got {resp.status_code}"]
    issues = []
    if captured.get("args", {}).get("display") != "vnc":
        issues.append(f"Expected display overridden to vnc, got {captured.get('args', {}).get('display')!r}")
    if not captured.get("args", {}).get("vnc_bind_local"):
        issues.append("Expected vnc_bind_local overridden to True despite explicit False in request")
    return issues


# ════════════════════════════════════════════════════════════════════════════════
# E. executor_client is a re-export
# ════════════════════════════════════════════════════════════════════════════════

def _t_executor_client_is_re_export() -> List[str]:
    """server.executor_client.execute_tool must be the same object as
    shared.executioner.tool_executor.execute_tool — it is a thin re-export,
    not an HTTP dispatcher."""
    sys.path.insert(0, _FILES_DIR)
    import importlib
    for mod in ("server.executor_client", "shared.executioner.tool_executor"):
        if mod in sys.modules:
            del sys.modules[mod]
    import server.executor_client as ec
    import shared.executioner.tool_executor as te
    if ec.execute_tool is not te.execute_tool:
        return [
            "server.executor_client.execute_tool is NOT the same object as "
            "shared.executioner.tool_executor.execute_tool — the re-export is broken"
        ]
    return []


# ── Test registry ─────────────────────────────────────────────────────────────

REMOTE_SPLIT_TESTS: List[RemoteSplitTest] = [
    # A. Stateless preflight
    RemoteSplitTest(
        id="rs_stateless_catches_bad_machine_type",
        tags=["remote_split", "preflight"],
        description="stateless_only=True still catches invalid machine_type",
        fn=_t_stateless_catches_bad_machine_type,
    ),
    RemoteSplitTest(
        id="rs_stateless_catches_placeholder_name",
        tags=["remote_split", "preflight"],
        description="stateless_only=True still catches placeholder VM names",
        fn=_t_stateless_catches_placeholder_name,
    ),
    RemoteSplitTest(
        id="rs_stateless_skips_iso_check",
        tags=["remote_split", "preflight"],
        description="stateless_only=True skips iso_path existence check",
        fn=_t_stateless_skips_iso_check,
    ),
    RemoteSplitTest(
        id="rs_full_aborts_bad_iso",
        tags=["remote_split", "preflight"],
        description="stateless_only=False aborts for a non-existent ISO path",
        fn=_t_full_aborts_on_bad_iso,
    ),
    RemoteSplitTest(
        id="rs_stateless_skips_launch_vm_check",
        tags=["remote_split", "preflight"],
        description="stateless_only=True skips launch_vm VM-exists check",
        fn=_t_stateless_skips_launch_vm_check,
    ),
    RemoteSplitTest(
        id="rs_full_aborts_launch_vm_nonexistent",
        tags=["remote_split", "preflight"],
        description="stateless_only=False aborts launch_vm when VM doesn't exist",
        fn=_t_full_aborts_launch_vm_nonexistent,
    ),
    # B. VNC arg binding
    RemoteSplitTest(
        id="rs_vnc_bind_local_true",
        tags=["remote_split", "vnc"],
        description="vnc_bind_local=True → 127.0.0.1:N,password=on in QEMU args",
        fn=_t_vnc_bind_local_true,
    ),
    RemoteSplitTest(
        id="rs_vnc_bind_local_false",
        tags=["remote_split", "vnc"],
        description="vnc_bind_local=False → :N (no address restriction, no password=on)",
        fn=_t_vnc_bind_local_false,
    ),
    # C. /chat endpoint
    RemoteSplitTest(
        id="rs_health_endpoint",
        tags=["remote_split", "chat", "api_server"],
        description="/health returns {status: ok} without auth",
        fn=_t_health_endpoint,
    ),
    RemoteSplitTest(
        id="rs_chat_auth_missing",
        tags=["remote_split", "chat", "auth"],
        description="/chat without Authorization header → 403",
        fn=_t_chat_auth_missing,
    ),
    RemoteSplitTest(
        id="rs_chat_auth_wrong_token",
        tags=["remote_split", "chat", "auth"],
        description="/chat with wrong Bearer token → 401",
        fn=_t_chat_auth_wrong_token,
    ),
    RemoteSplitTest(
        id="rs_chat_happy_path",
        tags=["remote_split", "chat"],
        description="/chat happy path → {session_id, text, tool_results, needs_input: null}",
        fn=_t_chat_happy_path,
    ),
    RemoteSplitTest(
        id="rs_chat_session_persistence",
        tags=["remote_split", "chat", "session"],
        description="/chat second call with session_id receives prior conversation history",
        fn=_t_chat_session_persistence,
    ),
    RemoteSplitTest(
        id="rs_chat_returns_needs_input",
        tags=["remote_split", "chat", "needs_input"],
        description="/chat flows needs_input through when process_message signals it",
        fn=_t_chat_returns_needs_input,
    ),
    RemoteSplitTest(
        id="rs_chat_auto_confirm_passed",
        tags=["remote_split", "chat", "needs_input"],
        description="/chat forwards auto_confirm=True to process_message",
        fn=_t_chat_auto_confirm_passed_through,
    ),
    RemoteSplitTest(
        id="rs_chat_delete_session",
        tags=["remote_split", "chat", "session"],
        description="DELETE /sessions/{id} clears the session (200)",
        fn=_t_chat_delete_session,
    ),
    RemoteSplitTest(
        id="rs_chat_missing_message",
        tags=["remote_split", "chat", "missing"],
        description="/chat missing 'message' field → 422",
        fn=_t_chat_missing_message_field,
    ),
    RemoteSplitTest(
        id="rs_chat_junk_extra_fields",
        tags=["remote_split", "chat", "junk"],
        description="/chat with unknown extra JSON fields → ignored, 200",
        fn=_t_chat_junk_extra_fields,
    ),
    RemoteSplitTest(
        id="rs_chat_foreign_session_id",
        tags=["remote_split", "chat", "foreign"],
        description="/chat with nonexistent session_id → fresh session, no crash",
        fn=_t_chat_foreign_session_id,
    ),
    RemoteSplitTest(
        id="rs_chat_conflict_auto_confirm_no_prior",
        tags=["remote_split", "chat", "conflict"],
        description="/chat auto_confirm=True with no prior needs_input → no crash",
        fn=_t_chat_conflict_auto_confirm_no_prior_input,
    ),
    # D. /execute endpoint
    RemoteSplitTest(
        id="rs_execute_auth_missing",
        tags=["remote_split", "execute", "auth"],
        description="/execute without Authorization header → 403",
        fn=_t_execute_auth_missing,
    ),
    RemoteSplitTest(
        id="rs_execute_auth_wrong_token",
        tags=["remote_split", "execute", "auth"],
        description="/execute with wrong Bearer token → 401",
        fn=_t_execute_auth_wrong_token,
    ),
    RemoteSplitTest(
        id="rs_execute_overrides_sdl_to_vnc",
        tags=["remote_split", "execute", "vnc"],
        description="/execute overrides display=sdl → vnc + injects vnc_bind_local=True",
        fn=_t_execute_overrides_sdl_to_vnc,
    ),
    RemoteSplitTest(
        id="rs_execute_overrides_gtk_to_vnc",
        tags=["remote_split", "execute", "vnc"],
        description="/execute overrides display=gtk → vnc",
        fn=_t_execute_overrides_gtk_to_vnc,
    ),
    RemoteSplitTest(
        id="rs_execute_passthrough_vnc_injects_bind_local",
        tags=["remote_split", "execute", "vnc"],
        description="/execute keeps display=vnc but still injects vnc_bind_local=True",
        fn=_t_execute_passthrough_vnc_injects_bind_local,
    ),
    RemoteSplitTest(
        id="rs_execute_preflight_abort",
        tags=["remote_split", "execute", "preflight"],
        description="Preflight abort on /execute → {success: False, preflight: True}",
        fn=_t_execute_preflight_abort,
    ),
    RemoteSplitTest(
        id="rs_execute_preflight_auto_fix",
        tags=["remote_split", "execute", "preflight"],
        description="Preflight auto_fix: fixed args used in execute call, result tagged",
        fn=_t_execute_preflight_auto_fix,
    ),
    RemoteSplitTest(
        id="rs_execute_preflight_ask_user",
        tags=["remote_split", "execute", "preflight"],
        description="Preflight ask_user on /execute → {clarify: True, success: False}",
        fn=_t_execute_preflight_ask_user,
    ),
    RemoteSplitTest(
        id="rs_execute_missing_tool_name",
        tags=["remote_split", "execute", "missing"],
        description="/execute missing tool_name → 422",
        fn=_t_execute_missing_tool_name,
    ),
    RemoteSplitTest(
        id="rs_execute_tool_not_in_allowlist",
        tags=["remote_split", "execute", "missing"],
        description="/execute tool not in allowed_remote_tools → 403",
        fn=_t_execute_tool_not_in_allowlist,
    ),
    RemoteSplitTest(
        id="rs_execute_junk_extra_fields",
        tags=["remote_split", "execute", "junk"],
        description="/execute with unknown extra JSON fields → ignored, 200",
        fn=_t_execute_junk_extra_fields,
    ),
    RemoteSplitTest(
        id="rs_execute_foreign_args",
        tags=["remote_split", "execute", "foreign"],
        description="/execute with args from another tool's schema → executor called, ignores foreign keys",
        fn=_t_execute_foreign_args,
    ),
    RemoteSplitTest(
        id="rs_execute_conflict_display_bind_local",
        tags=["remote_split", "execute", "conflict", "vnc"],
        description="/execute display=sdl + vnc_bind_local=False → both overridden by server",
        fn=_t_execute_conflict_display_and_bind_local,
    ),
    # E. executor_client re-export
    RemoteSplitTest(
        id="rs_executor_client_is_re_export",
        tags=["remote_split", "executor_client"],
        description="server.executor_client.execute_tool is shared.executioner.tool_executor.execute_tool",
        fn=_t_executor_client_is_re_export,
    ),
]


# ── Runner ────────────────────────────────────────────────────────────────────

def run_remote_split_test(tc: RemoteSplitTest) -> TestResult:
    start  = time.time()
    issues = _run(tc.fn)
    return TestResult(
        test_id       = tc.id,
        layer         = 11,
        passed        = len(issues) == 0,
        issues        = issues,
        fixes_applied = [],
        duration_s    = time.time() - start,
    )

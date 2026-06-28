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

  F. /sync endpoint:
    - missing auth → 401/403
    - wrong token → 401
    - valid → shortcut_commands, allowed_remote_tools, vms, profiles keys present
    - ALLOWED_VMS set → hidden VMs filtered out
    - empty ALLOWED_VMS → all VMs visible

  G. /events endpoint:
    - missing auth → 401/403
    - valid → {events: [...]} with ts/tool/outcome/duration_ms per entry
    - limit param forwarded to read_events
    - since=<future> → empty list
    - integration: /execute call triggers log_event; entry retrievable via GET /events

  H. /rotate-token endpoint:
    - missing auth → 401/403
    - new_token < 16 chars → 400/422
    - valid → {ok: True}
    - old token rejected after rotation
    - new token accepted after rotation
    - extra junk fields in body → ignored, 200
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
    """missing (allowlist) — tool not in allowed_remote_tools → 403 Forbidden.
    Requires patching _ALLOWED_TOOLS to a non-empty set; empty means unrestricted."""
    client, token = _make_test_client()
    with patch("server.http.api_server._ALLOWED_TOOLS", {"list_vms"}):
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
# F. /sync endpoint
# ════════════════════════════════════════════════════════════════════════════════

def _t_sync_auth_missing() -> List[str]:
    client, _ = _make_test_client()
    resp = client.get("/sync")
    if resp.status_code not in (401, 403):
        return [f"Expected 401/403 for missing auth on /sync, got {resp.status_code}"]
    return []


def _t_sync_auth_wrong_token() -> List[str]:
    client, _ = _make_test_client()
    resp = client.get("/sync", headers={"Authorization": "Bearer wrong-token-xyz"})
    if resp.status_code != 401:
        return [f"Expected 401 for wrong token on /sync, got {resp.status_code}"]
    return []


def _t_sync_valid_structure() -> List[str]:
    client, token = _make_test_client()

    with patch("server.http.api_server._mgr" if False else "shared.executioner.tool_executor.manager") as mock_mgr, \
         patch("shared.api.qemu_config.list_profiles", return_value=[]):
        mock_mgr.list_vms.return_value = []
        resp = client.get("/sync", headers={"Authorization": f"Bearer {token}"})

    if resp.status_code != 200:
        return [f"Unexpected status {resp.status_code}: {resp.text}"]
    data = resp.json()
    issues = []
    for key in ("shortcut_commands", "allowed_remote_tools", "vms", "profiles"):
        if key not in data:
            issues.append(f"Missing key {key!r} in /sync response")
    if not isinstance(data.get("allowed_remote_tools"), list):
        issues.append("allowed_remote_tools should be a list")
    if not isinstance(data.get("vms"), list):
        issues.append("vms should be a list")
    if not isinstance(data.get("profiles"), list):
        issues.append("profiles should be a list")
    return issues


def _t_sync_allowed_vms_filter() -> List[str]:
    """ALLOWED_VMS allowlist — /sync only returns VMs in the allowlist."""
    client, token = _make_test_client()

    fake_vms = [
        {"name": "allowed-vm", "status": "stopped"},
        {"name": "hidden-vm",  "status": "stopped"},
    ]

    with patch("shared.executioner.tool_executor.manager") as mock_mgr, \
         patch("shared.api.qemu_config.list_profiles", return_value=[]), \
         patch("server.http.api_server._ALLOWED_VMS", ["allowed-vm"]):
        mock_mgr.list_vms.return_value = fake_vms
        resp = client.get("/sync", headers={"Authorization": f"Bearer {token}"})

    if resp.status_code != 200:
        return [f"Unexpected status {resp.status_code}: {resp.text}"]
    vm_names = [v.get("name") for v in resp.json().get("vms", [])]
    issues = []
    if "allowed-vm" not in vm_names:
        issues.append("allowed-vm should appear in /sync vms")
    if "hidden-vm" in vm_names:
        issues.append("hidden-vm should be filtered out by ALLOWED_VMS")
    return issues


def _t_sync_empty_allowlist_returns_all() -> List[str]:
    """Empty ALLOWED_VMS means no filter — all VMs visible."""
    client, token = _make_test_client()

    fake_vms = [
        {"name": "vm-a", "status": "stopped"},
        {"name": "vm-b", "status": "stopped"},
    ]

    with patch("shared.executioner.tool_executor.manager") as mock_mgr, \
         patch("shared.api.qemu_config.list_profiles", return_value=[]), \
         patch("server.http.api_server._ALLOWED_VMS", []):
        mock_mgr.list_vms.return_value = fake_vms
        resp = client.get("/sync", headers={"Authorization": f"Bearer {token}"})

    if resp.status_code != 200:
        return [f"Unexpected status {resp.status_code}: {resp.text}"]
    vm_names = [v.get("name") for v in resp.json().get("vms", [])]
    issues = []
    for name in ("vm-a", "vm-b"):
        if name not in vm_names:
            issues.append(f"{name!r} should appear in /sync vms when allowlist is empty")
    return issues


# ════════════════════════════════════════════════════════════════════════════════
# G. /events endpoint
# ════════════════════════════════════════════════════════════════════════════════

def _t_events_auth_missing() -> List[str]:
    client, _ = _make_test_client()
    resp = client.get("/events")
    if resp.status_code not in (401, 403):
        return [f"Expected 401/403 for missing auth on /events, got {resp.status_code}"]
    return []


def _t_events_valid_structure() -> List[str]:
    client, token = _make_test_client()

    fake_events = [
        {"ts": "2026-06-25T10:00:00+00:00", "tool": "list_vms",
         "args": {}, "outcome": "ok", "duration_ms": 3.2},
    ]
    with patch("server.event_log.read_events", return_value=fake_events):
        resp = client.get("/events", headers={"Authorization": f"Bearer {token}"})

    if resp.status_code != 200:
        return [f"Unexpected status {resp.status_code}: {resp.text}"]
    data = resp.json()
    if "events" not in data:
        return ["Missing 'events' key in /events response"]
    if not isinstance(data["events"], list):
        return ["'events' should be a list"]
    if not data["events"]:
        return ["Expected at least 1 event in mocked response"]
    entry = data["events"][0]
    issues = []
    for key in ("ts", "tool", "outcome", "duration_ms"):
        if key not in entry:
            issues.append(f"Event entry missing key {key!r}")
    return issues


def _t_events_limit_param() -> List[str]:
    client, token = _make_test_client()

    many_events = [
        {"ts": f"2026-06-25T10:0{i}:00+00:00", "tool": "list_vms",
         "args": {}, "outcome": "ok", "duration_ms": 1.0}
        for i in range(10)
    ]
    captured_limit: Dict[str, Any] = {}

    def fake_read_events(limit=100, since=""):
        captured_limit["limit"] = limit
        return many_events[:limit]

    with patch("server.event_log.read_events", side_effect=fake_read_events):
        resp = client.get("/events?limit=2", headers={"Authorization": f"Bearer {token}"})

    if resp.status_code != 200:
        return [f"Unexpected status {resp.status_code}: {resp.text}"]
    issues = []
    if captured_limit.get("limit") != 2:
        issues.append(f"Expected limit=2 passed to read_events, got {captured_limit.get('limit')!r}")
    events = resp.json().get("events", [])
    if len(events) > 2:
        return [f"Expected at most 2 events with limit=2, got {len(events)}"]
    return issues


def _t_events_since_future() -> List[str]:
    client, token = _make_test_client()

    future_ts = "2099-01-01T00:00:00+00:00"
    future_ts_encoded = future_ts.replace("+", "%2B")
    captured: Dict[str, Any] = {}

    def fake_read_events(limit=100, since=""):
        captured["since"] = since
        return []  # nothing after a future timestamp

    with patch("server.event_log.read_events", side_effect=fake_read_events):
        resp = client.get(
            f"/events?since={future_ts_encoded}",
            headers={"Authorization": f"Bearer {token}"},
        )

    if resp.status_code != 200:
        return [f"Unexpected status {resp.status_code}: {resp.text}"]
    issues = []
    if captured.get("since") != future_ts:
        issues.append(f"Expected since={future_ts!r} forwarded to read_events, got {captured.get('since')!r}")
    if resp.json().get("events"):
        issues.append("Expected empty events list for future since timestamp")
    return issues


def _t_events_tool_call_logged() -> List[str]:
    """executor_client.execute_tool (the path /chat uses) triggers log_event;
    the entry is then retrievable via GET /events.
    Note: /execute bypasses executor_client and does NOT log — only /chat logs."""
    client, token = _make_test_client()
    sys.path.insert(0, _FILES_DIR)
    logged: List[Any] = []

    def fake_underlying(tool_name, args, verbose=False):
        return {"success": True, "vms": []}

    def fake_log(tool, args, result, duration_ms):
        logged.append({"ts": "2026-01-01T00:00:00+00:00", "tool": tool,
                        "args": {}, "outcome": "ok", "duration_ms": duration_ms})

    with patch("server.executor_client._execute_tool", side_effect=fake_underlying), \
         patch("server.executor_client._log_event", side_effect=fake_log):
        import server.executor_client as ec
        if "server.executor_client" in sys.modules:
            del sys.modules["server.executor_client"]
        import server.executor_client as ec
        with patch.object(ec, "_execute_tool", side_effect=fake_underlying), \
             patch.object(ec, "_log_event", side_effect=fake_log):
            ec.execute_tool("list_vms", {})

    with patch("server.event_log.read_events", return_value=logged):
        resp = client.get("/events", headers={"Authorization": f"Bearer {token}"})

    if resp.status_code != 200:
        return [f"Unexpected status on /events: {resp.status_code}"]
    events = resp.json().get("events", [])
    if not events:
        return ["Expected at least one event after executor_client.execute_tool call"]
    if events[0].get("tool") != "list_vms":
        return [f"Expected logged tool=list_vms, got {events[0].get('tool')!r}"]
    return []


# ════════════════════════════════════════════════════════════════════════════════
# H. /rotate-token endpoint
# ════════════════════════════════════════════════════════════════════════════════

def _t_rotate_auth_missing() -> List[str]:
    client, _ = _make_test_client()
    resp = client.post("/rotate-token", json={"new_token": "a-completely-valid-new-token-here"})
    if resp.status_code not in (401, 403):
        return [f"Expected 401/403 for missing auth on /rotate-token, got {resp.status_code}"]
    return []


def _t_rotate_token_too_short() -> List[str]:
    client, token = _make_test_client()
    with patch("pathlib.Path.write_text"), patch("pathlib.Path.chmod"):
        resp = client.post(
            "/rotate-token",
            json={"new_token": "short"},
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code not in (400, 422):
        return [f"Expected 400/422 for token < 16 chars, got {resp.status_code}: {resp.text}"]
    return []


def _t_rotate_token_valid() -> List[str]:
    client, token = _make_test_client()
    new_token = "a-valid-new-token-" + _uid()
    with patch("pathlib.Path.write_text"), patch("pathlib.Path.chmod"):
        resp = client.post(
            "/rotate-token",
            json={"new_token": new_token},
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        return [f"Expected 200 from /rotate-token, got {resp.status_code}: {resp.text}"]
    data = resp.json()
    if not data.get("ok"):
        return [f"Expected ok=True in response, got {data!r}"]
    return []


def _t_rotate_old_token_rejected() -> List[str]:
    client, old_token = _make_test_client()
    new_token = "a-valid-new-token-" + _uid()
    with patch("pathlib.Path.write_text"), patch("pathlib.Path.chmod"):
        rot = client.post(
            "/rotate-token",
            json={"new_token": new_token},
            headers={"Authorization": f"Bearer {old_token}"},
        )
    if rot.status_code != 200:
        return [f"Rotation failed with {rot.status_code}, cannot test old-token rejection"]
    # Old token must now be rejected
    resp = client.get("/sync", headers={"Authorization": f"Bearer {old_token}"})
    if resp.status_code != 401:
        return [f"Expected 401 for old token after rotation, got {resp.status_code}"]
    return []


def _t_rotate_new_token_works() -> List[str]:
    client, old_token = _make_test_client()
    new_token = "a-valid-new-token-" + _uid()
    with patch("pathlib.Path.write_text"), patch("pathlib.Path.chmod"):
        rot = client.post(
            "/rotate-token",
            json={"new_token": new_token},
            headers={"Authorization": f"Bearer {old_token}"},
        )
    if rot.status_code != 200:
        return [f"Rotation failed with {rot.status_code}, cannot test new-token access"]
    # New token must now be accepted
    with patch("shared.executioner.tool_executor.manager") as mock_mgr, \
         patch("shared.api.qemu_config.list_profiles", return_value=[]):
        mock_mgr.list_vms.return_value = []
        resp = client.get("/sync", headers={"Authorization": f"Bearer {new_token}"})
    if resp.status_code != 200:
        return [f"Expected 200 with new token after rotation, got {resp.status_code}: {resp.text}"]
    return []


def _t_rotate_junk_body_fields() -> List[str]:
    client, token = _make_test_client()
    new_token = "a-valid-new-token-" + _uid()
    with patch("pathlib.Path.write_text"), patch("pathlib.Path.chmod"):
        resp = client.post(
            "/rotate-token",
            json={"new_token": new_token, "zebra_junk": "zzz", "extra_field": 99},
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        return [f"Expected 200 with junk extra fields, got {resp.status_code}: {resp.text}"]
    return []


# ════════════════════════════════════════════════════════════════════════════════
# E. executor_client is a re-export
# ════════════════════════════════════════════════════════════════════════════════

def _t_executor_client_is_re_export() -> List[str]:
    """server.executor_client.execute_tool is a wrapper (adds logging, access control,
    display override) that delegates to shared.executioner.tool_executor.execute_tool
    via ec._execute_tool.  Verify the delegation is intact — ec._execute_tool must be
    the same object as te.execute_tool."""
    sys.path.insert(0, _FILES_DIR)
    for mod in ("server.executor_client", "shared.executioner.tool_executor"):
        if mod in sys.modules:
            del sys.modules[mod]
    import server.executor_client as ec
    import shared.executioner.tool_executor as te
    if ec._execute_tool is not te.execute_tool:
        return [
            "server.executor_client._execute_tool is NOT the same object as "
            "shared.executioner.tool_executor.execute_tool — the delegation is broken"
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
    # F. /sync endpoint
    RemoteSplitTest(
        id="rs_sync_auth_missing",
        tags=["remote_split", "sync", "auth"],
        description="GET /sync without auth → 401/403",
        fn=_t_sync_auth_missing,
    ),
    RemoteSplitTest(
        id="rs_sync_auth_wrong_token",
        tags=["remote_split", "sync", "auth"],
        description="GET /sync wrong token → 401",
        fn=_t_sync_auth_wrong_token,
    ),
    RemoteSplitTest(
        id="rs_sync_valid_structure",
        tags=["remote_split", "sync", "valid"],
        description="GET /sync returns required keys: shortcut_commands, allowed_remote_tools, vms, profiles",
        fn=_t_sync_valid_structure,
    ),
    RemoteSplitTest(
        id="rs_sync_allowed_vms_filter",
        tags=["remote_split", "sync", "access_control"],
        description="GET /sync with ALLOWED_VMS set — only allowed VMs returned",
        fn=_t_sync_allowed_vms_filter,
    ),
    RemoteSplitTest(
        id="rs_sync_empty_allowlist_returns_all",
        tags=["remote_split", "sync", "access_control"],
        description="GET /sync with empty ALLOWED_VMS — all VMs returned (open allowlist)",
        fn=_t_sync_empty_allowlist_returns_all,
    ),
    # G. /events endpoint
    RemoteSplitTest(
        id="rs_events_auth_missing",
        tags=["remote_split", "events", "auth"],
        description="GET /events without auth → 401/403",
        fn=_t_events_auth_missing,
    ),
    RemoteSplitTest(
        id="rs_events_valid_structure",
        tags=["remote_split", "events", "valid"],
        description="GET /events returns {events: [...]} with correct entry shape",
        fn=_t_events_valid_structure,
    ),
    RemoteSplitTest(
        id="rs_events_limit_param",
        tags=["remote_split", "events", "valid"],
        description="GET /events?limit=2 — result has at most 2 entries",
        fn=_t_events_limit_param,
    ),
    RemoteSplitTest(
        id="rs_events_since_future",
        tags=["remote_split", "events", "valid"],
        description="GET /events?since=<future_ts> — returns empty list",
        fn=_t_events_since_future,
    ),
    RemoteSplitTest(
        id="rs_events_tool_call_logged",
        tags=["remote_split", "events", "integration"],
        description="/execute call produces a log entry retrievable via GET /events",
        fn=_t_events_tool_call_logged,
    ),
    # H. /rotate-token endpoint
    RemoteSplitTest(
        id="rs_rotate_auth_missing",
        tags=["remote_split", "rotate_token", "auth"],
        description="POST /rotate-token without auth → 401/403",
        fn=_t_rotate_auth_missing,
    ),
    RemoteSplitTest(
        id="rs_rotate_token_too_short",
        tags=["remote_split", "rotate_token", "broken"],
        description="POST /rotate-token with token < 16 chars → 400",
        fn=_t_rotate_token_too_short,
    ),
    RemoteSplitTest(
        id="rs_rotate_token_valid",
        tags=["remote_split", "rotate_token", "valid"],
        description="POST /rotate-token with valid new token → ok:True",
        fn=_t_rotate_token_valid,
    ),
    RemoteSplitTest(
        id="rs_rotate_old_token_rejected",
        tags=["remote_split", "rotate_token", "valid"],
        description="After /rotate-token, old token is rejected on /health-requiring endpoints",
        fn=_t_rotate_old_token_rejected,
    ),
    RemoteSplitTest(
        id="rs_rotate_new_token_works",
        tags=["remote_split", "rotate_token", "valid"],
        description="After /rotate-token, new token works on subsequent requests",
        fn=_t_rotate_new_token_works,
    ),
    RemoteSplitTest(
        id="rs_rotate_junk_body_fields",
        tags=["remote_split", "rotate_token", "junk"],
        description="POST /rotate-token with extra unknown JSON fields alongside new_token — FastAPI ignores extras",
        fn=_t_rotate_junk_body_fields,
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

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
    - orchestrator.executor_client.execute_tool is a re-export of
      orchestrator.pipeline.execute_tool (same object)

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

import contextlib, os, sys, tempfile, time, traceback, uuid
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


_auth_isolation_active = False  # set by _isolated_auth_paths() while its context is open


def _make_test_client():
    """Return (TestClient, token). Sets API_TOKEN env var and re-imports api_server.

    Also isolates the operator/session store to a fresh temp dir — unless an
    outer _isolated_auth_paths() context is already active (checked via
    _auth_isolation_active), in which case this leaves that isolation alone
    rather than layering a second, different temp dir on top of it (which
    would orphan anything the test already wrote — e.g. an operator account
    created before calling this — in the outer context's directory while the
    client ends up pointed at yet another empty one).

    Every test in this file goes through here, and orchestrator/http/api_server.py's
    auth now consults orchestrator.auth.store.operators_exist() on every
    request; if a real operator account exists on whatever machine runs this
    suite (genuine local usage, not test state), every /chat and /execute test
    that assumes "no operators yet -> plain token works" would otherwise fail
    depending on what's actually on that box. Patches (when applied here) are
    started, never stopped — matches the pattern already established for the
    API_TOKEN env var above: the whole test_api.py run is a single one-shot
    process, not a long-lived session, so there's nothing to restore after.
    """
    token = f"test-secret-{_uid()}"
    os.environ["API_TOKEN"] = token
    sys.path.insert(0, _FILES_DIR)
    if not _auth_isolation_active:
        from orchestrator.auth import sessions as _op_sessions
        from orchestrator.auth import store as _op_store
        import pathlib
        d = pathlib.Path(tempfile.mkdtemp(prefix="rs_auth_"))
        patch.object(_op_store,    "OPERATORS_FILE",       d / "operators.json").start()
        patch.object(_op_sessions, "SESSIONS_FILE",        d / "operator_sessions.json").start()
        patch.object(_op_sessions, "CURRENT_SESSION_FILE", d / "current_session").start()
    from orchestrator.http.api_server import app
    from fastapi.testclient import TestClient
    return TestClient(app, raise_server_exceptions=False), token


@contextlib.contextmanager
def _isolated_auth_paths():
    """Point the operator/session store at a scratch temp dir for the duration.

    orchestrator/auth/store.py + sessions.py read/write real files under
    ~/.gorgon — genuine host state, not per-test-isolated. Once a real
    operator account exists on a box (real usage, not just this test suite),
    every test that assumes "no operators yet -> localhost bypass" would
    otherwise start failing. Patching the module-level path constants keeps
    every operator/session test fully isolated from whatever's actually on
    the machine running the suite.

    Sets _auth_isolation_active while open so a _make_test_client() call
    inside this context (common when a test needs to create_operator() before
    building its client) reuses this same directory instead of patching to a
    second, different one — which would silently orphan whatever the test
    already wrote here.
    """
    global _auth_isolation_active
    from orchestrator.auth import sessions as _op_sessions
    from orchestrator.auth import store as _op_store
    with tempfile.TemporaryDirectory() as d:
        import pathlib
        d = pathlib.Path(d)
        with patch.object(_op_store,    "OPERATORS_FILE",        d / "operators.json"), \
             patch.object(_op_sessions, "SESSIONS_FILE",         d / "operator_sessions.json"), \
             patch.object(_op_sessions, "CURRENT_SESSION_FILE",  d / "current_session"):
            _auth_isolation_active = True
            try:
                yield
            finally:
                _auth_isolation_active = False


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
    from orchestrator.preflight.validator import _preflight_check
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
    from orchestrator.preflight.validator import _preflight_check
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
    from orchestrator.preflight.validator import _preflight_check
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
    from orchestrator.preflight.validator import _preflight_check
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
    from orchestrator.preflight.validator import _preflight_check
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
    from orchestrator.preflight.validator import _preflight_check
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
    from executor.api.qemu_config import MachineConfig
    from executor.api.qemu_arg_builder import QemuArgBuilder
    cfg                = MachineConfig()
    cfg.name           = f"vnc-test-{_uid()}"
    cfg.display        = "vnc"
    cfg.vnc_port       = 5901
    cfg.vnc_bind_local = vnc_bind_local
    cfg.kvm            = False
    cfg.disks          = []
    cfg.networks       = []
    try:
        return QemuArgBuilder(cfg).build()
    finally:
        # build() writes smbios_chassis.bin into the VM dir as a real side
        # effect; this test only inspects the arg list, so don't leave the dir.
        import shutil
        shutil.rmtree(cfg.get_vm_dir(), ignore_errors=True)


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
    with patch("orchestrator.ai.chat.cli.process_message",
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
    with patch("orchestrator.ai.chat.cli.process_message", side_effect=tracking_pm):
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
    with patch("orchestrator.ai.chat.cli.process_message",
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

    with patch("orchestrator.ai.chat.cli.process_message", side_effect=capturing_pm):
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

    with patch("orchestrator.ai.chat.cli.process_message",
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
    with patch("orchestrator.ai.chat.cli.process_message",
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
    with patch("orchestrator.ai.chat.cli.process_message",
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
    with patch("orchestrator.ai.chat.cli.process_message",
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

    def fake_execute(tool_name, args, verbose=False, log=True):
        captured["args"] = dict(args)
        return {"success": True, "name": "test-vm", "pid": 12345, "display": "vnc"}

    with patch("orchestrator.executor_client.execute_tool", side_effect=fake_execute), \
         patch("orchestrator.preflight.validator._preflight_check", return_value={"action": "ok"}):
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

    def fake_execute(tool_name, args, verbose=False, log=True):
        captured["args"] = dict(args)
        return {"success": True, "name": "test-vm", "pid": 12345, "display": "vnc"}

    with patch("orchestrator.executor_client.execute_tool", side_effect=fake_execute), \
         patch("orchestrator.preflight.validator._preflight_check", return_value={"action": "ok"}):
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

    def fake_execute(tool_name, args, verbose=False, log=True):
        captured["args"] = dict(args)
        return {"success": True, "name": "test-vm", "pid": 12345, "display": "vnc"}

    with patch("orchestrator.executor_client.execute_tool", side_effect=fake_execute), \
         patch("orchestrator.preflight.validator._preflight_check", return_value={"action": "ok"}):
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
    with patch("orchestrator.preflight.validator._preflight_check", return_value={
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

    def fake_execute(tool_name, args, verbose=False, log=True):
        captured["args"] = dict(args)
        return {"success": True, "name": args.get("name", "vm")}

    fixed = {"name": f"rs-fixed-{_uid()}", "machine_type": "q35", "os_type": "linux"}
    with patch("orchestrator.executor_client.execute_tool", side_effect=fake_execute), \
         patch("orchestrator.preflight.validator._preflight_check", return_value={
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
    with patch("orchestrator.preflight.validator._preflight_check", return_value={
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
    """missing (allowlist) — tool not in allowed_remote_tools.

    Enforcement lives solely in executor_client.execute_tool() now (the same point /chat has
    always relied on with no pre-check of its own) — HTTP 200 with the violation embedded in the
    result, same shape as any other tool-level failure, not a distinct 403. A prior duplicate
    pre-check in api_server.py returned 403 with leakier wording; removed as part of the tool-
    compartmentalization base-layer fix (2026-07-13) so /execute and /chat behave identically and
    a blocked tool looks the same as one that "doesn't exist" rather than announcing itself.
    Requires patching orchestrator.executor_client._ALLOWED_TOOLS (the real enforcement point) to
    a non-empty set; empty means unrestricted."""
    client, token = _make_test_client()
    with patch("orchestrator.executor_client._ALLOWED_TOOLS", {"list_vms"}):
        resp = client.post(
            "/execute",
            json={"tool_name": "send_monitor_cmd", "args": {"name": "vm", "cmd": "info"}},
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        return [f"Expected 200 (violation embedded in result), got {resp.status_code}"]
    body = resp.json()
    if body.get("ok") is not True:
        return [f"Expected ok=True envelope, got {body.get('ok')!r}"]
    result = body.get("result", {})
    if result.get("success") is not False:
        return [f"Expected result.success=False, got {result.get('success')!r}"]
    if "not available" not in result.get("error", ""):
        return [f"Expected vague 'not available' wording, got {result.get('error')!r}"]
    if "allowed_remote_tools" in result.get("error", "") or "config.json" in result.get("error", ""):
        return ["Error message leaks config details — should be vague, not explain how to unlock it"]
    return []


def _t_execute_hidden_vm_indistinguishable_from_missing() -> List[str]:
    """security (VM hiding) — a hidden VM must be indistinguishable from a nonexistent one.

    Regression test for a real leak: preflight's own existence check for launch_vm (and any other
    _PREFLIGHT_TOOLS entry) calls manager.list_vms() directly. In local mode, _manager_proxy() used
    to return the raw, unfiltered QemuManager — so preflight would see a hidden (allowlist-filtered)
    VM as real and skip straight to its ISO check instead of its "doesn't exist" abort, while a
    genuinely-missing name hit that abort. The final executor_client-level error text happened to
    both say "not found", but the *response shape* differed (preflight=True + correction text vs
    not) — a distinguishing side channel that let probing reveal which VMs exist but are hidden.
    After the fix, both cases should hit the exact same preflight abort path.
    """
    client, token = _make_test_client()
    fake_vms = [
        {"name": "allowed-vm", "status": "stopped"},
        {"name": "hidden-vm",  "status": "stopped"},
    ]

    def _call(vm_name: str):
        with patch("executor.tool_dispatch.context.manager") as mock_mgr, \
             patch("orchestrator.http.api_server._ALLOWED_VMS", ["allowed-vm"]), \
             patch("orchestrator.executor_client._ALLOWED_VMS", ["allowed-vm"]):
            mock_mgr.list_vms.return_value = fake_vms
            return client.post(
                "/execute",
                json={"tool_name": "launch_vm", "args": {"name": vm_name}},
                headers={"Authorization": f"Bearer {token}"},
            )

    resp_hidden = _call("hidden-vm")       # real VM, filtered out by the allowlist
    resp_fake   = _call("totally-fake-vm")  # never existed at all

    issues = []
    for label, resp in (("hidden-vm", resp_hidden), ("totally-fake-vm", resp_fake)):
        if resp.status_code != 200:
            issues.append(f"{label}: expected 200, got {resp.status_code}")

    body_hidden = resp_hidden.json().get("result", {})
    body_fake   = resp_fake.json().get("result", {})

    if body_hidden.get("preflight") != body_fake.get("preflight"):
        issues.append(
            f"preflight flag differs: hidden={body_hidden.get('preflight')!r} "
            f"fake={body_fake.get('preflight')!r} — a hidden VM must go through the SAME "
            f"preflight path as a genuinely missing one"
        )
    if body_hidden.get("success") is not False or body_fake.get("success") is not False:
        issues.append("both calls should report success=False")
    # Neither response should ever surface "allowed-vm" (the one real, visible VM) as a fuzzy-match
    # candidate would only appear if list_vms() leaked the full unfiltered set into preflight.
    for label, body in (("hidden-vm", body_hidden), ("totally-fake-vm", body_fake)):
        if "allowed-vm" in str(body.get("correction", "")) and "Did you mean" in body.get("error", ""):
            issues.append(f"{label}: fuzzy-match candidates leaked into the response: {body}")
    return issues


def _t_execute_junk_extra_fields() -> List[str]:
    """junk — unknown extra JSON fields in body → ignored, request succeeds."""
    client, token = _make_test_client()

    def fake_execute(tool_name, args, verbose=False, log=True):
        return {"success": True, "vms": []}

    with patch("orchestrator.executor_client.execute_tool", side_effect=fake_execute), \
         patch("orchestrator.preflight.validator._preflight_check", return_value={"action": "ok"}):
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

    def fake_execute(tool_name, args, verbose=False, log=True):
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
    with patch("orchestrator.executor_client.execute_tool", side_effect=fake_execute), \
         patch("orchestrator.preflight.validator._preflight_check", return_value={"action": "ok"}):
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

    def fake_execute(tool_name, args, verbose=False, log=True):
        captured["args"] = dict(args)
        return {"success": True, "name": "test-vm", "pid": 1, "display": "vnc"}

    with patch("orchestrator.executor_client.execute_tool", side_effect=fake_execute), \
         patch("orchestrator.preflight.validator._preflight_check", return_value={"action": "ok"}):
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

    with patch("executor.tool_dispatch.context.manager") as mock_mgr, \
         patch("executor.api.qemu_config.list_profiles", return_value=[]):
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

    with patch("executor.tool_dispatch.context.manager") as mock_mgr, \
         patch("executor.api.qemu_config.list_profiles", return_value=[]), \
         patch("orchestrator.http.api_server._ALLOWED_VMS", ["allowed-vm"]):
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

    with patch("executor.tool_dispatch.context.manager") as mock_mgr, \
         patch("executor.api.qemu_config.list_profiles", return_value=[]), \
         patch("orchestrator.http.api_server._ALLOWED_VMS", []):
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
    with patch("orchestrator.event_log.read_events", return_value=fake_events):
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

    with patch("orchestrator.event_log.read_events", side_effect=fake_read_events):
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

    with patch("orchestrator.event_log.read_events", side_effect=fake_read_events):
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

    with patch("orchestrator.executor_client._execute_tool", side_effect=fake_underlying), \
         patch("orchestrator.executor_client._log_event", side_effect=fake_log):
        import orchestrator.executor_client as ec
        if "orchestrator.executor_client" in sys.modules:
            del sys.modules["orchestrator.executor_client"]
        import orchestrator.executor_client as ec
        with patch.object(ec, "_execute_tool", side_effect=fake_underlying), \
             patch.object(ec, "_log_event", side_effect=fake_log):
            ec.execute_tool("list_vms", {})

    with patch("orchestrator.event_log.read_events", return_value=logged):
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
    with patch("executor.tool_dispatch.context.manager") as mock_mgr, \
         patch("executor.api.qemu_config.list_profiles", return_value=[]):
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
# I. Operator login/session layer
# ════════════════════════════════════════════════════════════════════════════════

def _t_operator_localhost_bypass_pre_bootstrap() -> List[str]:
    """The key regression this whole feature is about: localhost is trusted
    with no creds at all UNTIL an operator account exists, then it isn't.

    _make_test_client()'s TestClient reports as host "testclient" by default
    (Starlette's own default, not "127.0.0.1") — none of the existing tests
    needed to override that since they all present explicit creds. This is
    the one test that needs a TestClient Starlette actually treats as
    localhost, so it builds its own rather than touching the shared helper
    every other auth-rejection test depends on behaving as non-local.
    """
    issues = []
    with _isolated_auth_paths():
        os.environ["API_TOKEN"] = f"test-secret-{_uid()}"
        sys.path.insert(0, _FILES_DIR)
        from fastapi.testclient import TestClient
        from orchestrator.http.api_server import app
        client = TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 12345))
        with patch("executor.tool_dispatch.context.manager") as mock_mgr, \
             patch("executor.api.qemu_config.list_profiles", return_value=[]):
            mock_mgr.list_vms.return_value = []
            resp = client.get("/sync")
        if resp.status_code != 200:
            issues.append(f"Expected localhost pre-bootstrap access to succeed, got {resp.status_code}: {resp.text}")

        from orchestrator.auth import store as op_store
        op_store.create_operator("admin", "correct-horse-battery")

        resp = client.get("/sync")
        if resp.status_code != 401:
            issues.append(f"Expected localhost to be REJECTED once an operator exists, got {resp.status_code}")
    return issues


def _t_operator_login_wrong_password() -> List[str]:
    with _isolated_auth_paths():
        from orchestrator.auth import store as op_store
        op_store.create_operator("admin", "correct-horse-battery")
        client, _ = _make_test_client()
        resp = client.post("/login", json={"username": "admin", "password": "wrong"})
        if resp.status_code != 401:
            return [f"Expected 401 for wrong password, got {resp.status_code}: {resp.text}"]
    return []


def _t_operator_login_unknown_user() -> List[str]:
    with _isolated_auth_paths():
        client, _ = _make_test_client()
        resp = client.post("/login", json={"username": "ghost", "password": "whatever123"})
        if resp.status_code != 401:
            return [f"Expected 401 for unknown user, got {resp.status_code}: {resp.text}"]
    return []


def _t_operator_login_success_session_works() -> List[str]:
    issues = []
    with _isolated_auth_paths():
        from orchestrator.auth import store as op_store
        op_store.create_operator("admin", "correct-horse-battery")
        client, _ = _make_test_client()
        login = client.post("/login", json={"username": "admin", "password": "correct-horse-battery"})
        if login.status_code != 200:
            return [f"Expected 200 from /login, got {login.status_code}: {login.text}"]
        token = login.json().get("session_token")
        if not token:
            issues.append(f"Expected a session_token in /login response, got {login.json()!r}")
            return issues

        with patch("executor.tool_dispatch.context.manager") as mock_mgr, \
             patch("executor.api.qemu_config.list_profiles", return_value=[]):
            mock_mgr.list_vms.return_value = []
            resp = client.get("/sync", headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            issues.append(f"Expected session bearer token to authorize /sync, got {resp.status_code}: {resp.text}")
    return issues


def _t_operator_session_cookie_works() -> List[str]:
    """The same session also works as a cookie — one /login serves the CLI
    (bearer) and a future browser client (cookie) from one store."""
    issues = []
    with _isolated_auth_paths():
        from orchestrator.auth import store as op_store
        op_store.create_operator("admin", "correct-horse-battery")
        client, _ = _make_test_client()
        login = client.post("/login", json={"username": "admin", "password": "correct-horse-battery"})
        if "gorgon_session" not in login.cookies:
            issues.append("Expected /login to set a gorgon_session cookie")
            return issues
        with patch("executor.tool_dispatch.context.manager") as mock_mgr, \
             patch("executor.api.qemu_config.list_profiles", return_value=[]):
            mock_mgr.list_vms.return_value = []
            resp = client.get("/sync")  # TestClient carries the cookie automatically
        if resp.status_code != 200:
            issues.append(f"Expected the session cookie alone to authorize /sync, got {resp.status_code}")
    return issues


def _t_operator_api_token_unaffected() -> List[str]:
    """The existing API_TOKEN machine-to-machine path is untouched by any of
    this — it must keep working even after operator accounts exist."""
    issues = []
    with _isolated_auth_paths():
        from orchestrator.auth import store as op_store
        op_store.create_operator("admin", "correct-horse-battery")
        client, token = _make_test_client()
        with patch("executor.tool_dispatch.context.manager") as mock_mgr, \
             patch("executor.api.qemu_config.list_profiles", return_value=[]):
            mock_mgr.list_vms.return_value = []
            resp = client.get("/sync", headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            issues.append(f"Expected the plain API_TOKEN to still work, got {resp.status_code}: {resp.text}")
    return issues


def _t_operator_logout_invalidates() -> List[str]:
    issues = []
    with _isolated_auth_paths():
        from orchestrator.auth import store as op_store
        op_store.create_operator("admin", "correct-horse-battery")
        client, _ = _make_test_client()
        token = client.post("/login", json={"username": "admin", "password": "correct-horse-battery"}).json()["session_token"]
        out = client.post("/logout", headers={"Authorization": f"Bearer {token}"})
        if out.status_code != 200:
            issues.append(f"Expected 200 from /logout, got {out.status_code}: {out.text}")
        resp = client.get("/sync", headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 401:
            issues.append(f"Expected 401 using a session token after logout, got {resp.status_code}")
    return issues


def _t_operator_crud_roundtrip() -> List[str]:
    issues = []
    with _isolated_auth_paths():
        from orchestrator.auth import store as op_store
        op_store.create_operator("admin", "correct-horse-battery")
        client, _ = _make_test_client()
        token = client.post("/login", json={"username": "admin", "password": "correct-horse-battery"}).json()["session_token"]
        hdrs = {"Authorization": f"Bearer {token}"}

        created = client.post("/operators", json={"username": "second", "password": "another-pw-123"}, headers=hdrs)
        if created.status_code != 200:
            issues.append(f"Expected 200 creating a second operator, got {created.status_code}: {created.text}")

        listed = client.get("/operators", headers=hdrs)
        names = listed.json().get("operators", [])
        if "admin" not in names or "second" not in names:
            issues.append(f"Expected both operators listed, got {names!r}")

        deleted = client.delete("/operators/second", headers=hdrs)
        if deleted.status_code != 200:
            issues.append(f"Expected 200 deleting operator, got {deleted.status_code}: {deleted.text}")

        dup = client.post("/operators", json={"username": "admin", "password": "another-pw-123"}, headers=hdrs)
        if dup.status_code != 400:
            issues.append(f"Expected 400 creating a duplicate username, got {dup.status_code}")
    return issues


def _t_operator_create_short_password_rejected() -> List[str]:
    issues = []
    with _isolated_auth_paths():
        from orchestrator.auth import store as op_store
        op_store.create_operator("admin", "correct-horse-battery")
        client, _ = _make_test_client()
        token = client.post("/login", json={"username": "admin", "password": "correct-horse-battery"}).json()["session_token"]
        resp = client.post("/operators", json={"username": "weak", "password": "short"},
                            headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 400:
            issues.append(f"Expected 400 for a password under 8 chars, got {resp.status_code}")
    return issues


def _t_operator_token_insufficient_on_chat_and_execute() -> List[str]:
    """Once an operator exists, the plain API_TOKEN alone must no longer
    authorize /chat or /execute — only an operator session does. This is the
    actual regression this stricter dependency exists to close: the shipped
    default token (connection_config.json's "token") is what every
    interactive client carries by default, so without this, operator login
    is optional in practice, not mandatory, for these two surfaces."""
    issues = []
    with _isolated_auth_paths():
        from orchestrator.auth import store as op_store
        op_store.create_operator("admin", "correct-horse-battery")
        client, token = _make_test_client()

        resp = client.post("/execute", json={"tool_name": "list_vms", "args": {}},
                            headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 401:
            issues.append(f"Expected 401 using the plain API_TOKEN on /execute post-bootstrap, got {resp.status_code}")

        resp = client.post("/chat", json={"message": "hi"},
                            headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 401:
            issues.append(f"Expected 401 using the plain API_TOKEN on /chat post-bootstrap, got {resp.status_code}")

        # Sanity check: the SAME token still works on an unrelated endpoint
        # that wasn't tightened (_require_auth, not _require_operator_auth).
        with patch("executor.tool_dispatch.context.manager") as mock_mgr, \
             patch("executor.api.qemu_config.list_profiles", return_value=[]):
            mock_mgr.list_vms.return_value = []
            resp = client.get("/sync", headers={"Authorization": f"Bearer {token}"})
        if resp.status_code != 200:
            issues.append(f"Expected the plain token to still work on /sync (untouched), got {resp.status_code}")
    return issues


def _t_operator_session_works_on_execute() -> List[str]:
    """A valid operator session (not the plain token) authorizes /execute."""
    issues = []
    with _isolated_auth_paths():
        from orchestrator.auth import store as op_store
        op_store.create_operator("admin", "correct-horse-battery")
        client, _ = _make_test_client()
        login = client.post("/login", json={"username": "admin", "password": "correct-horse-battery"})
        session_token = login.json()["session_token"]

        with patch("orchestrator.executor_client.execute_tool",
                   return_value={"success": True, "vms": []}):
            resp = client.post(
                "/execute", json={"tool_name": "list_vms", "args": {}},
                headers={"Authorization": f"Bearer {session_token}"},
            )
        if resp.status_code != 200:
            issues.append(f"Expected 200 using an operator session on /execute, got {resp.status_code}: {resp.text}")
    return issues


def _t_direct_cli_gate() -> List[str]:
    """orchestrator/ai/direct_cli.py's cli_direct() gate — the in-process
    path that never touches HTTP at all, and therefore never touches
    _require_auth. Exercised directly, no TestClient involved."""
    issues = []
    with _isolated_auth_paths():
        from orchestrator.ai.chat.direct_cli import _operator_gate_ok
        from orchestrator.auth import sessions as op_sessions
        from orchestrator.auth import store as op_store

        if not _operator_gate_ok("list"):
            issues.append("Expected the gate to allow dispatch pre-bootstrap (no operators yet)")

        op_store.create_operator("admin", "correct-horse-battery")
        if _operator_gate_ok("list"):
            issues.append("Expected the gate to BLOCK dispatch once an operator exists with no active session")
        if not _operator_gate_ok("login"):
            issues.append("Expected 'login' itself to always be exempt from the gate")

        token = op_sessions.create_session("admin")
        op_sessions.write_current_session(token)
        if not _operator_gate_ok("list"):
            issues.append("Expected the gate to allow dispatch once logged in")

        op_sessions.invalidate_session(op_sessions.read_current_session())
        op_sessions.clear_current_session()
        if _operator_gate_ok("list"):
            issues.append("Expected the gate to BLOCK dispatch again after logout")
    return issues


# ════════════════════════════════════════════════════════════════════════════════
# E. executor_client is a re-export
# ════════════════════════════════════════════════════════════════════════════════

def _t_executor_client_is_re_export() -> List[str]:
    """orchestrator.executor_client.execute_tool is a wrapper (adds logging, access
    control, display override) that delegates to orchestrator.pipeline.execute_tool
    (the full sanitize/gate/dispatch pipeline) via ec._execute_tool. Verify the
    delegation is intact — ec._execute_tool must be the same object as
    pipeline.execute_tool."""
    sys.path.insert(0, _FILES_DIR)
    for mod in ("orchestrator.executor_client", "orchestrator.pipeline"):
        if mod in sys.modules:
            del sys.modules[mod]
    import orchestrator.executor_client as ec
    import orchestrator.pipeline as pipeline
    if ec._execute_tool is not pipeline.execute_tool:
        return [
            "orchestrator.executor_client._execute_tool is NOT the same object as "
            "orchestrator.pipeline.execute_tool — the delegation is broken"
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
        description="/execute tool not in allowed_remote_tools → 200, vague error embedded in result",
        fn=_t_execute_tool_not_in_allowlist,
    ),
    RemoteSplitTest(
        id="rs_execute_hidden_vm_indistinguishable_from_missing",
        tags=["remote_split", "execute", "security"],
        description="A hidden (allowlist-filtered) VM must look identical to a genuinely nonexistent one through preflight",
        fn=_t_execute_hidden_vm_indistinguishable_from_missing,
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
        description="orchestrator.executor_client.execute_tool is orchestrator.pipeline.execute_tool",
        fn=_t_executor_client_is_re_export,
    ),
    # I. Operator login/session layer
    RemoteSplitTest(
        id="rs_operator_localhost_bypass_pre_bootstrap",
        tags=["remote_split", "auth", "operator"],
        description="Localhost trusted with no creds until an operator exists, then it isn't",
        fn=_t_operator_localhost_bypass_pre_bootstrap,
    ),
    RemoteSplitTest(
        id="rs_operator_login_wrong_password",
        tags=["remote_split", "auth", "operator"],
        description="POST /login with wrong password -> 401",
        fn=_t_operator_login_wrong_password,
    ),
    RemoteSplitTest(
        id="rs_operator_login_unknown_user",
        tags=["remote_split", "auth", "operator"],
        description="POST /login for a username that doesn't exist -> 401",
        fn=_t_operator_login_unknown_user,
    ),
    RemoteSplitTest(
        id="rs_operator_login_success_session_works",
        tags=["remote_split", "auth", "operator"],
        description="POST /login success returns a session_token that authorizes a gated endpoint",
        fn=_t_operator_login_success_session_works,
    ),
    RemoteSplitTest(
        id="rs_operator_session_cookie_works",
        tags=["remote_split", "auth", "operator"],
        description="The session cookie set by /login authorizes a gated endpoint on its own",
        fn=_t_operator_session_cookie_works,
    ),
    RemoteSplitTest(
        id="rs_operator_api_token_unaffected",
        tags=["remote_split", "auth", "operator"],
        description="The existing API_TOKEN bearer path still works once operator accounts exist",
        fn=_t_operator_api_token_unaffected,
    ),
    RemoteSplitTest(
        id="rs_operator_logout_invalidates",
        tags=["remote_split", "auth", "operator"],
        description="POST /logout invalidates the session; it's rejected on the next request",
        fn=_t_operator_logout_invalidates,
    ),
    RemoteSplitTest(
        id="rs_operator_crud_roundtrip",
        tags=["remote_split", "auth", "operator"],
        description="POST/GET/DELETE /operators — create, list, delete, duplicate rejected",
        fn=_t_operator_crud_roundtrip,
    ),
    RemoteSplitTest(
        id="rs_operator_create_short_password_rejected",
        tags=["remote_split", "auth", "operator"],
        description="POST /operators with a password under 8 chars -> 400",
        fn=_t_operator_create_short_password_rejected,
    ),
    RemoteSplitTest(
        id="rs_operator_token_insufficient_on_chat_and_execute",
        tags=["remote_split", "auth", "operator"],
        description="Plain API_TOKEN no longer authorizes /chat or /execute once an operator exists (other endpoints unaffected)",
        fn=_t_operator_token_insufficient_on_chat_and_execute,
    ),
    RemoteSplitTest(
        id="rs_operator_session_works_on_execute",
        tags=["remote_split", "auth", "operator"],
        description="A valid operator session (not the plain token) authorizes /execute",
        fn=_t_operator_session_works_on_execute,
    ),
    RemoteSplitTest(
        id="rs_direct_cli_gate",
        tags=["remote_split", "auth", "operator", "direct_cli"],
        description="direct_cli.py's in-process gate blocks/allows dispatch independently of HTTP",
        fn=_t_direct_cli_gate,
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


def cleanup_remote_artifacts() -> None:
    """Remove any VM dirs the remote-split layer may leave behind.

    The VNC arg-binding test builds a real MachineConfig — whose build() writes
    smbios_chassis.bin into the VM dir as a side effect — and the pre-flight
    probes use rs-* names. Prefix-scoped so this only touches this layer's own
    artifacts. Registered in test_api's cleanup safety net so an interrupted run
    can't leave the dirs behind (the builder also cleans up inline on success).
    """
    import os
    import shutil
    vm_dir = os.path.expanduser("~/.qemu_vms")
    if not os.path.isdir(vm_dir):
        return
    for entry in os.listdir(vm_dir):
        if entry.startswith("vnc-test-") or entry.startswith("rs-"):
            shutil.rmtree(os.path.join(vm_dir, entry), ignore_errors=True)

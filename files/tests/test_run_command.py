"""test_run_command.py — confinement guard for the run_command primitive.

Proves the bubblewrap jail holds: writes confined to the workspace, the auth secrets
unreadable, network off, both languages work. If any of these fail, the write-gate has a
hole — this is the load-bearing safety test for the general-command capability.
See docs/design/general-command-primitive.md.
"""
import os
import shutil

import pytest

from executor.tool_dispatch.tools.run_command import RunCommandTool, WORKSPACE_DIR

pytestmark = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bubblewrap (bwrap) not installed"
)

_t = RunCommandTool()


def _run(code, lang="shell"):
    return _t.run({"code": code, "lang": lang}, None)


def test_basic_shell():
    r = _run("echo hi")
    assert r["success"] and r["stdout"].strip() == "hi"


def test_python_lang():
    r = _run("print(2 + 2)", "python")
    assert r["stdout"].strip() == "4"


def test_workspace_write_persists():
    p = os.path.join(WORKSPACE_DIR, "_t_ws.txt")
    try:
        r = _run("echo data > _t_ws.txt && cat _t_ws.txt")
        assert r["success"] and r["stdout"].strip() == "data"
        assert os.path.exists(p)
    finally:
        if os.path.exists(p):
            os.remove(p)


def test_no_escape_to_readonly_system():
    """A write to a read-only system path (/etc) must not create a real file."""
    _run("echo pwned > /etc/_t_escape 2>/dev/null")
    assert not os.path.exists("/etc/_t_escape")


def test_no_escape_to_real_home():
    """An absolute write to the real home (unbound) must not persist on the real fs."""
    home = os.path.expanduser("~")
    _run(f"echo pwned > {home}/_t_escape 2>/dev/null")
    assert not os.path.exists(os.path.join(home, "_t_escape"))


def test_secrets_unreadable():
    """The auth store under ~/.gorgon is never mounted → a confined command can't read it."""
    r = _run("cat /home/*/.gorgon/operators.json 2>&1 || true")
    assert "password_hash" not in r["stdout"] and "salt" not in r["stdout"]


def test_network_off_by_default():
    r = _run(
        "import socket; socket.setdefaulttimeout(3); "
        "socket.create_connection(('1.1.1.1', 53)); print('NET')",
        "python",
    )
    assert "NET" not in r["stdout"]


def test_unknown_lang_rejected():
    r = _run("whatever", "ruby")
    assert not r["success"] and "lang" in r.get("error", "").lower()


def test_missing_code_rejected():
    r = _t.run({"lang": "shell"}, None)
    assert not r["success"] and "code" in r.get("error", "").lower()


# ── prompt integration: the model is un-fenced (unless the agent forbids run_command) ──
def test_prompt_unfences_when_available():
    from orchestrator.ai.chat.ollama_client import _build_system_prompt, _run_command_available
    assert _run_command_available() is True
    p = _build_system_prompt()
    assert "EVERYDAY OPERATIONS" in p
    assert "run_command" in p and "local_probe" in p and "Unverified" in p


def test_prompt_gated_by_blacklist(monkeypatch):
    import orchestrator.ai.chat.ollama_client as oc
    monkeypatch.setattr(oc, "is_forbidden", lambda tool, args=None: tool == "run_command")
    assert oc._run_command_available() is False
    assert "EVERYDAY OPERATIONS" not in oc._build_system_prompt()

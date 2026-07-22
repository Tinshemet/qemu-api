"""test_local_probe.py — local_probe predicates + make_probe host routing.

Verifies the grounding half of the general-command capability: local_probe answers host
predicates (workspace-scoped), and make_probe routes a `local:`/`host:` clause to it so a
run_command effect is booked on reality, not the exit code.
"""
import os
import shutil

import pytest

from executor.tool_dispatch.tools.local_probe import LocalProbeTool
from executor.api._vm_constants import WORKSPACE_DIR
from executor.tool_dispatch.tool_executor import dispatch_tool
from orchestrator.ai.planner.autonomous import make_probe

pytestmark = pytest.mark.skipif(
    shutil.which("bwrap") is None, reason="bubblewrap (bwrap) not installed"
)

_lp = LocalProbeTool()
_execute = lambda tool, args: dispatch_tool(tool, args, verbose=True)


def _probe(assertion, target, value=None):
    args = {"assertion": assertion, "target": target}
    if value is not None:
        args["value"] = value
    return _lp.run(args, None)


@pytest.fixture
def wsfile():
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    p = os.path.join(WORKSPACE_DIR, "_probe_test.txt")
    with open(p, "w") as f:
        f.write("alpha beta gamma\n")
    yield p
    if os.path.exists(p):
        os.remove(p)


# ── local_probe predicates ──────────────────────────────────────────────────────
def test_file_exists(wsfile):
    assert _probe("file_exists", "_probe_test.txt")["holds"] is True
    assert _probe("file_exists", "_nope.txt")["holds"] is False


def test_dir_exists():
    assert _probe("dir_exists", ".")["holds"] is True


def test_file_contains(wsfile):
    assert _probe("file_contains", "_probe_test.txt", "beta")["holds"] is True
    assert _probe("file_contains", "_probe_test.txt", "zzz")["holds"] is False


def test_file_matches(wsfile):
    assert _probe("file_matches", "_probe_test.txt", r"al\w+")["holds"] is True


def test_is_writable(wsfile):
    assert _probe("is_writable", "_probe_test.txt")["holds"] is True


def test_command_available():
    assert _probe("command_available", "sh")["holds"] is True
    assert _probe("command_available", "definitely_not_a_cmd_xyz")["holds"] is False


def test_escape_is_unverifiable():
    assert _probe("file_exists", "/etc/passwd")["success"] is False          # absolute, outside ws
    assert _probe("file_exists", "../../../etc/passwd")["success"] is False  # traversal blocked


def test_contains_requires_value(wsfile):
    assert _probe("file_contains", "_probe_test.txt")["success"] is False


# ── make_probe routing (scope:assertion:target[:value]) ─────────────────────────
def test_make_probe_routes_local(wsfile):
    probe = make_probe(_execute)
    assert probe("local:file_exists:_probe_test.txt") is True
    assert probe("local:file_exists:_missing.txt") is False
    assert probe("local:file_contains:_probe_test.txt:beta") is True
    assert probe("local:file_exists:/etc/passwd") is None   # escape → unverifiable


def test_make_probe_integration_run_then_verify():
    """run_command creates a file; make_probe→local_probe verifies it (the ledger's honesty loop)."""
    probe = make_probe(_execute)
    out = os.path.join(WORKSPACE_DIR, "_integ.csv")
    if os.path.exists(out):
        os.remove(out)
    try:
        dispatch_tool("run_command", {"code": "printf 'a,b\\n1,2\\n' > _integ.csv"}, verbose=True)
        assert probe("local:file_exists:_integ.csv") is True
        assert probe("local:file_contains:_integ.csv:a,b") is True
    finally:
        if os.path.exists(out):
            os.remove(out)

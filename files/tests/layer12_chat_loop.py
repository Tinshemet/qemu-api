"""
layer12_chat_loop.py — characterization tests for orchestrator.ai.chat.cli.chat_loop.

chat_loop has no unit-test coverage today; it is an 781-line interactive REPL.
These tests pin its *current* observable behavior (which tools get executed, with
which args, given scripted user input + AI responses) so the planned decomposition
can be verified as behavior-preserving. They assert on observable outcomes, not on
internal structure — so they survive the refactor and catch regressions.

Run standalone::  python3 -m tests.layer12_chat_loop
"""
from typing import Callable, List, Tuple

from tests.chat_harness import run_chat, ChatRecording


# ── scenarios ────────────────────────────────────────────────────────────────
# Each returns None on success or raises AssertionError. Behavior here is the
# *observed* behavior of the current code — see the harness discovery notes.

def t_list_shortcut() -> None:
    """'list' is a slash-command shortcut — runs list_vms directly, no AI turn."""
    rec = run_chat(inputs=["list", "exit"], ollama=[])
    assert rec.tools == ["list_vms"], rec.tools


def t_system_shortcut() -> None:
    """'system' shortcut runs check_system directly."""
    rec = run_chat(inputs=["system", "exit"], ollama=[])
    assert rec.tools == ["check_system"], rec.tools


def t_clear_session_shortcut() -> None:
    """'clear session' clears without any tool execution."""
    rec = run_chat(inputs=["clear session", "exit"], ollama=[])
    assert rec.tools == [], rec.tools


def t_create_confirm_yes() -> None:
    """create_vm is a y/n-confirm tool; 'y' lets it execute."""
    rec = run_chat(
        inputs=["create a linux vm named box", "y", "exit"],
        ollama=[{"tools": [("create_vm", {"name": "box", "os_type": "linux"})]}, "done"],
    )
    assert "create_vm" in rec.tools, rec.tools


def t_create_confirm_no() -> None:
    """Declining the y/n confirm means create_vm never runs."""
    rec = run_chat(
        inputs=["create a linux vm named box", "n", "exit"],
        ollama=[{"tools": [("create_vm", {"name": "box", "os_type": "linux"})]}, "ok"],
    )
    assert "create_vm" not in rec.tools, rec.tools


def t_delete_double_confirm_ok() -> None:
    """delete_vm is critical — double confirm (YES, then the exact name) executes."""
    rec = run_chat(
        inputs=["delete vm box", "YES", "box", "exit"],
        ollama=[{"tools": [("delete_vm", {"name": "box"})]}, "deleted"],
    )
    # delete_vm is a _VM_TOOLS member, so the context-assistant gate's grounding
    # check probes list_vms (known_names) once before the real call goes through.
    assert rec.tools == ["list_vms", "delete_vm"], rec.tools


def t_delete_double_confirm_wrong_name() -> None:
    """Wrong name at the second step cancels the delete."""
    rec = run_chat(
        inputs=["delete vm box", "YES", "notbox", "exit"],
        ollama=[{"tools": [("delete_vm", {"name": "box"})]}, "ok"],
    )
    assert "delete_vm" not in rec.tools, rec.tools


def t_delete_first_step_cancel() -> None:
    """Empty first confirmation (not 'YES') cancels before the name step."""
    rec = run_chat(
        inputs=["delete vm box", "", "exit"],
        ollama=[{"tools": [("delete_vm", {"name": "box"})]}, "ok"],
    )
    assert "delete_vm" not in rec.tools, rec.tools


def t_preflight_ask_user_needs_both_confirms() -> None:
    """launch_vm w/ preflight ask_user needs the preflight choice AND the safety y/n."""
    pf = lambda n, a: ({"action": "ask_user", "reason": "r", "question": "q?",
                        "options": ["Yes, proceed", "Cancel"], "fix_field": None}
                       if n == "launch_vm" else {"action": "ok"})
    rec = run_chat(
        inputs=["launch box", "Yes, proceed", "y", "exit"],
        ollama=[{"tools": [("launch_vm", {"name": "box"})]}, "launched"],
        preflight=pf,
    )
    # launch_vm is a _VM_TOOLS member, so the context-assistant gate's grounding
    # check probes list_vms (known_names) once before the real call goes through.
    assert rec.tools == ["list_vms", "launch_vm"], rec.tools


def t_preflight_ask_user_cancel() -> None:
    """Choosing the Cancel option at the preflight prompt stops it."""
    pf = lambda n, a: ({"action": "ask_user", "reason": "r", "question": "q?",
                        "options": ["Yes, proceed", "Cancel"], "fix_field": None}
                       if n == "launch_vm" else {"action": "ok"})
    rec = run_chat(
        inputs=["launch box", "Cancel", "exit"],
        ollama=[{"tools": [("launch_vm", {"name": "box"})]}, "ok"],
        preflight=pf,
    )
    assert "launch_vm" not in rec.tools, rec.tools


def t_preflight_abort() -> None:
    """preflight abort blocks execution entirely (no prompt)."""
    pf = lambda n, a: ({"action": "abort", "reason": "no", "correction": "c"}
                       if n == "create_vm" else {"action": "ok"})
    rec = run_chat(
        inputs=["create a linux vm named box", "exit"],
        ollama=[{"tools": [("create_vm", {"name": "box", "os_type": "linux"})]}, "ok"],
        preflight=pf,
    )
    assert "create_vm" not in rec.tools, rec.tools


def t_preflight_auto_fix_applies() -> None:
    """auto_fix rewrites the args before the (still-required) safety confirm."""
    pf = lambda n, a: ({"action": "auto_fix", "fixed_args": {"name": "box", "os_type": "linux", "memory_mb": 2048},
                        "correction": "clamped memory"} if n == "create_vm" else {"action": "ok"})
    rec = run_chat(
        inputs=["create a linux vm named box", "y", "exit"],
        ollama=[{"tools": [("create_vm", {"name": "box", "os_type": "linux", "memory_mb": 999999})]}, "done"],
        preflight=pf,
    )
    assert rec.tools == ["create_vm"], rec.tools
    _, args = rec.executed[0]
    assert args.get("memory_mb") == 2048, args


def t_context_assistant_mismatch_replan() -> None:
    """A context-assistant hint re-prompts the AI; the mismatched call doesn't run,
    but the AI's corrected next call does."""
    # Fire the hint once (mismatch, not the 'never mentioned it' variant).
    hint_state = {"fired": False}
    def ctx(ui, tool, args):
        if not hint_state["fired"]:
            hint_state["fired"] = True
            return "That looks like a mismatch — did you mean a different tool?"
        return None
    rec = run_chat(
        inputs=["show me box", "exit"],
        ollama=[{"tools": [("delete_vm", {"name": "box"})]},   # mismatched → hint → replan
                {"tools": [("vm_status", {"name": "box"})]},   # corrected
                "here it is"],
        context=ctx,
    )
    assert "delete_vm" not in rec.tools, rec.tools
    assert "vm_status" in rec.tools, rec.tools


def t_executor_clarify_drain() -> None:
    """execute_tool returning clarify prompts the user, then the AI re-plans and
    the second attempt executes."""
    calls = {"n": 0}
    def execr(name, args):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"success": False, "clarify": True, "needs_clarification": "name",
                    "question": "What name?", "options": []}
        return {"success": True, "name": args.get("name", "")}
    # Real ordering: create_vm's y/n confirm fires BEFORE execute_tool, so the
    # first "y" is needed for the clarify to even be reached. The clarify answer
    # ("dev-box") marks name as clarified, so the 2nd attempt skips its confirm.
    rec = run_chat(
        inputs=["make a vm", "y", "dev-box", "exit"],
        ollama=[{"tools": [("create_vm", {"name": "x", "os_type": "linux"})]},
                {"tools": [("create_vm", {"name": "dev-box", "os_type": "linux"})]},
                "created"],
        exec_results=execr,
    )
    assert rec.tools.count("create_vm") == 2, rec.tools


def t_exit_clean() -> None:
    """An exit command ends the session with nothing executed."""
    rec = run_chat(inputs=["quit"], ollama=[])
    assert rec.tools == [], rec.tools


SCENARIOS: List[Tuple[str, Callable[[], None]]] = [
    ("list shortcut → list_vms",                    t_list_shortcut),
    ("system shortcut → check_system",              t_system_shortcut),
    ("clear session → no tool",                     t_clear_session_shortcut),
    ("create_vm y/n confirm YES → executes",        t_create_confirm_yes),
    ("create_vm y/n confirm NO → skipped",          t_create_confirm_no),
    ("delete_vm double-confirm OK → executes",      t_delete_double_confirm_ok),
    ("delete_vm wrong name → cancelled",            t_delete_double_confirm_wrong_name),
    ("delete_vm first-step cancel → skipped",       t_delete_first_step_cancel),
    ("preflight ask_user needs both confirms",      t_preflight_ask_user_needs_both_confirms),
    ("preflight ask_user cancel → skipped",         t_preflight_ask_user_cancel),
    ("preflight abort → skipped",                   t_preflight_abort),
    ("preflight auto_fix rewrites args",            t_preflight_auto_fix_applies),
    ("context-assistant mismatch → replan",         t_context_assistant_mismatch_replan),
    ("executor clarify → drain → re-execute",       t_executor_clarify_drain),
    ("exit command → clean end",                    t_exit_clean),
]


CHAT_TESTS = SCENARIOS   # framework alias — each item is a (name, fn) tuple


def run_chat_scenario(tc: Tuple[str, Callable[[], None]]):
    """Run one characterization scenario and return a framework TestResult.

    Example::

        r = run_chat_scenario(("exit → clean", t_exit_clean))
        r.passed  # → True
    """
    import time
    from tests.shared import TestResult
    name, fn = tc
    start = time.time()
    try:
        fn()
        return TestResult(test_id=name, layer=12, passed=True, issues=[],
                          fixes_applied=[], duration_s=time.time() - start)
    except AssertionError as e:
        return TestResult(test_id=name, layer=12, passed=False, issues=[f"got {e}"],
                          fixes_applied=[], duration_s=time.time() - start)
    except Exception as e:   # noqa: BLE001 — any unexpected error is a failure
        return TestResult(test_id=name, layer=12, passed=False,
                          issues=[f"{type(e).__name__}: {e}"],
                          fixes_applied=[], duration_s=time.time() - start)


def run() -> Tuple[int, int]:
    """Run all scenarios; return (passed, total). Prints a per-scenario line."""
    passed = 0
    for name, fn in SCENARIOS:
        try:
            fn()
            passed += 1
            print(f"  ✓ {name}")
        except AssertionError as e:
            print(f"  ✗ {name}  — got {e}")
        except Exception as e:  # noqa: BLE001 — report any unexpected error as a failure
            import traceback
            print(f"  ✗ {name}  — ERROR {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\nchat_loop characterization: {passed}/{len(SCENARIOS)} passed")
    return passed, len(SCENARIOS)


if __name__ == "__main__":
    import sys
    p, t = run()
    sys.exit(0 if p == t else 1)

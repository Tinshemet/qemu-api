"""
chat_harness.py — deterministic driver for orchestrator.ai.cli.chat_loop.

chat_loop is an 781-line interactive REPL with no seams for testing on its own.
This harness mocks every I/O / AI / tool-execution boundary so a scenario can be
expressed as scripts of (user inputs, AI responses, tool results) and the
*observable* behavior recorded: the ordered list of execute_tool calls (with
args) and the final saved session messages.

It exists to characterize chat_loop's current behavior before refactoring it —
every mock is a boundary, never internal logic, so the real dispatch/gate code
runs unchanged.

Example::

    rec = run_chat(
        inputs=["list", "exit"],
        ollama=[],                       # 'list' is a shortcut — no AI round-trip
    )
    assert [n for n, _ in rec.executed] == ["list_vms"]
"""
import contextlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from unittest.mock import patch

import orchestrator.ai.cli as cli
import orchestrator.ai.chat_turn as chat_turn
import orchestrator.ai.chat_types as chat_types
import shared.display as _display


@dataclass
class ChatRecording:
    """What a scripted chat_loop run did that a caller can assert on."""
    executed: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    saved:    Optional[List[Dict[str, Any]]]   = None
    prints:   List[str]                        = field(default_factory=list)

    @property
    def tools(self) -> List[str]:
        """Just the tool names, in call order."""
        return [name for name, _ in self.executed]

    def printed(self, substr: str) -> bool:
        """True if any console.print line contained *substr* (case-insensitive)."""
        low = substr.lower()
        return any(low in p.lower() for p in self.prints)


def _ollama_message(spec: Union[dict, str]) -> Dict[str, Any]:
    """Turn a compact spec into an Ollama chat response.

    spec is either a plain string (assistant text, no tools) or a dict
    ``{"content": str, "tools": [(tool_name, args_dict), ...]}``.
    """
    if isinstance(spec, str):
        spec = {"content": spec}
    return {"message": {
        "content":    spec.get("content", ""),
        "tool_calls": [{"function": {"name": n, "arguments": a}}
                       for n, a in spec.get("tools", [])],
    }}


def run_chat(
    *,
    inputs:       List[str],
    ollama:       List[Union[dict, str]],
    exec_results: Union[dict, Callable[[str, dict], dict], None] = None,
    preflight:    Union[dict, Callable[[str, dict], dict], None] = None,
    context:      Union[str, Callable[[str, str, dict], Optional[str]], None] = None,
    slots:        Union[dict, Callable[[str], dict], None] = None,
    loop_max:     int = 5,
) -> ChatRecording:
    """Drive cli.chat_loop through one scripted session and record what it did.

    Args:
        inputs:  console.input returns these in order; when exhausted it raises
                 EOFError, which chat_loop treats as end-of-session.
        ollama:  _call_ollama returns these (compact specs) in order; when
                 exhausted it returns None ("no response" -> break the AI loop).
        exec_results: what execute_tool returns — a dict keyed by tool name, a
                 callable ``(name, args) -> result``, or None for a generic
                 success. Every call is recorded regardless.
        preflight: what _preflight_check returns — an action dict, a callable, or
                 None for ``{"action": "ok"}``.
        context: what check_context returns (the context-assistant hint) — a
                 string, a callable, or None.
        slots:   what extract_slots returns (the pre-gate's view of the user's
                 message) — a dict, a callable, or None for ``{}``.
        loop_max: the per-turn agentic tool-loop cap for this run.

    Returns:
        A ChatRecording with .executed / .tools / .saved / .prints.

    Example::

        rec = run_chat(inputs=["create a vm named box", "y", "exit"],
                       ollama=[{"tools": [("create_vm", {"name": "box"})]}, "done"])
        assert "create_vm" in rec.tools
    """
    rec = ChatRecording()

    pending_inputs = list(inputs)
    def _input(*_a, **_k):
        if pending_inputs:
            return pending_inputs.pop(0)
        raise EOFError

    pending_ollama = [_ollama_message(s) for s in ollama]
    def _call_ollama(_messages):
        return pending_ollama.pop(0) if pending_ollama else None

    def _execute_tool(name, args, verbose=False):
        rec.executed.append((name, dict(args)))
        if callable(exec_results):
            return exec_results(name, args)
        if isinstance(exec_results, dict) and name in exec_results:
            return exec_results[name]
        return {"success": True, "name": args.get("name", ""), "vm_dir": "/x"}

    def _preflight_check(name, args, _mgr, _verbose, stateless_only=False):
        if callable(preflight):
            return preflight(name, args)
        return preflight if isinstance(preflight, dict) else {"action": "ok"}

    def _check_context(user_input, tool, args, recent_context=""):
        return context(user_input, tool, args) if callable(context) else context

    def _extract_slots(user_input):
        return slots(user_input) if callable(slots) else (slots or {})

    def _print(*a, **_k):
        rec.prints.append(" ".join(str(x) for x in a))

    def _save(messages):
        rec.saved = [dict(m) for m in messages]

    stack = contextlib.ExitStack()
    add = stack.enter_context
    # Environment: force local mode (no liveness thread, full preflight path) and
    # disable session auto-clear so the run is deterministic.
    # A boundary is patched in BOTH modules that import it: the gate pipeline
    # now lives in chat_turn.py while the REPL shell stays in cli.py, and each
    # binds these names into its own namespace at import time. patch.object
    # rebinds per-namespace, so a seam reached from both sides must be patched
    # on both. hasattr keeps it correct when a name lives in only one module.
    def _seam(name, repl):
        for _mod in (cli, chat_turn, chat_types):
            if hasattr(_mod, name):
                add(patch.object(_mod, name, repl))

    _seam("API_URL", "local")
    add(patch.object(cli, "AUTO_CLEAR_SESSION", False))
    add(patch.object(cli, "_LOOP_MAX", loop_max))
    # Seams.
    _seam("_call_ollama", _call_ollama)
    _seam("execute_tool", _execute_tool)
    _seam("_preflight_check", _preflight_check)
    _seam("check_context", _check_context)
    _seam("extract_slots", _extract_slots)
    _seam("load_session", lambda: [])
    _seam("save_session", _save)
    _seam("detect_drift", lambda _m: None)
    _seam("clear_session", lambda: None)
    _seam("print_banner", lambda **_k: None)
    _seam("_get_ovmf", lambda: {"available": False, "code": ""})
    _seam("render_vm_specs", lambda *_a, **_k: None)
    _seam("_show_preflight_warning", lambda *_a, **_k: None)
    _seam("set_custom_mode", lambda *_a, **_k: None)
    add(patch.object(_display, "render_vnc_connect", lambda *_a, **_k: None))
    add(patch.object(cli.console, "input", _input))
    add(patch.object(cli.console, "print", _print))
    with stack:
        cli.chat_loop(verbose=False)
    return rec

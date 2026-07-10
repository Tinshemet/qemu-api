"""
cli.py — CLI Entry Point and Chat Loop Layer

Provides the interactive AI chat loop and the direct sub-command CLI
(qemu-api list, launch, stop, etc.). This is the main entry point
for both modes; ollama_wrapper.py is a thin shim that re-exports from here.
"""

import json
import os
import sys
import threading
from typing import List

from rich.panel import Panel
from rich.table import Table

_MC = {"os_type": "linux", "cpu_cores": 2, "memory_mb": 2048, "machine_type": "q35", "uefi": False}
from orchestrator.executor_client import get_ovmf as _get_ovmf  # noqa: E402
from .session import (
    AUTO_CLEAR_SESSION, clear_session, detect_drift, load_session,
    save_session, set_auto_clear, set_loop_max, get_loop_max,
)
from shared.display import console, print_banner
from .ollama_client      import OLLAMA_MODEL, OLLAMA_URL, _call_ollama
from .context_assistant  import check_context, extract_slots
from orchestrator.sanitizer.context_gate import _REQUIRED as _GATE_REQUIRED
from orchestrator.sanitizer.sanitizer import OS_TYPE_ALIASES
from orchestrator.executor_client import execute_tool, API_URL, _VERIFY
try:
    from shared.executioner.tool_executor import manager, _VM_DEFS
except ImportError:
    manager = None                                                            # type: ignore[assignment]
    _VM_DEFS = {"disk_size_gb": 60, "network_mode": "nat", "disk_bus": "virtio"}
from orchestrator.preflight.validator import set_custom_mode, _preflight_check
from .chat_turn import (  # per-turn processing (extracted from this file)
    TurnState, GateOutcome, _process_tool_call, _is_critical,
    _build_vm_spec_rows,   # re-exported: create_vm spec-preview (used by tests)
)
from .http_chat import process_message  # re-exported: HTTP /chat entry (used by api_server)

_CFG            = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_EXIT_CMDS      = set(_CFG["exit_commands"])
_SHORTCUTS      = _CFG["shortcut_commands"]
_LOOP_MAX       = get_loop_max()   # respects tool_loop_max_override if set
_ACTION_WORDS   = set(_CFG["action_words"])
_OS_KEYWORDS    = set(_CFG["os_keywords_gate"])
_CONFIRM_YN     = {k: tuple(v) for k, v in _CFG["confirm_yn"].items()}
_CONFIRM_NAME   = {k: tuple(v) for k, v in _CFG["confirm_name"].items()}
_RENDERS_OUTPUT = set(_CFG.get("rendered_tools", []))


def _show_drift_report(messages: list, runtime_drift_count: int) -> None:
    """Render the session drift report (orphaned turns, consecutive no-tool count)."""
    from rich.table import Table
    from rich.text  import Text

    user_count      = sum(1 for m in messages if m.get("role") == "user")
    assistant_count = sum(1 for m in messages if m.get("role") == "assistant")
    orphan_count    = user_count - assistant_count
    orphan_pct      = int(orphan_count / user_count * 100) if user_count else 0

    max_consec, consec = 0, 0
    for m in messages:
        if m.get("role") == "user":
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 0

    drift_result = detect_drift(messages)
    if drift_result:
        level, _ = drift_result
        if level == "critical":
            status_text = Text("✖ CRITICAL — model likely poisoned", style="bold red")
        else:
            status_text = Text("⚠ WARNING — early drift signal", style="bold yellow")
    else:
        status_text = Text("✓ HEALTHY", style="bold green")

    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column("key",   style="dim")
    t.add_column("value", style="bold")

    t.add_row("Status",                  status_text)
    t.add_row("Session messages",        str(len(messages)))
    t.add_row("User turns",              str(user_count))
    t.add_row("Verified responses",      str(assistant_count))
    t.add_row("Orphaned turns",          f"{orphan_count}  ({orphan_pct}%)")
    t.add_row("Max consecutive orphans", str(max_consec))
    t.add_row("Runtime drift (turns)",   str(runtime_drift_count))

    if drift_result:
        _, msg = drift_result
        t.add_row("Advice", Text(msg, style="yellow" if drift_result[0] == "warn" else "red"))

    console.print(Panel(t, title="Session Drift Report", border_style="cyan"))


# ── Chat loop ──────────────────────────────────────────────────────────────────


def _handle_command(ui: str, messages: List[dict], runtime_drift_count: int,
                    verbose: bool) -> bool:
    """Handle a REPL slash-command shortcut, if the input is one.

    Covers list / system / profiles / clear-session / drift / auto-clear on|off /
    loop-limit. Returns True when it handled the input (the caller continues the
    REPL), False when the input isn't a command (fall through to the AI).

    Example::

        _handle_command("list", messages, 0, False)   # → True (ran list_vms)
        _handle_command("make a vm", messages, 0, False)  # → False
    """
    global _LOOP_MAX
    if ui in _SHORTCUTS["list"]:
        execute_tool("list_vms", {}, verbose)
        return True
    if ui in _SHORTCUTS["system"]:
        execute_tool("check_system", {}, verbose)
        return True
    if ui in _SHORTCUTS["profiles"]:
        execute_tool("list_profiles", {}, verbose)
        return True
    if ui in _SHORTCUTS["clear_session"]:
        clear_session()
        messages.clear()
        console.print("[dim]Session cleared.[/dim]")
        return True
    if ui in _SHORTCUTS["drift"]:
        _show_drift_report(messages, runtime_drift_count)
        return True
    if ui in _SHORTCUTS["auto_clear_on"]:
        set_auto_clear(True)
        console.print("[dim]Auto-clear enabled — session will be cleared on next start.[/dim]")
        return True
    if ui in _SHORTCUTS["auto_clear_off"]:
        set_auto_clear(False)
        console.print("[dim]Auto-clear disabled.[/dim]")
        return True
    ll_matched = next((s for s in _SHORTCUTS["loop_limit"] if ui == s or ui.startswith(s + " ")), None)
    if ll_matched is not None:
        ll_inline = ui[len(ll_matched):].strip()
        if ll_inline:
            ll_input = ll_inline
        else:
            console.print(f"[dim]Current tool loop limit: [bold]{_LOOP_MAX}[/bold] (default: {_CFG['chat']['tool_loop_max']})[/dim]")
            console.print("[dim]Enter a number to set a new limit, or press Enter to clear the override.[/dim]")
            try:
                ll_input = console.input("[bold cyan]New limit:[/bold cyan] ").strip()
            except (KeyboardInterrupt, EOFError):
                return True
        if ll_input == "":
            set_loop_max(None)
            _LOOP_MAX = _CFG["chat"]["tool_loop_max"]
            console.print(f"[dim]Loop limit reset to default ({_LOOP_MAX}).[/dim]")
        elif ll_input.isdigit() and int(ll_input) > 0:
            _LOOP_MAX = int(ll_input)
            set_loop_max(_LOOP_MAX)
            console.print(f"[dim]Loop limit set to {_LOOP_MAX}.[/dim]")
        else:
            console.print("[dim]Invalid input — loop limit unchanged.[/dim]")
        return True
    return False


def chat_loop(verbose: bool = False) -> None:
    """Run the interactive AI chat REPL until the user exits."""
    global _LOOP_MAX
    print_banner(
        verbose=verbose,
        ollama_url=OLLAMA_URL,
        ollama_model=OLLAMA_MODEL,
        ovmf_available=_get_ovmf().get("available", False),
        ovmf_code=_get_ovmf().get("code", ""),
        api_url=API_URL,
    )
    if AUTO_CLEAR_SESSION:
        clear_session()
        console.print("[dim]Session auto-cleared (auto_clear=true in config).[/dim]")

    messages = load_session()

    drift_result = detect_drift(messages)
    if drift_result:
        _drift_level, _drift_msg = drift_result
        if _drift_level == "critical":
            console.print(f"[bold red]✖ Session critically drifted — auto-clearing to prevent poisoning.[/bold red]")
            console.print(f"[dim]{_drift_msg}[/dim]")
            clear_session()
            messages = []
        else:
            console.print(f"[bold yellow]⚠ {_drift_msg}[/bold yellow]")

    _runtime_drift_count  = 0    # consecutive action turns with no tool execution
    _synthetic_continue   = False # True when re-entering loop after cap-hit continuation

    # Background liveness monitor — pings /health every 30 s when remote.
    if API_URL != "local":
        import threading, requests as _req
        _liveness_stop = threading.Event()
        def _liveness_loop() -> None:
            """Background thread — ping the server every 30s and warn if it stops responding."""
            import time as _t
            while not _liveness_stop.wait(30):
                try:
                    r = _req.get(f"{API_URL}/health", timeout=5, verify=_VERIFY)
                    if not r.ok:
                        console.print(f"\n[bold yellow]⚠ Client machine health check failed ({r.status_code}) — it may have restarted.[/bold yellow]")
                except Exception:
                    console.print(f"\n[bold red]✖ Client machine at {API_URL} is not responding. Check that 'qemu-api serve' is still running.[/bold red]")
        _liveness_thread = threading.Thread(target=_liveness_loop, daemon=True)
        _liveness_thread.start()

    while True:
        _is_synthetic = _synthetic_continue
        if _synthetic_continue:
            _synthetic_continue = False
        else:
            try:
                user_input = console.input("\n[bold cyan]You:[/bold cyan] ").strip()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Goodbye.[/dim]")
                break

            if not user_input:
                continue

        _ui = user_input.lower().strip()

        if _ui in _EXIT_CMDS:
            console.print("[dim]Goodbye.[/dim]")
            break

        if _handle_command(_ui, messages, _runtime_drift_count, verbose):
            continue

        if not _is_synthetic:
            messages.append({"role": "user", "content": user_input})

        state = TurnState(user_wants_action=bool(set(_ui.split()) & _ACTION_WORDS))

        # Agentic tool loop — up to _LOOP_MAX rounds per user turn
        for _loop_iter in range(_LOOP_MAX):
            response = _call_ollama(messages)
            if not response:
                console.print("[warn]No response from Ollama.[/warn]")
                break

            msg           = response.get("message", {})
            assistant_msg = {
                "role":       "assistant",
                "content":    msg.get("content", ""),
                "tool_calls": msg.get("tool_calls", []),
            }
            messages.append(assistant_msg)

            tool_calls = msg.get("tool_calls", [])
            state.last_had_tools = bool(tool_calls)
            if not tool_calls:
                text = msg.get("content", "").strip()
                # Empty response (no tool calls, no text) — nudge the AI to respond.
                if not text and _loop_iter < _LOOP_MAX - 1:
                    messages.pop()
                    messages.append({
                        "role":    "user",
                        "content": (
                            "_INTERNAL_ Your last response was empty. "
                            "Please call the appropriate tool or provide a text response now."
                        ),
                    })
                    continue
                # If the model gave a text-only response for an action request
                # without ever calling a tool, it hallucinated — force a retry.
                if state.user_wants_action and not state.tools_called and _loop_iter < _LOOP_MAX - 1:
                    # Remove the bad assistant message so the model doesn't
                    # anchor on its own hallucinated success in the next attempt.
                    # Use _INTERNAL_ prefix so save_session filters it out.
                    messages.pop()
                    messages.append({
                        "role":    "user",
                        "content": (
                            "_INTERNAL_ You responded with text but did not call any tool. "
                            "You cannot perform actions by text alone — you MUST call "
                            "the appropriate tool (e.g. create_vm, launch_vm, list_vms). "
                            "Call the tool now."
                        ),
                    })
                    continue
                if text:
                    console.print(f"\n[bold green]Assistant:[/bold green] {text}\n")
                break

            state.tools_called = True

            state.reset_iteration()

            for tc in tool_calls:
                _tc_out = _process_tool_call(tc, user_input, _ui, state, messages, verbose)
                if _tc_out is GateOutcome.EXIT:
                    return
                if _tc_out is not GateOutcome.PROCEED:   # REPLAN / CANCELLED — end this round
                    break

            if state.op_cancelled:
                continue  # let AI ask what the user wants to do instead

            if state.context_assistant_fired and not tool_calls:
                # Mismatch/high-stakes path: hint was injected, give AI another pass.
                # Hallucinated-field path: args were patched in-place, loop continues normally.
                state.tools_called = False
                continue

            if state.clarify_happened:
                hint = (
                    f" The user provided: {state.clarify_answer} (for fields: {state.clarify_field})."
                    if state.clarify_field else ""
                )
                messages.append({
                    "role":    "user",
                    "content": (
                        f"_INTERNAL_{hint}"
                        " Now call the appropriate tool again using only what the user has"
                        " explicitly provided in this conversation — do not reuse names or"
                        " values from earlier sessions."
                    ),
                })
                continue

        else:
            # for loop exhausted all _LOOP_MAX iterations without a natural break
            if state.last_had_tools:
                console.print(
                    f"\n[yellow]⚠  Tool loop reached the {_LOOP_MAX}-iteration limit "
                    f"— the task may be incomplete.[/yellow]"
                )
                try:
                    _cap_ans = console.input("[bold cyan]Continue? (y/n):[/bold cyan] ").strip().lower()
                except (KeyboardInterrupt, EOFError):
                    _cap_ans = "n"
                if _cap_ans == "y":
                    messages.append({
                        "role":    "user",
                        "content": "_INTERNAL_ You were cut off by the tool loop limit. Continue the task from where you left off.",
                    })
                    _synthetic_continue = True
                    save_session(messages)
                    continue  # outer REPL while loop — no new user input needed

        # If the user wanted an action but no tool ever executed, the last
        # assistant message is a hallucinated success — strip it before saving.
        if state.user_wants_action and not state.tool_executed and messages:
            last = messages[-1]
            if last.get("role") == "assistant" and not last.get("tool_calls"):
                messages.pop()
        save_session(messages)

        # Runtime drift counter — warn after 3 consecutive action turns where
        # the model gave text instead of calling a tool.
        if state.user_wants_action and not state.tool_executed:
            _runtime_drift_count += 1
            if _runtime_drift_count >= 6:
                console.print(
                    f"[bold red]✖ drift critical: {_runtime_drift_count} consecutive turns "
                    f"with no tool call — the model is likely poisoned. "
                    f"Type 'clear session' now.[/bold red]"
                )
            elif _runtime_drift_count >= 3:
                console.print(
                    f"[bold yellow]⚠ drift detected: {_runtime_drift_count} consecutive "
                    f"turns with no tool call — type 'clear session' to reset[/bold yellow]"
                )
        elif state.tool_executed:
            _runtime_drift_count = 0


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from .direct_cli import cli_direct

    argv    = sys.argv[1:]
    verbose = "-v" in argv or "--verbose" in argv
    argv    = [a for a in argv if a not in ("-v", "--verbose")]

    if "-cu" in argv:
        set_custom_mode(True)
        argv = [a for a in argv if a != "-cu"]
        console.print("[dim]Custom mode active — product verification disabled[/dim]")

    if "-cs" in argv:
        clear_session()
        argv = [a for a in argv if a != "-cs"]
        console.print("[dim]Session cleared.[/dim]")

    if argv:
        cli_direct(argv, verbose=verbose)
    else:
        chat_loop(verbose=verbose)

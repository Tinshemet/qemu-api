"""
cli.py — orchestrator CLI entry point and chat-loop layer.

The terminal entry point for the orchestrator side: ``OrchestratorCLI.run()`` (via
``python3 -m orchestrator.ai.chat.cli``) parses the -v/-cu/-cs flags and routes to
the direct sub-command CLI (``chat/commands`` → ``cli_direct``, args present) or the
interactive AI chat REPL (``chat_loop``, no args). The client-side entry is
``client/client_wrapper.py`` → ``ClientCLI`` (the ``gorgon`` alias). ``process_message``
(the HTTP single-turn twin of ``chat_loop``) is re-exported here for the api_server.
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
    save_session, get_loop_max, get_verbose,
)
from .shortcuts import handle_command   # the REPL shortcut dispatcher (list/system/drift/…)
from shared.display import console, print_banner
from ..active_library     import LIBRARY
from .ollama_client      import OLLAMA_MODEL, OLLAMA_URL, _call_ollama
from .context_assistant  import check_context, extract_slots, proactive_prep
from orchestrator.sanitizer.context_gate import _REQUIRED as _GATE_REQUIRED
from orchestrator.sanitizer.sanitizer import OS_TYPE_ALIASES
from orchestrator.executor_client import API_URL, _VERIFY
try:
    from executor.tool_dispatch.tool_executor import manager, _VM_DEFS
except ImportError:
    manager = None                                                            # type: ignore[assignment]
    _VM_DEFS = {"disk_size_gb": 60, "network_mode": "nat", "disk_bus": "virtio"}
from orchestrator.preflight.validator import set_custom_mode, _preflight_check
from orchestrator.auth import store as _auth_store, sessions as _auth_sessions
from .chat_turn import (  # per-turn processing (extracted from this file)
    TurnState, GateOutcome, _process_tool_call, _is_critical,
    _build_vm_spec_rows,   # re-exported: create_vm spec-preview (used by tests)
)
from .http_chat import process_message  # re-exported: HTTP /chat entry (used by api_server)

_CFG            = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_EXIT_CMDS      = set(_CFG["exit_commands"])
_LOOP_MAX       = get_loop_max()   # respects tool_loop_max_override if set (shortcuts mutate this)
_ACTION_WORDS   = set(_CFG["action_words"])
# State/read-query words. Folded into the "wants a tool" trigger so the model is
# forced to ground factual questions ("which VMs are running?") in a tool call
# instead of answering — and hallucinating — from memory.
_STATE_QUERY_WORDS = set(_CFG.get("state_query_words", []))
_OS_KEYWORDS    = set(_CFG["os_keywords_gate"])
_RENDERS_OUTPUT = set(_CFG.get("rendered_tools", []))


# ── Chat loop ──────────────────────────────────────────────────────────────────


def _repl_require_operator_password(action: str) -> bool:
    """Re-authenticate the operator for a high-impact contract action in the REPL.
    Mirrors the client's _require_operator_password: an active session isn't
    enough — the password must be re-entered. Degrades open pre-bootstrap."""
    if not _auth_store.operators_exist():
        return True
    user = _auth_sessions.current_username()
    if not user:
        console.print("[bold red]Login required.[/bold red] Run [cyan]gorgon login[/cyan] first.")
        return False
    import getpass
    if _auth_store.verify_password(user, getpass.getpass(f"Operator password to {action}: ")):
        return True
    console.print("[bold red]Password incorrect — aborted.[/bold red]")
    return False


def _maybe_forge_contract(ui: str) -> bool:
    """Handle a forge-a-contract request inline in the REPL instead of the AI loop.

    The local REPL is console-based, so it runs the real interactive forge
    (forge_fields.json-driven, operator-gated) directly — parity with the client
    chat wizard and `gorgon contract forge`. Returns True when it handled the
    input (the caller continues the REPL, skipping the model); False otherwise,
    so a question about contracts still reaches the AI. Detection is shared with
    the chat wizard so both surfaces trigger identically.
    """
    from orchestrator.ai.agent import forge_chat as _forge_chat
    if not _forge_chat.looks_like_forge_intent(ui):
        return False
    if not _repl_require_operator_password("forge a contract"):
        return True  # handled (aborted) — do not fall through to the model
    from orchestrator.ai.agent import forge as _forge
    import shared.bundle as _bundle
    _forge.forge_interactive(
        ask=lambda p: console.input(f"[bold cyan]{p}:[/bold cyan] ").strip(),
        out=console.print,
        write_dir=_bundle.AGENTS_ROOT,   # a forged agent lands in its bundle
    )
    return True


def chat_loop(verbose: bool = False) -> None:
    """Run the interactive AI chat REPL until the user exits."""
    global _LOOP_MAX
    _forced_verbose = verbose      # -v forces it on; otherwise follow the persisted toggle,
    verbose = _forced_verbose or get_verbose()   # refreshed each turn so `verbose on/off` applies live

    # Same gate as chat/commands/ cli_direct() — both are local, in-process
    # entry points to `manager`, so both need it independently (neither goes
    # through orchestrator/http/api_server.py's _require_auth). Pre-bootstrap
    # (no operator accounts yet) this is a no-op, matching legacy behavior.
    if _auth_store.operators_exist() and _auth_sessions.current_username() is None:
        console.print("[bold red]Login required.[/bold red] Run [cyan]gorgon login[/cyan] first.")
        return

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

    # Build the Active Library once at session start; the per-turn system prompt
    # reads its digest, and each executed tool keeps it current via apply().
    LIBRARY.snapshot()

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
            """Background thread — ping the executor every 30s and warn if it stops responding."""
            import time as _t
            while not _liveness_stop.wait(30):
                try:
                    r = _req.get(f"{API_URL}/health", timeout=5, verify=_VERIFY)
                    if not r.ok:
                        console.print(f"\n[bold yellow]⚠ Executor health check failed ({r.status_code}) — it may have restarted.[/bold yellow]")
                except Exception:
                    console.print(f"\n[bold red]✖ Executor at {API_URL} is not responding. Check that it's still running.[/bold red]")
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
        verbose = _forced_verbose or get_verbose()   # pick up a `verbose on/off` toggle from a prior turn

        if _ui in _EXIT_CMDS:
            console.print("[dim]Goodbye.[/dim]")
            break

        if handle_command(_ui, messages, _runtime_drift_count, verbose):
            continue

        if _maybe_forge_contract(_ui):
            continue

        # sign/edit/show/list a contract → point at the CLI (they act on a file)
        from orchestrator.ai.agent import forge_chat as _fc
        _contract_redirect = _fc.contract_cli_redirect(_ui)
        if _contract_redirect:
            console.print(f"[yellow]{_contract_redirect}[/yellow]")
            continue

        if not _is_synthetic:
            messages.append({"role": "user", "content": user_input})

        # "action" here means any tool-worthy intent — an action OR a state/read
        # query — so factual questions get grounded in a tool call, not confabulated.
        state = TurnState(user_wants_action=bool({t.strip('.,!?;:') for t in _ui.split()} & (_ACTION_WORDS | _STATE_QUERY_WORDS)))

        # Proactive pre-pass: deterministic guidance (likely tool + literal slots)
        # injected ONLY on the first round — the initial step where a wrong pick
        # costs a whole churn round — and transiently (never persisted to history).
        _guidance = "" if _is_synthetic else proactive_prep(user_input)
        # NB: round-0 tool-narrowing was tried here and REVERTED — verified it
        # degrades llama3.1's referential reasoning (offering 4 tools instead of 46
        # made "same OS as test1" hallucinate os_type 4/4 runs). The fuller tool
        # context anchors the weak model; the soft guidance above is the win, not
        # restricting the tool set. See context_assistant.narrow_tools.

        # Agentic tool loop — up to _LOOP_MAX rounds per user turn
        for _loop_iter in range(_LOOP_MAX):
            _call_msgs = messages
            if _loop_iter == 0 and _guidance:
                _call_msgs = messages + [{"role": "user", "content": "_INTERNAL_ " + _guidance}]
            response = _call_ollama(_call_msgs)
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

class OrchestratorCLI:
    """The orchestrator terminal entry point (``python3 -m orchestrator.ai.chat.cli``).

    ``run()`` parses the leading flags — ``-v``/``--verbose`` (verbose tool output),
    ``-cu`` (custom mode: skip product verification), ``-cs`` (clear the saved
    session) — then routes: any remaining args → the direct sub-command CLI
    (``cli_direct``); no args → the interactive chat REPL (``chat_loop``).
    """

    def run(self, argv=None) -> None:
        """Parse the flags and dispatch. ``argv`` defaults to ``sys.argv[1:]``;
        pass a list to drive it in tests."""
        self._migrate_bundles()
        argv = list(sys.argv[1:] if argv is None else argv)
        argv, verbose = self._parse_flags(argv)
        self._dispatch(argv, verbose)

    def _migrate_bundles(self) -> None:
        """Best-effort, idempotent one-time consolidation of legacy scattered agent
        state into ~/.qemu_vms/_agents/ bundles. Never blocks startup."""
        try:
            import shared.bundle as _bundle
            from orchestrator.ai.agent import AGENT_DIR
            _bundle.migrate(AGENT_DIR)
        except Exception:
            pass

    def _parse_flags(self, argv):
        """Strip -v/-cu/-cs (applying their side effects); return (argv, verbose).
        Verbose is on if -v/--verbose is present OR the persisted toggle is set."""
        verbose = "-v" in argv or "--verbose" in argv or get_verbose()
        argv    = [a for a in argv if a not in ("-v", "--verbose")]
        if "-cu" in argv:
            argv = [a for a in argv if a != "-cu"]
            set_custom_mode(True)
            console.print("[dim]Custom mode active — product verification disabled[/dim]")
        if "-cs" in argv:
            argv = [a for a in argv if a != "-cs"]
            clear_session()
            console.print("[dim]Session cleared.[/dim]")
        return argv, verbose

    def _dispatch(self, argv, verbose) -> None:
        """Route: remaining args → the direct sub-command CLI; none → the chat REPL."""
        if argv:
            from .commands import cli_direct
            cli_direct(argv, verbose=verbose)
        else:
            chat_loop(verbose=verbose)


if __name__ == "__main__":
    OrchestratorCLI().run()

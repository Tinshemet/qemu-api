"""
chat_turn.py — the interactive gates and per-tool-call dispatch.

The five interactive gates (safety, pre-flight, context-assistant,
manual-config, clarify) and _process_tool_call(), which drives one model tool
call through them end to end. The gate-independent base (TurnState, GateOutcome,
the pure arg transforms) lives in chat_types.py, imported and re-exported here
for cli.py / http_chat / the tests. Imports each dependency from its own source
module — one-directional edges (cli -> chat_turn -> chat_types), no cycle.
"""

import json
import os
from typing import List, Optional, Tuple

from shared.display import console, render_vm_specs
from orchestrator.executor_client import execute_tool, API_URL, _VM_TOOLS
from orchestrator.sanitizer.sanitizer import OS_TYPE_ALIASES
from orchestrator.preflight.validator import _preflight_check, _show_preflight_warning
from .context_assistant import check_context

_MC = {"os_type": "linux", "cpu_cores": 2, "memory_mb": 2048, "machine_type": "q35", "uefi": False}
try:
    from shared.executioner.tool_executor import manager, _VM_DEFS
except ImportError:
    manager = None                                                            # type: ignore[assignment]
    _VM_DEFS = {"disk_size_gb": 60, "network_mode": "nat", "disk_bus": "virtio"}

from .chat_types import (  # base types + pure transforms (extracted from this file)
    TurnState, GateOutcome, _is_critical, _build_vm_spec_rows,
    _maybe_enable_custom_mode, _resolve_os_type, _build_pre_gate_result,
)
_CFG            = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_OS_KEYWORDS    = set(_CFG["os_keywords_gate"])
_CONFIRM_YN     = {k: tuple(v) for k, v in _CFG["confirm_yn"].items()}
_CONFIRM_NAME   = {k: tuple(v) for k, v in _CFG["confirm_name"].items()}
_RENDERS_OUTPUT = set(_CFG.get("rendered_tools", []))
_CRITICAL_TOOLS = set(_CFG.get("critical_tools", ["delete_vm"]))   # irreversible → double-confirm
_RECENT_CONTEXT_WINDOW = _CFG["chat"].get("recent_context_window", 6)  # msgs kept for multi-turn context


def _safety_gate(tool_name: str, raw_args: dict, state: "TurnState",
                 messages: List[dict]) -> GateOutcome:
    """Interactive safety confirmation before a mutating tool runs.

    delete_vm double-confirms (YES then the exact name); the reversible y/n tools
    confirm once (and batch within a turn); the name-confirm tools require an
    exact name match. Skipped when the value was already clarified/confirmed this
    turn.

    Returns EXIT (Ctrl-C/EOF), CANCELLED (declined — cancel messages appended and
    state.op_cancelled set), or PROCEED (confirmed or not required).

    Example::

        _safety_gate("delete_vm", {"name": "box"}, state, messages)
        # prompts YES + name; → GateOutcome.PROCEED once both match
    """
    conf_entry = _CONFIRM_YN.get(tool_name) or _CONFIRM_NAME.get(tool_name)
    if not conf_entry:
        return GateOutcome.PROCEED
    field, verb = conf_entry
    proposed = raw_args.get(field, "")
    if (field, proposed) in state.clarified_values or (field, proposed) in state.confirmed_values:
        return GateOutcome.PROCEED

    def cancel() -> None:
        """Append the cancellation messages and mark the operation cancelled."""
        messages.append({
            "role":    "tool",
            "content": json.dumps(
                {"success": False, "error": "Operation cancelled by user."}, default=str),
        })
        messages.append({
            "role":    "user",
            "content": "_INTERNAL_ The user cancelled this operation. Ask what they would like to do instead.",
        })
        state.op_cancelled = True

    if _is_critical(tool_name, raw_args):
        # Double confirm: YES → VM name
        console.print(f"\n[bold red]⚠  {verb}: [bold]{proposed}[/bold] — this will also delete its disk(s)[/bold red]")
        console.print("[dim]Type YES to proceed, or press Enter to cancel.[/dim]")
        try:
            step1 = console.input("[bold red]Confirm (YES):[/bold red] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Cancelled.[/dim]")
            return GateOutcome.EXIT
        if step1.upper() != "YES":
            cancel()
            return GateOutcome.CANCELLED
        console.print(f"[dim]Type the name [bold]{proposed}[/bold] to confirm.[/dim]")
        try:
            step2 = console.input("[bold red]Confirm name:[/bold red] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Cancelled.[/dim]")
            return GateOutcome.EXIT
        if step2 != proposed:
            console.print("[dim]Name did not match. Cancelled.[/dim]")
            cancel()
            return GateOutcome.CANCELLED

    elif tool_name in _CONFIRM_YN:
        # y/n confirm for reversible modify and launch/stop. Batch-skip if this
        # tool type was already confirmed earlier in the same turn.
        if tool_name in state.confirmed_tool_types:
            console.print(f"  [dim]auto-confirmed: {verb}: {proposed}[/dim]")
        else:
            if tool_name == "create_vm":
                render_vm_specs(_build_vm_spec_rows(raw_args))
            hint = f"[bold]{proposed}[/bold]" if proposed else "[dim]unknown[/dim]"
            console.print(f"\n[yellow]⚠  {verb}: {hint}[/yellow]")
            try:
                answer = console.input("[bold cyan]Proceed? (y/n):[/bold cyan] ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Cancelled.[/dim]")
                return GateOutcome.EXIT
            if answer not in ("y", "yes", "1"):
                cancel()
                return GateOutcome.CANCELLED
            state.confirmed_tool_types.add(tool_name)

    else:
        # Name confirm for destructive operations — exact match required
        hint = f"[bold]{proposed}[/bold]" if proposed else "[dim]unknown[/dim]"
        console.print(f"\n[yellow]⚠  {verb}: {hint}[/yellow]")
        console.print(f"[dim]Type the name to confirm, or press Enter to cancel.[/dim]")
        try:
            confirmed = console.input("[bold cyan]Confirm:[/bold cyan] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Cancelled.[/dim]")
            return GateOutcome.EXIT
        if confirmed != proposed:
            if confirmed:
                console.print("[dim]Name did not match. Cancelled.[/dim]")
            cancel()
            return GateOutcome.CANCELLED

    state.confirmed_values.add((field, proposed))   # this exact value confirmed
    return GateOutcome.PROCEED


def _preflight_gate(tool_name: str, raw_args: dict, state: "TurnState",
                    messages: List[dict], verbose: bool) -> Tuple[dict, "GateOutcome"]:
    """Run pre-flight validation and act on the result before execution.

    Returns (raw_args, outcome): EXIT (Ctrl-C), REPLAN (abort — the AI is nudged
    to re-plan), CANCELLED (ask_user declined), or PROCEED (ok / auto_fix /
    ask_user-approved). Mutates raw_args for auto_fix, an ask_user fix_field, and
    the create_profile force flag.

    Example::

        raw_args, out = _preflight_gate("create_vm", {"name": "v"}, st, msgs, False)
        # out is GateOutcome.PROCEED when preflight returns action == "ok"
    """
    pf = _preflight_check(
        tool_name, raw_args,
        manager if API_URL == "local" else None,
        verbose,
        stateless_only=(API_URL != "local"),
    )
    action = pf.get("action", "ok")

    if action == "abort":
        messages.append({
            "role":    "tool",
            "content": json.dumps({"success": False, "error": pf["reason"]}, default=str),
        })
        messages.append({
            "role":    "user",
            "content": (
                f"_INTERNAL_ {pf['reason']}. "
                f"{pf.get('correction', '')} Do not retry this operation."
            ),
        })
        return raw_args, GateOutcome.REPLAN

    if action == "auto_fix":
        raw_args = pf["fixed_args"]
        if not verbose:
            console.print(f"  [yellow]⚙  Pre-flight auto-fixed: {pf['correction']}[/yellow]")

    elif action == "ask_user" and tool_name not in _CONFIRM_NAME:
        _show_preflight_warning(pf, console)
        fix_field = pf.get("fix_field")
        opts      = pf.get("options", [])
        try:
            pf_answer = console.input("[bold cyan]Your choice:[/bold cyan] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Cancelled.[/dim]")
            return raw_args, GateOutcome.EXIT
        cancelled = (
            not pf_answer
            or (opts and pf_answer.lower() == opts[-1].lower())
            or pf_answer.lower() in ("no", "cancel", "n")
        )
        if cancelled:
            messages.append({
                "role":    "tool",
                "content": json.dumps(
                    {"success": False, "error": "Operation cancelled by user."}, default=str),
            })
            messages.append({
                "role":    "user",
                "content": "_INTERNAL_ The user cancelled this operation. Ask what they would like to do instead.",
            })
            state.op_cancelled = True
            return raw_args, GateOutcome.CANCELLED
        if fix_field:
            raw_args = dict(raw_args)
            raw_args[fix_field] = pf_answer
            state.clarified_fields.add(fix_field)
        elif tool_name == "create_profile":
            # User approved "Save anyway" — bypass the executor's duplicate preflight.
            raw_args = dict(raw_args)
            raw_args["force"] = True

    # After the CLI handled preflight for create_profile (ok / auto_fix /
    # ask_user-approved), mark force=True so the executor skips its own preflight.
    if tool_name == "create_profile" and action in ("ok", "auto_fix"):
        raw_args = dict(raw_args)
        raw_args["force"] = True
    return raw_args, GateOutcome.PROCEED


def _context_assistant_gate(tool_name: str, raw_args: dict, user_input: str,
                            recent_context: str, state: "TurnState",
                            messages: List[dict]) -> Tuple[dict, "GateOutcome"]:
    """Fire the context assistant (once per turn) and act on its hint.

    A hallucinated required field ("never mentioned it") is asked of the user
    directly and patched into raw_args (PROCEED); a tool mismatch / high-stakes
    hint pops the bad assistant message and nudges the AI to re-plan (REPLAN).

    Returns (raw_args, outcome): EXIT (Ctrl-C), REPLAN (mismatch), or PROCEED
    (patched / no hint / already fired this turn).

    Example::

        _context_assistant_gate("delete_vm", {"name": "x"}, "show x", "", st, msgs)
        # mismatch hint → (raw_args, GateOutcome.REPLAN)
    """
    if state.context_assistant_fired:
        return raw_args, GateOutcome.PROCEED
    known_names = None
    if tool_name in _VM_TOOLS:
        known_names = {v["name"] for v in execute_tool("list_vms", {}, verbose=True, log=False)}
    hint = check_context(user_input, tool_name, raw_args, recent_context=recent_context,
                          known_names=known_names)
    if not hint:
        return raw_args, GateOutcome.PROCEED
    state.context_assistant_fired = True
    if "never mentioned it" in hint:
        # Hallucinated required field — ask the user directly (the model ignores
        # the hint if we just re-prompt it).
        import re as _re
        fields = _re.findall(r"You set (\w+)=", hint)
        filled = {}
        for f in fields:
            console.print(f"[yellow]?[/yellow] What {f} would you like to use?")
            try:
                ans = console.input("[bold cyan]You:[/bold cyan] ").strip()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Cancelled.[/dim]")
                return raw_args, GateOutcome.EXIT
            if ans:
                filled[f] = ans
        if filled:
            raw_args = dict(raw_args)
            raw_args.update(filled)
            messages.append({"role": "user", "content": str(filled)})
            state.clarified_fields.update(filled.keys())
            state.clarified_values.update(filled.items())
        return raw_args, GateOutcome.PROCEED   # continue with the corrected args
    # Mismatch or high-stakes — let the AI re-evaluate.
    messages.pop()
    messages.append({
        "role":    "user",
        "content": f"_INTERNAL_ {hint} Re-evaluate and call the correct tool.",
    })
    return raw_args, GateOutcome.REPLAN


def _manual_config_gate(tool_name: str, raw_args: dict, pre_gate_result: Optional[dict],
                        state: "TurnState") -> Tuple[dict, Optional[dict], "GateOutcome"]:
    """Interactive per-VM config when create_vm was called with manual=True.

    Prompts for os/cpu/mem/disk, applies them, marks os_type clarified, and
    clears the pre-gate result (manual config owns the missing fields). Returns
    (raw_args, pre_gate_result, outcome): EXIT (Ctrl-C) or PROCEED.

    Example::

        _manual_config_gate("create_vm", {"name": "v", "manual": True}, None, st)
        # prompts for config → (raw_args_without_manual, None, GateOutcome.PROCEED)
    """
    if tool_name == "create_vm" and raw_args.get("manual"):
        raw_args = dict(raw_args)
        raw_args.pop("manual", None)
        def_os   = raw_args.get("os_type", "linux")
        def_cpu  = raw_args.get("cpu_cores", 2)
        def_mem  = raw_args.get("memory_mb", 4096)
        def_disk = raw_args.get("disk_size_gb", 20)
        console.print(
            f"\n  [cyan]Configuring [bold]{raw_args.get('name')}[/bold]"
            f"  [{def_os} | {def_cpu} CPU | {def_mem} MB | {def_disk} GB][/cyan]"
        )
        console.print("  [dim]Press Enter for defaults, or specify: e.g. 'windows, 8GB, 4 CPU, 50GB'[/dim]")
        try:
            man_input = console.input("[bold cyan]  Config:[/bold cyan] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Cancelled.[/dim]")
            return raw_args, pre_gate_result, GateOutcome.EXIT
        if man_input:
            import re as _re
            for kw in _OS_KEYWORDS:
                if kw in man_input.split():
                    raw_args["os_type"] = OS_TYPE_ALIASES.get(kw, kw)
                    break
            m = _re.search(r'(\d+)\s*gb(?!\s*disk)', man_input)
            if m:
                raw_args["memory_mb"] = int(m.group(1)) * 1024
            m = _re.search(r'(\d+)\s*mb', man_input)
            if m:
                raw_args["memory_mb"] = int(m.group(1))
            m = _re.search(r'(\d+)\s*(?:cpu|core)', man_input)
            if m:
                raw_args["cpu_cores"] = int(m.group(1))
            m = _re.search(r'(\d+)\s*gb\s*disk', man_input)
            if m:
                raw_args["disk_size_gb"] = int(m.group(1))
        # Manual config owns os_type — give it a value and mark it clarified so
        # the pre-gate doesn't re-ask; then drop the pre-gate result entirely.
        if not raw_args.get("os_type"):
            raw_args["os_type"] = def_os
        state.clarified_fields.add("os_type")
        state.clarified_values.add(("os_type", raw_args["os_type"]))
        pre_gate_result = None
        state.confirmed_tool_types.discard("create_vm")   # each VM needs its own config
    elif tool_name == "create_vm" and "manual" in raw_args:
        raw_args = dict(raw_args)
        raw_args.pop("manual", None)
    return raw_args, pre_gate_result, GateOutcome.PROCEED


def _clarify_drain(result: dict, tool_name: str, state: "TurnState",
                   messages: List[dict]) -> GateOutcome:
    """Drain a clarify response: prompt for each missing field (or a verbatim
    clarify answer / the overwrite shortcut), update state, inject the re-plan
    message. Returns EXIT (Ctrl-C) or SKIP_TOOL (drained — caller breaks the tool
    loop; the post-loop re-plans with the answers).

    Example::

        _clarify_drain({"clarify": True, "needs_clarification": "name",
                        "question": "Name?"}, "create_vm", state, msgs)
        # → GateOutcome.SKIP_TOOL after recording the answer
    """
    # Drain ALL missing fields in one pass — no Ollama round-trip per field.
    filled: dict = {}
    missing_fields = result.get("missing") or [{
        "field":    result.get("needs_clarification", ""),
        "question": result.get("question", "Please provide more detail."),
        "options":  result.get("options", []),
    }]
    for mf in missing_fields:
        q    = mf["question"]
        opts = mf["options"]
        f    = mf["field"]

        # No field to fill. Two distinct cases:
        #
        # 1. tool_name == "clarify": the AI asked the user a question
        #    (e.g. "Did you mean 'loq'?"). Pass the answer back verbatim
        #    so the AI decides the next step — don't override intent.
        #
        # 2. tool_name != "clarify": executor returned a "Save anyway /
        #    Cancel" prompt. Tell the AI to retry with force=true.
        if not f:
            if opts:
                console.print(
                    f"[yellow]?[/yellow] {q}  "
                    + "  ".join(f"[{o}]" for o in opts)
                )
            else:
                console.print(f"[yellow]?[/yellow] {q}")
            try:
                _conf = console.input("[bold cyan]You:[/bold cyan] ").strip()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Goodbye.[/dim]")
                return GateOutcome.EXIT
            _cancelled = (
                not _conf
                or (opts and _conf.lower() == opts[-1].lower())
                or _conf.lower() in ("no", "cancel", "n")
            )
            if tool_name == "clarify":
                # AI-initiated question — return the answer verbatim
                if _conf:
                    filled[f] = _conf
                    messages.append({"role": "user", "content": _conf})
            elif _cancelled:
                messages.append({"role": "user", "content": "_INTERNAL_ The user cancelled. Do not retry this operation."})
                state.op_cancelled = True
            else:
                hint = result.get("hint", "")
                messages.append({"role": "user", "content": _conf})
                messages.append({"role": "user", "content": f"_INTERNAL_ The user confirmed. {hint} Keep ALL original arguments exactly as they were."})
            state.clarify_happened = True
            state.clarify_answer   = _conf
            state.clarify_field    = ""
            break

        if opts:
            console.print(
                f"[yellow]?[/yellow] {q}  "
                + "  ".join(f"[{o}]" for o in opts)
            )
        else:
            console.print(f"[yellow]?[/yellow] {q}")
        try:
            clarified = console.input("[bold cyan]You:[/bold cyan] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye.[/dim]")
            return GateOutcome.EXIT
        if clarified:
            # Overwrite shortcut: user said "overwrite" for a name conflict.
            if f == "name" and "overwrite" in clarified.lower():
                orig = result.get("original_name", "")
                if orig:
                    filled["name"]      = orig
                    filled["overwrite"] = "true"
                    messages.append({"role": "user", "content": clarified})
                    messages.append({
                        "role":    "user",
                        "content": f"_INTERNAL_ The user chose to overwrite. Call create_vm again with name='{orig}' and overwrite=true, keeping ALL other original arguments exactly as they were.",
                    })
                    state.clarified_fields.update(filled.keys())
                    state.clarified_values.update(filled.items())
                    state.clarify_happened = True
                    state.clarify_answer   = clarified
                    state.clarify_field    = "overwrite"
                    break
            filled[f] = clarified
            messages.append({"role": "user", "content": clarified})
            # If the user named a specific distro, inject os_name so the
            # executor can auto-find the matching ISO.
            if f == "os_type" and clarified.lower().strip() in OS_TYPE_ALIASES:
                filled["os_name"] = clarified.lower().strip()
    state.clarified_fields.update(filled.keys())
    state.clarified_values.update(filled.items())
    if filled:
        _field_summary = ", ".join(f"{k}='{v}'" for k, v in filled.items())
        _iso_hint = (
            " The user named a specific distro — you MUST call scan_isos first,"
            f" then pass the matching ISO path as iso_path in create_vm."
            if "os_name" in filled else ""
        )
        messages.append({
            "role":    "user",
            "content": f"_INTERNAL_ The user provided the missing values: {_field_summary}. Call the correct tool using these EXACT values — do not invent different ones.{_iso_hint}",
        })
    state.clarify_happened = True
    state.clarify_answer   = str(filled)
    state.clarify_field    = ", ".join(filled.keys())
    return GateOutcome.SKIP_TOOL


def _process_tool_call(tc: dict, user_input: str, ui: str, state: "TurnState",
                       messages: List[dict], verbose: bool) -> "GateOutcome":
    """Run one model tool call through every gate, then execute it.

    Drives a single tool_call dict through custom-mode detection, the context
    assistant, os_type resolution, pre-flight, the safety confirmation, the
    manual-config prompt, execution, output rendering, and clarify draining —
    appending the tool-result message to ``messages`` along the way.

    Returns a GateOutcome the caller acts on:
        PROCEED            → move to the next tool call in this round
        EXIT               → user asked to quit; caller returns from chat_loop
        REPLAN / CANCELLED → stop this round; caller breaks the tool loop and
                             lets the post-round logic (op_cancelled / clarify /
                             context-assistant) run

    Example::

        out = _process_tool_call(tc, "make a vm", "make a vm", state, msgs, False)
        if out is GateOutcome.EXIT:   return
        if out is not GateOutcome.PROCEED:  break
    """
    fn        = tc.get("function", {})
    tool_name = fn.get("name", "")
    raw_args  = fn.get("arguments", {})
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except Exception:
            raw_args = {}

    if verbose:
        console.print(
            f"  [tool]→ {tool_name}[/tool]  [dim]{json.dumps(raw_args)}[/dim]"
        )

    # ── Custom mode: "custom" in prompt disables HTTP check for profiles ──
    _maybe_enable_custom_mode(tool_name, ui, messages)

    # ── Context assistant ──────────────────────────────────────
    # Only runs once per user turn — if it already fired and the
    # AI still chose a bad tool, let the downstream layers handle it.
    #
    # _recent_context: last 6 real user messages joined into one
    # string. Used by the context assistant and the pre-gate so
    # multi-turn flows ("delete test1" → "yes") don't lose the
    # entity name when only the confirmation arrives as user_input.
    _recent_user_msgs = [
        m.get("content", "").lower() for m in messages
        if m.get("role") == "user"
        and not str(m.get("content", "")).startswith("_INTERNAL_")
    ]
    _recent_context = " ".join(_recent_user_msgs[-_RECENT_CONTEXT_WINDOW:])
    raw_args, _ca_out = _context_assistant_gate(
        tool_name, raw_args, user_input, _recent_context, state, messages)
    if _ca_out is GateOutcome.EXIT:
        return GateOutcome.EXIT
    if _ca_out is GateOutcome.REPLAN:
        return GateOutcome.REPLAN

    # ── os_type guard ──────────────────────────────────────────
    raw_args = _resolve_os_type(tool_name, raw_args, ui, state)

    # ── Pre-flight check ───────────────────────────────────────
    raw_args, _pf_out = _preflight_gate(tool_name, raw_args, state, messages, verbose)
    if _pf_out is GateOutcome.EXIT:
        return GateOutcome.EXIT
    if _pf_out is not GateOutcome.PROCEED:   # REPLAN (abort) or CANCELLED
        return _pf_out

    # ── Safety confirmation gate ───────────────────────────────
    safety_out = _safety_gate(tool_name, raw_args, state, messages)
    if safety_out is GateOutcome.EXIT:
        return GateOutcome.EXIT
    if safety_out is GateOutcome.CANCELLED:
        return GateOutcome.CANCELLED

    # ── Pre-execution gate ─────────────────────────────────────────
    _pre_gate_result = _build_pre_gate_result(
        tool_name, raw_args, user_input, _recent_context, state
    )

    # ── Manual per-VM config prompt ────────────────────────────────
    raw_args, _pre_gate_result, _mc_out = _manual_config_gate(
        tool_name, raw_args, _pre_gate_result, state)
    if _mc_out is GateOutcome.EXIT:
        return GateOutcome.EXIT

    if _pre_gate_result:
        result = _pre_gate_result
    else:
        result = execute_tool(tool_name, raw_args, verbose)
        state.tool_executed = True

    # Remote VNC launch — render connection panel and strip from tool result.
    if (
        not verbose
        and isinstance(result, dict)
        and result.get("success")
        and result.get("vnc_connect_cmd")
    ):
        from shared.display import render_vnc_connect
        render_vnc_connect(console, result)
        result = {
            "success": True, "name": result.get("name"), "display": "vnc",
            "rendered": True,
            "note": "VM launched via VNC. Connection panel shown to user. Do not repeat the commands.",
        }
        tool_content = json.dumps(result, default=str)

    # Tools that self-render formatted output: strip data so the AI
    # doesn't repeat the table/panel in its text response.
    elif tool_name in _RENDERS_OUTPUT and not verbose and not _pre_gate_result:
        tool_content = json.dumps(
            {"success": True, "rendered": True,
             "note": "Output already displayed to user. Do not repeat it."},
            default=str,
        )
    else:
        tool_content = json.dumps(result, default=str)

    messages.append({
        "role":    "tool",
        "content": tool_content,
    })

    if isinstance(result, dict) and result.get("clarify"):
        if _clarify_drain(result, tool_name, state, messages) is GateOutcome.EXIT:
            return GateOutcome.EXIT
        return GateOutcome.REPLAN

    return GateOutcome.PROCEED


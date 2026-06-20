"""
cli.py — CLI Entry Point and Chat Loop Layer

Provides the interactive AI chat loop and the direct sub-command CLI
(qemu-api list, launch, stop, etc.). This is the main entry point
for both modes; ollama_wrapper.py is a thin shim that re-exports from here.
"""

import http.server
import json
import os
import socket
import sys
import threading
from typing import List

from rich import box
from rich.panel import Panel
from rich.table import Table

from api.qemu_config  import _MC, OVMF, check_profile_compatibility, check_system_capabilities, get_all_profiles, list_profiles
from .session      import AUTO_CLEAR_SESSION, clear_session, detect_drift, load_session, save_session, set_auto_clear, set_loop_max, get_loop_max
from .display      import (
    console,
    _print_banner, _render_compat, _render_monitor, _render_profiles,
    _render_snapshots, _render_status, _render_system, _render_vm_list, _render_vm_specs,
)
from .fingerprint        import _tf_report
from .ollama_client      import OLLAMA_MODEL, OLLAMA_URL, _call_ollama
from .context_assistant  import check_context, extract_slots
from sanitizer.context_gate import _REQUIRED as _GATE_REQUIRED
from sanitizer.sanitizer    import OS_TYPE_ALIASES
from executioner.tool_executor import execute_tool, manager, _VM_DEFS
from preflight.validator    import set_custom_mode, _preflight_check, _show_preflight_warning

_CFG            = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_EXIT_CMDS      = set(_CFG["exit_commands"])
_SHORTCUTS      = _CFG["shortcut_commands"]
_LOOP_MAX       = get_loop_max()   # respects tool_loop_max_override if set
_ACTION_WORDS   = set(_CFG["action_words"])
_OS_KEYWORDS    = set(_CFG["os_keywords_gate"])
_CONFIRM_YN     = {k: tuple(v) for k, v in _CFG["confirm_yn"].items()}
_CONFIRM_NAME   = {k: tuple(v) for k, v in _CFG["confirm_name"].items()}
_RENDERS_OUTPUT = set(_CFG.get("rendered_tools", []))

def _is_critical(tool_name: str, args: dict) -> bool:
    """True when the operation requires double confirmation (irreversible + data loss)."""
    return tool_name == "delete_vm"


# Builds (label, value) rows describing the specs create_vm is about to use,
# falling back to the same defaults the executor applies so the preview
# matches what will actually be created.
# In: dict args → Out: List[tuple[str, str]]
def _build_vm_spec_rows(args: dict) -> list:
    name    = args.get("name") or "?"
    os_type = args.get("os_type") or _MC["os_type"]
    os_name = args.get("os_name") or ""

    # Suppress profile when user explicitly set SMBIOS fingerprinting fields —
    # mirrors the same logic in tool_executor so the preview matches what gets created.
    _manual_smbios = any(args.get(f) for f in ("serial_number", "bios_vendor", "chassis_type", "smbios_type"))
    profile = "" if _manual_smbios else (args.get("profile") or "")
    if not profile and not _manual_smbios:
        product = (args.get("product_name", "") + " " + args.get("manufacturer", "")).lower()
        for pname, pdata in get_all_profiles().items():
            pp = (pdata.get("product_name", "") + " " + pdata.get("manufacturer", "")).lower()
            if any(kw in pp for kw in product.split() if len(kw) > 3):
                profile = pname
                break

    _pdata = get_all_profiles().get(profile, {}) if profile else {}

    cpu_cores    = args.get("cpu_cores")    or _pdata.get("cpu_cores")    or _MC["cpu_cores"]
    memory_mb    = int(args.get("memory_mb") or _pdata.get("memory_mb")    or _MC["memory_mb"])
    machine_type = args.get("machine_type") or _pdata.get("machine_type") or _MC["machine_type"]
    if args.get("hardened"):
        machine_type = "q35"
    disk_gb      = args.get("disk_size_gb") or _VM_DEFS["disk_size_gb"]
    _DISK_BUS_VALUES = {"sata", "nvme", "scsi", "ide", "virtio"}
    _raw_fmt     = args.get("disk_format") or ""
    disk_bus_preview = args.get("disk_bus") or (_raw_fmt if _raw_fmt.lower() in _DISK_BUS_VALUES else "") or _VM_DEFS.get("disk_bus", "virtio")
    disk_fmt     = disk_bus_preview
    net_mode     = args.get("network_mode") or _VM_DEFS["network_mode"]
    iso_path     = args.get("iso_path") or ""

    is_windows = "windows" in str(os_type).lower() or "windows" in str(os_name).lower()
    uefi       = True if is_windows else bool(args.get("uefi", _pdata.get("uefi", _MC["uefi"])))
    if is_windows:
        machine_type = "q35"

    rows = [
        ("Name", name),
        ("OS",   f"{os_type}" + (f" ({os_name})" if os_name else "")),
    ]
    if profile:
        rows.append(("Profile", f"{profile}" + (f"  ({_pdata.get('description')})" if _pdata.get("description") else "")))
    rows += [
        ("CPU Cores", str(cpu_cores)),
        ("Memory",    f"{memory_mb // 1024} GB" if memory_mb >= 1024 else f"{memory_mb} MB"),
        ("Disk",      f"{disk_gb} GB ({disk_fmt})"),
        ("Network",   net_mode),
        ("Machine",   f"{machine_type}  {'UEFI' if uefi else 'BIOS'}"),
        ("ISO",       iso_path if iso_path else "[dim]auto-detect / none[/dim]"),
    ]
    return rows


def _show_stealth_popup(vm_name: str, setup_cmd: str) -> None:
    import platform
    import subprocess
    is_win_guest = setup_cmd.startswith("irm ")
    if is_win_guest:
        how    = "Open PowerShell inside the VM and run:"
        reboot = "No reboot required."
    else:
        how    = "Open a terminal inside the VM and run:"
        reboot = "Then reboot the VM."
    text = (
        f"Stealth VM \"{vm_name}\" needs one-time guest setup.\n\n"
        f"{how}\n\n"
        f"  {setup_cmd}\n\n"
        f"{reboot}\n\n"
        f"When done, run on the host:\n"
        f"  qemu-api setup-done {vm_name}"
    )
    title = f"Stealth Setup: {vm_name}"

    # ── Windows host ──────────────────────────────────────────────────────────
    if platform.system() == "Windows":
        try:
            import ctypes
            # Run in a daemon thread so the CLI doesn't block on the dialog
            threading.Thread(
                target=lambda: ctypes.windll.user32.MessageBoxW(0, text, title, 0x40),
                daemon=True,
            ).start()
            return
        except Exception:
            pass

    # ── Linux/macOS host: zenity first (GNOME/Cinnamon) ──────────────────────
    try:
        subprocess.Popen([
            "zenity", "--info",
            f"--title={title}",
            f"--text={text}",
            "--width=520",
            "--no-wrap",
        ])
        return
    except FileNotFoundError:
        pass
    # notify-send (desktop notification, non-blocking)
    try:
        subprocess.Popen([
            "notify-send", title, setup_cmd,
            "--urgency=critical", "--expire-time=0",
        ])
        return
    except FileNotFoundError:
        pass
    # tkinter (universal fallback)
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(title, text)
        root.destroy()
    except Exception:
        pass


def _show_drift_report(messages: list, runtime_drift_count: int) -> None:
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

# Runs the interactive Ollama chat REPL: reads input, drives the agentic tool loop (up to 15 rounds), handles clarifications, and saves session.
# In: bool verbose → Out: nothing (blocks until exit)
def chat_loop(verbose: bool = False):
    global _LOOP_MAX
    _print_banner(
        verbose=verbose,
        ollama_url=OLLAMA_URL,
        ollama_model=OLLAMA_MODEL,
        ovmf_available=OVMF["available"],
        ovmf_code=OVMF.get("code", ""),
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

        if _ui in _SHORTCUTS["list"]:
            result = execute_tool("list_vms", {}, verbose)
            continue

        if _ui in _SHORTCUTS["system"]:
            execute_tool("check_system", {}, verbose)
            continue

        if _ui in _SHORTCUTS["profiles"]:
            execute_tool("list_profiles", {}, verbose)
            continue

        if _ui in _SHORTCUTS["clear_session"]:
            clear_session()
            messages = []
            console.print("[dim]Session cleared.[/dim]")
            continue

        if _ui in _SHORTCUTS["drift"]:
            _show_drift_report(messages, _runtime_drift_count)
            continue

        if _ui in _SHORTCUTS["auto_clear_on"]:
            set_auto_clear(True)
            console.print("[dim]Auto-clear enabled — session will be cleared on next start.[/dim]")
            continue

        if _ui in _SHORTCUTS["auto_clear_off"]:
            set_auto_clear(False)
            console.print("[dim]Auto-clear disabled.[/dim]")
            continue

        _ll_matched = next((s for s in _SHORTCUTS["loop_limit"] if _ui == s or _ui.startswith(s + " ")), None)
        if _ll_matched is not None:
            _ll_inline = _ui[len(_ll_matched):].strip()
            if _ll_inline:
                _ll_input = _ll_inline
            else:
                console.print(f"[dim]Current tool loop limit: [bold]{_LOOP_MAX}[/bold] (default: {_CFG['chat']['tool_loop_max']})[/dim]")
                console.print("[dim]Enter a number to set a new limit, or press Enter to clear the override.[/dim]")
                try:
                    _ll_input = console.input("[bold cyan]New limit:[/bold cyan] ").strip()
                except (KeyboardInterrupt, EOFError):
                    continue
            if _ll_input == "":
                set_loop_max(None)
                _LOOP_MAX = _CFG["chat"]["tool_loop_max"]
                console.print(f"[dim]Loop limit reset to default ({_LOOP_MAX}).[/dim]")
            elif _ll_input.isdigit() and int(_ll_input) > 0:
                _LOOP_MAX = int(_ll_input)
                set_loop_max(_LOOP_MAX)
                console.print(f"[dim]Loop limit set to {_LOOP_MAX}.[/dim]")
            else:
                console.print("[dim]Invalid input — loop limit unchanged.[/dim]")
            continue

        if not _is_synthetic:
            messages.append({"role": "user", "content": user_input})

        _user_wants_action = bool(set(_ui.split()) & _ACTION_WORDS)
        _tools_called_this_turn   = False
        _just_clarified_fields: set = set()  # field names answered via clarify gate this turn
        _just_clarified_values: set = set()  # (field, value) pairs answered via clarify gate
        _confirmed_values: set = set()       # (field, value) pairs confirmed via safety gate
        _confirmed_tool_types: set = set()  # tool names batch-confirmed this turn
        _context_assistant_fired  = False
        _tool_executed_this_turn  = False    # True only when execute_tool actually ran
        _last_had_tools           = False    # True when last Ollama response had tool_calls

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
            _last_had_tools = bool(tool_calls)
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
                if _user_wants_action and not _tools_called_this_turn and _loop_iter < _LOOP_MAX - 1:
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

            _tools_called_this_turn = True

            _clarify_happened = False
            _clarify_answer   = ""
            _clarify_field    = ""
            _op_cancelled     = False

            for tc in tool_calls:
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
                if tool_name == "create_profile":
                    _profile_ctx = _ui + " " + " ".join(
                        m.get("content", "").lower() for m in messages[-6:]
                        if m.get("role") == "user"
                    )
                    if "custom" in _profile_ctx:
                        set_custom_mode(True)
                        console.print("[dim]Custom mode active — product verification disabled[/dim]")

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
                _recent_context = " ".join(_recent_user_msgs[-6:])
                if not _context_assistant_fired:
                    _ca_hint = check_context(user_input, tool_name, raw_args,
                                             recent_context=_recent_context)
                    if _ca_hint:
                        _context_assistant_fired = True
                        if "never mentioned it" in _ca_hint:
                            # Hallucinated required field — ask the user directly
                            # rather than re-prompting the AI (model ignores the hint).
                            import re as _re
                            _fields = _re.findall(r"You set (\w+)=", _ca_hint)
                            _filled = {}
                            for _f in _fields:
                                console.print(f"[yellow]?[/yellow] What {_f} would you like to use?")
                                try:
                                    _ans = console.input("[bold cyan]You:[/bold cyan] ").strip()
                                except (KeyboardInterrupt, EOFError):
                                    console.print("\n[dim]Cancelled.[/dim]")
                                    return
                                if _ans:
                                    _filled[_f] = _ans
                            if _filled:
                                raw_args = dict(raw_args)
                                raw_args.update(_filled)
                                messages.append({"role": "user", "content": str(_filled)})
                                _just_clarified_fields.update(_filled.keys())
                                _just_clarified_values.update(_filled.items())
                            # Don't break — continue with corrected args
                        else:
                            # Mismatch or high-stakes — let the AI re-evaluate
                            messages.pop()
                            messages.append({
                                "role":    "user",
                                "content": f"_INTERNAL_ {_ca_hint} Re-evaluate and call the correct tool.",
                            })
                            break       # break tool_calls loop, re-enter outer loop
                # ──────────────────────────────────────────────────────────

                # ── os_type guard ──────────────────────────────────────────
                # When the user names an OS (e.g. "mint"), resolve it to the
                # canonical type (e.g. "linux") and set it directly — don't
                # let the AI guess from session history.  When no OS is
                # mentioned, strip any AI-inferred value so the gate can ask.
                if tool_name == "create_vm" \
                        and "os_type" not in _just_clarified_fields:
                    _ui_tokens = {t.strip('.,!?;:') for t in _ui.split()}
                    _matched_kw = next(iter(_OS_KEYWORDS & _ui_tokens), None)
                    if _matched_kw:
                        _canonical = OS_TYPE_ALIASES.get(_matched_kw, _matched_kw)
                        raw_args = dict(raw_args)
                        raw_args["os_type"] = _canonical
                        _just_clarified_fields.add("os_type")
                        _just_clarified_values.add(("os_type", _canonical))
                    elif "os_type" in raw_args:
                        raw_args.pop("os_type")
                # ──────────────────────────────────────────────────────────

                # ── Pre-flight check ───────────────────────────────────────
                _pf        = _preflight_check(tool_name, raw_args, manager, verbose)
                _pf_action = _pf.get("action", "ok")

                if _pf_action == "abort":
                    messages.append({
                        "role":    "tool",
                        "content": json.dumps(
                            {"success": False, "error": _pf["reason"]}, default=str
                        ),
                    })
                    messages.append({
                        "role":    "user",
                        "content": (
                            f"_INTERNAL_ {_pf['reason']}. "
                            f"{_pf.get('correction', '')} Do not retry this operation."
                        ),
                    })
                    break

                elif _pf_action == "auto_fix":
                    raw_args = _pf["fixed_args"]
                    if not verbose:
                        console.print(
                            f"  [yellow]⚙  Pre-flight auto-fixed: {_pf['correction']}[/yellow]"
                        )

                elif _pf_action == "ask_user" and tool_name not in _CONFIRM_NAME:
                    _show_preflight_warning(_pf, console)
                    fix_field = _pf.get("fix_field")
                    opts      = _pf.get("options", [])
                    try:
                        pf_answer = console.input("[bold cyan]Your choice:[/bold cyan] ").strip()
                    except (KeyboardInterrupt, EOFError):
                        console.print("\n[dim]Cancelled.[/dim]")
                        return
                    cancelled = (
                        not pf_answer
                        or (opts and pf_answer.lower() == opts[-1].lower())
                        or pf_answer.lower() in ("no", "cancel", "n")
                    )
                    if cancelled:
                        messages.append({
                            "role":    "tool",
                            "content": json.dumps(
                                {"success": False, "error": "Operation cancelled by user."},
                                default=str,
                            ),
                        })
                        messages.append({
                            "role":    "user",
                            "content": "_INTERNAL_ The user cancelled this operation. Ask what they would like to do instead.",
                        })
                        _op_cancelled = True
                        break
                    if fix_field:
                        raw_args = dict(raw_args)
                        raw_args[fix_field] = pf_answer
                        _just_clarified_fields.add(fix_field)
                    elif tool_name == "create_profile":
                        # User approved "Save anyway" — bypass the executor's
                        # duplicate preflight so we don't double-prompt.
                        raw_args = dict(raw_args)
                        raw_args["force"] = True

                # After the CLI has handled preflight for create_profile (ok,
                # auto_fix, or ask_user-approved), always mark force=True so the
                # executor skips its own duplicate preflight check entirely.
                if tool_name == "create_profile" and _pf_action in ("ok", "auto_fix"):
                    raw_args = dict(raw_args)
                    raw_args["force"] = True
                # ──────────────────────────────────────────────────────────

                # ── Safety confirmation gate ───────────────────────────────
                # Skip if the key field was answered via the clarify gate this
                # turn — the user just confirmed the value moments ago.
                _conf_entry = (
                    _CONFIRM_YN.get(tool_name) or _CONFIRM_NAME.get(tool_name)
                )
                if _conf_entry:
                    field, verb = _conf_entry
                    proposed = raw_args.get(field, "")
                if _conf_entry and (field, proposed) not in _just_clarified_values and (field, proposed) not in _confirmed_values:

                    def _cancel_op():
                        messages.append({
                            "role":    "tool",
                            "content": json.dumps(
                                {"success": False, "error": "Operation cancelled by user."},
                                default=str,
                            ),
                        })
                        messages.append({
                            "role":    "user",
                            "content": "_INTERNAL_ The user cancelled this operation. Ask what they would like to do instead.",
                        })

                    if _is_critical(tool_name, raw_args):
                        # Double confirm: YES → VM name
                        console.print(f"\n[bold red]⚠  {verb}: [bold]{proposed}[/bold] — this will also delete its disk(s)[/bold red]")
                        console.print("[dim]Type YES to proceed, or press Enter to cancel.[/dim]")
                        try:
                            step1 = console.input("[bold red]Confirm (YES):[/bold red] ").strip()
                        except (KeyboardInterrupt, EOFError):
                            console.print("\n[dim]Cancelled.[/dim]")
                            return
                        if step1.upper() != "YES":
                            _cancel_op()
                            _op_cancelled = True
                            break
                        console.print(f"[dim]Type the name [bold]{proposed}[/bold] to confirm.[/dim]")
                        try:
                            step2 = console.input("[bold red]Confirm name:[/bold red] ").strip()
                        except (KeyboardInterrupt, EOFError):
                            console.print("\n[dim]Cancelled.[/dim]")
                            return
                        if step2 != proposed:
                            console.print("[dim]Name did not match. Cancelled.[/dim]")
                            _cancel_op()
                            _op_cancelled = True
                            break

                    elif tool_name in _CONFIRM_YN:
                        # y/n confirm for reversible modify and launch/stop.
                        # If this tool type was already confirmed earlier in the
                        # same turn (batch), skip re-prompting.
                        if tool_name in _confirmed_tool_types:
                            console.print(f"  [dim]auto-confirmed: {verb}: {proposed}[/dim]")
                        else:
                            if tool_name == "create_vm":
                                _render_vm_specs(_build_vm_spec_rows(raw_args))
                            hint = f"[bold]{proposed}[/bold]" if proposed else "[dim]unknown[/dim]"
                            console.print(f"\n[yellow]⚠  {verb}: {hint}[/yellow]")
                            try:
                                answer = console.input("[bold cyan]Proceed? (y/n):[/bold cyan] ").strip().lower()
                            except (KeyboardInterrupt, EOFError):
                                console.print("\n[dim]Cancelled.[/dim]")
                                return
                            if answer not in ("y", "yes", "1"):
                                _cancel_op()
                                _op_cancelled = True
                                break
                            _confirmed_tool_types.add(tool_name)

                    else:
                        # Name confirm for destructive operations — exact match required
                        hint = f"[bold]{proposed}[/bold]" if proposed else "[dim]unknown[/dim]"
                        console.print(f"\n[yellow]⚠  {verb}: {hint}[/yellow]")
                        console.print(f"[dim]Type the name to confirm, or press Enter to cancel.[/dim]")
                        try:
                            confirmed = console.input("[bold cyan]Confirm:[/bold cyan] ").strip()
                        except (KeyboardInterrupt, EOFError):
                            console.print("\n[dim]Cancelled.[/dim]")
                            return
                        if confirmed != proposed:
                            if confirmed:
                                console.print("[dim]Name did not match. Cancelled.[/dim]")
                            _cancel_op()
                            _op_cancelled = True
                            break

                    _confirmed_values.add((field, proposed))  # this exact value confirmed
                # ──────────────────────────────────────────────────────────

                # ── Pre-execution gate ─────────────────────────────────────────
                # Check required trackable fields against what the user actually
                # said — not what the AI put in args. If any gated field is
                # absent from the user's message and hasn't been clarified this
                # turn, jump straight to clarify without calling execute_tool.
                # This prevents hallucinated args from bypassing the gate.
                _gate_required = _GATE_REQUIRED.get(tool_name, [])
                _pre_gate_result = None
                if _gate_required and tool_name != "clarify":
                    _user_slots = extract_slots(user_input)
                    for _clf in _just_clarified_fields:
                        if _clf in raw_args and raw_args[_clf]:
                            _user_slots[_clf] = raw_args[_clf]
                    _missing_early = [
                        {"field": f, "question": q, "options": opts}
                        for f, q, opts in _gate_required
                        if f in _user_slots and _user_slots[f] is None
                        # Skip if the AI's value for this field is grounded in
                        # recent conversation history — handles multi-turn flows
                        # where the entity ("test1") was named in a prior turn
                        # and the current message is only a confirmation ("yes").
                        and not (
                            raw_args.get(f)
                            and isinstance(raw_args.get(f), str)
                            and (
                                raw_args[f].lower() in _recent_context
                                # "test1" grounded in context even when user wrote "test 1"
                                or raw_args[f].lower().replace(" ", "") in _recent_context.replace(" ", "")
                            )
                        )
                    ]
                    if _missing_early:
                        _pre_gate_result = {
                            "success":             False,
                            "clarify":             True,
                            "missing":             _missing_early,
                            "question":            _missing_early[0]["question"],
                            "options":             _missing_early[0]["options"],
                            "needs_clarification": _missing_early[0]["field"],
                            "error":               (
                                f"Missing required arguments for {tool_name}: "
                                f"{[m['field'] for m in _missing_early]}"
                            ),
                        }
                # ──────────────────────────────────────────────────────────────

                # ── Manual per-VM config prompt ────────────────────────────────
                if tool_name == "create_vm" and raw_args.get("manual"):
                    raw_args = dict(raw_args)
                    raw_args.pop("manual", None)
                    _def_os   = raw_args.get("os_type", "linux")
                    _def_cpu  = raw_args.get("cpu_cores", 2)
                    _def_mem  = raw_args.get("memory_mb", 4096)
                    _def_disk = raw_args.get("disk_size_gb", 20)
                    console.print(
                        f"\n  [cyan]Configuring [bold]{raw_args.get('name')}[/bold]"
                        f"  [{_def_os} | {_def_cpu} CPU | {_def_mem} MB | {_def_disk} GB][/cyan]"
                    )
                    console.print("  [dim]Press Enter for defaults, or specify: e.g. 'windows, 8GB, 4 CPU, 50GB'[/dim]")
                    try:
                        _man_input = console.input("[bold cyan]  Config:[/bold cyan] ").strip().lower()
                    except (KeyboardInterrupt, EOFError):
                        console.print("\n[dim]Cancelled.[/dim]")
                        return
                    if _man_input:
                        import re as _re
                        # os_type
                        for _kw in _OS_KEYWORDS:
                            if _kw in _man_input.split():
                                raw_args["os_type"] = OS_TYPE_ALIASES.get(_kw, _kw)
                                break
                        # memory: "8gb" / "8192mb" / "8192"
                        _m = _re.search(r'(\d+)\s*gb(?!\s*disk)', _man_input)
                        if _m:
                            raw_args["memory_mb"] = int(_m.group(1)) * 1024
                        _m = _re.search(r'(\d+)\s*mb', _man_input)
                        if _m:
                            raw_args["memory_mb"] = int(_m.group(1))
                        # cpu cores: "4 cpu" / "4 cores" / "4 core"
                        _m = _re.search(r'(\d+)\s*(?:cpu|core)', _man_input)
                        if _m:
                            raw_args["cpu_cores"] = int(_m.group(1))
                        # disk: "50gb disk" / "50 gb disk"
                        _m = _re.search(r'(\d+)\s*gb\s*disk', _man_input)
                        if _m:
                            raw_args["disk_size_gb"] = int(_m.group(1))
                    # Ensure os_type has a value and mark it clarified so the
                    # pre-gate doesn't re-ask — manual config owns this field.
                    if not raw_args.get("os_type"):
                        raw_args["os_type"] = _def_os
                    _just_clarified_fields.add("os_type")
                    _just_clarified_values.add(("os_type", raw_args["os_type"]))
                    # Clear any pre-gate result — manual config handled missing fields.
                    _pre_gate_result = None
                    # Don't auto-confirm next VM — each needs its own config
                    _confirmed_tool_types.discard("create_vm")
                elif tool_name == "create_vm" and "manual" in raw_args:
                    raw_args = dict(raw_args)
                    raw_args.pop("manual", None)
                # ──────────────────────────────────────────────────────────────

                if _pre_gate_result:
                    result = _pre_gate_result
                else:
                    result = execute_tool(tool_name, raw_args, verbose)
                    _tool_executed_this_turn = True

                # Tools that self-render formatted output: strip data so the AI
                # doesn't repeat the table/panel in its text response.
                if tool_name in _RENDERS_OUTPUT and not verbose and not _pre_gate_result:
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
                                return
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
                                _op_cancelled = True
                            else:
                                hint = result.get("hint", "")
                                messages.append({"role": "user", "content": _conf})
                                messages.append({"role": "user", "content": f"_INTERNAL_ The user confirmed. {hint} Keep ALL original arguments exactly as they were."})
                            _clarify_happened = True
                            _clarify_answer   = _conf
                            _clarify_field    = ""
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
                            return
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
                                    _just_clarified_fields.update(filled.keys())
                                    _just_clarified_values.update(filled.items())
                                    _clarify_happened = True
                                    _clarify_answer   = clarified
                                    _clarify_field    = "overwrite"
                                    break
                            filled[f] = clarified
                            messages.append({"role": "user", "content": clarified})
                            # If the user named a specific distro, inject os_name so the
                            # executor can auto-find the matching ISO.
                            if f == "os_type" and clarified.lower().strip() in OS_TYPE_ALIASES:
                                filled["os_name"] = clarified.lower().strip()
                    _just_clarified_fields.update(filled.keys())
                    _just_clarified_values.update(filled.items())
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
                    _clarify_happened = True
                    _clarify_answer   = str(filled)
                    _clarify_field    = ", ".join(filled.keys())
                    break  # Don't process further tool calls until AI re-plans with the answers

            if _op_cancelled:
                continue  # let AI ask what the user wants to do instead

            if _context_assistant_fired and not tool_calls:
                # Mismatch/high-stakes path: hint was injected, give AI another pass.
                # Hallucinated-field path: args were patched in-place, loop continues normally.
                _tools_called_this_turn = False
                continue

            if _clarify_happened:
                hint = (
                    f" The user provided: {_clarify_answer} (for fields: {_clarify_field})."
                    if _clarify_field else ""
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
            if _last_had_tools:
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
        if _user_wants_action and not _tool_executed_this_turn and messages:
            last = messages[-1]
            if last.get("role") == "assistant" and not last.get("tool_calls"):
                messages.pop()
        save_session(messages)

        # Runtime drift counter — warn after 3 consecutive action turns where
        # the model gave text instead of calling a tool.
        if _user_wants_action and not _tool_executed_this_turn:
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
        elif _tool_executed_this_turn:
            _runtime_drift_count = 0


# ── Direct sub-command CLI ─────────────────────────────────────────────────────

# Dispatches direct sub-commands (list, launch, stop, snapshot, network, etc.) to the manager and renders output.
# In: List[str] args, bool verbose → Out: nothing
def cli_direct(args: List[str], verbose: bool = False):
    def pp(data):
        if verbose:
            console.print_json(json.dumps(data, default=str))

    cmd  = args[0]
    rest = args[1:]

    if cmd == "list":
        vms = manager.list_vms()
        _render_vm_list(vms)
        if verbose:
            pp(vms)

    elif cmd == "status" and rest:
        r = manager.vm_status(rest[0])
        _render_status(r)
        if verbose:
            pp(r)

    elif cmd == "monitor":
        name = rest[0] if rest else "all"
        r    = manager.monitor_all() if name == "all" else manager.monitor_vm(name)
        if isinstance(r, dict) and "state" in r:
            _render_monitor(r)
        else:
            for v in r.values():
                _render_monitor(v)
        if verbose:
            pp(r)

    elif cmd == "launch" and rest:
        r     = manager.launch_vm(rest[0], display=rest[1] if len(rest) > 1 else None)
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")
        if r.get("setup_cmd"):
            setup_cmd  = r["setup_cmd"]
            is_windows = setup_cmd.startswith("irm ")
            how_line   = (
                "Open [bold]PowerShell[/bold] inside the VM and run:"
                if is_windows else
                "Open a terminal inside the VM and run (then reboot):"
            )
            console.print(Panel(
                f"[bold]Stealth guest setup required.[/bold] {how_line}\n\n"
                f"[cyan]{setup_cmd}[/cyan]\n\n"
                f"[dim]When done, run:[/dim] [bold]qemu-api setup-done {rest[0]}[/bold]",
                title="Stealth Setup", border_style="yellow",
            ))
            _show_stealth_popup(rest[0], setup_cmd)

    elif cmd == "stop" and rest:
        r     = manager.stop_vm(rest[0])
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

    elif cmd == "config" and rest:
        r = manager.show_config(rest[0])
        if r.get("success"):
            console.print_json(json.dumps(r["config"], default=str))
        else:
            console.print(f"[error]{r['error']}[/error]")

    elif cmd == "resize" and len(rest) >= 2:
        r     = manager.resize_disk(rest[0], 0, int(rest[1]))
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

    elif cmd == "clone" and len(rest) >= 2:
        r     = manager.clone_vm(rest[0], rest[1])
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

    elif cmd == "snapshot" and len(rest) >= 2:
        sub = rest[0]
        if sub == "list" and len(rest) >= 2:
            r = manager.snapshot_list(rest[1])
            _render_snapshots(r)
        elif sub == "create" and len(rest) >= 3:
            r = manager.snapshot_create(rest[1], rest[2])
            console.print(f"[success]{r.get('message', r.get('error'))}[/success]")
        elif sub == "restore" and len(rest) >= 3:
            r = manager.snapshot_restore(rest[1], rest[2])
            console.print(f"[success]{r.get('message', r.get('error'))}[/success]")
        elif sub == "delete" and len(rest) >= 3:
            r = manager.snapshot_delete(rest[1], rest[2])
            console.print(f"[success]{r.get('message', r.get('error'))}[/success]")

    elif cmd == "network" and rest:
        sub = rest[0]
        if sub == "list":
            console.print_json(json.dumps(manager.list_networks(), default=str))
        elif sub == "create" and len(rest) >= 2:
            console.print_json(json.dumps(manager.create_network(rest[1]), default=str))
        elif sub == "delete" and len(rest) >= 2:
            console.print_json(json.dumps(manager.delete_network(rest[1]), default=str))
        elif sub == "add" and len(rest) >= 3:
            console.print_json(json.dumps(manager.add_vm_to_network(rest[1], rest[2]), default=str))

    elif cmd == "limit" and len(rest) >= 2:
        cpu = int(rest[1]) if len(rest) > 1 else None
        mem = int(rest[2]) if len(rest) > 2 else None
        r   = manager.set_resource_limits(rest[0], cpu_percent=cpu, memory_mb=mem)
        console.print_json(json.dumps(r, default=str))

    elif cmd == "delete" and rest:
        if console.input(f"[warn]Delete '{rest[0]}'? [y/N]:[/warn] ").lower() == "y":
            r = manager.delete_vm(rest[0])
            console.print(f"[success]{r.get('message', r.get('error'))}[/success]")

    elif cmd == "cmd" and len(rest) >= 2:
        r = manager.send_monitor_cmd(rest[0], rest[1])
        if r.get("success"):
            console.print(r["output"])

    elif cmd == "profiles":
        _render_profiles(list_profiles())

    elif cmd == "check-profile" and rest:
        _render_compat(check_profile_compatibility(rest[0]))

    elif cmd == "system":
        caps = check_system_capabilities()
        caps["ovmf_paths"] = OVMF
        _render_system(caps)

    elif cmd == "isos":
        isos = manager.scan_isos()
        if isos:
            t = Table(box=box.ROUNDED, border_style="cyan")
            t.add_column("File")
            t.add_column("Size")
            t.add_column("Path", style="dim")
            for iso in isos:
                t.add_row(iso["name"], f"{iso['size_gb']}GB", iso["path"])
            console.print(t)
        else:
            console.print("[warn]No ISOs found in common locations.[/warn]")

    elif cmd == "show-cmd" and rest:
        r = manager.print_command(rest[0])
        if r.get("success"):
            console.print(Panel(r["command"], title="QEMU Command", border_style="cyan"))

    elif cmd == "setup-done" and rest:
        r = manager.mark_stealth_done(rest[0])
        style = "success" if r.get("success") else "error"
        console.print(f"[{style}]{r.get('message', r.get('error', ''))}[/{style}]")

    elif cmd == "guest-setup" and rest:
        vm_name = rest[0]
        r = manager.generate_guest_setup(vm_name)
        if not r.get("success"):
            console.print(f"[error]{r['error']}[/error]")
            return

        script_path = r["path"]
        script_dir  = os.path.dirname(script_path)
        script_file = os.path.basename(script_path)

        # Find a free port and serve the script via HTTP so the VM can pull it
        with socket.socket() as s:
            s.bind(('', 0))
            port = s.getsockname()[1]

        class _Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=script_dir, **kw)
            def log_message(self, *_):
                pass  # silence access log

        srv = http.server.HTTPServer(('0.0.0.0', port), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        host_ip = "10.0.2.2"  # QEMU user-networking default gateway = host
        url     = f"http://{host_ip}:{port}/{script_file}"

        console.print(Panel(
            f"[bold]Script:[/bold] {script_path}\n\n"
            f"[bold]Inside the VM, run:[/bold]\n"
            f"[cyan]curl {url} | sudo bash[/cyan]\n\n"
            f"[dim]Server will exit when you press Ctrl+C.[/dim]",
            title=f"Guest Setup — {vm_name}",
            border_style="green",
        ))
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            srv.shutdown()
            console.print("[dim]Server stopped.[/dim]")

    elif cmd == "clear-session":
        clear_session()

    elif cmd == "-tf" and rest:
        _tf_report(rest[0])

    else:
        console.print(Panel(
            "[bold]Direct CLI usage:[/bold]\n\n"
            "  qemu-api list\n"
            "  qemu-api status <name>\n"
            "  qemu-api monitor <name|all>\n"
            "  qemu-api launch <name> [display]\n"
            "  qemu-api stop <name>\n"
            "  qemu-api clone <source> <new>\n"
            "  qemu-api config <name>\n"
            "  qemu-api resize <name> <gb>\n"
            "  qemu-api snapshot list|create|restore|delete <vm> [snap]\n"
            "  qemu-api network list|create|delete|add [args]\n"
            "  qemu-api limit <name> <cpu%> [mem_mb]\n"
            "  qemu-api delete <name>\n"
            "  qemu-api cmd <name> \"<qemu cmd>\"\n"
            "  qemu-api profiles\n"
            "  qemu-api check-profile <name>\n"
            "  qemu-api system\n"
            "  qemu-api isos\n"
            "  qemu-api show-cmd <name>\n"
            "  qemu-api clear-session\n"
            "  qemu-api -tf <name>\n\n"
            "Add [bold]-v[/bold] anywhere for verbose/raw output.\n"
            "Add [bold]-cu[/bold] to AI chat to skip product verification for custom machines.\n"
            "Add [bold]-cs[/bold] to AI chat to clear the session before starting.",
            border_style="cyan", title="qemu-api help",
        ))


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
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

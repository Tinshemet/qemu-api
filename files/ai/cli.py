"""
cli.py — CLI Entry Point and Chat Loop Layer

Provides the interactive AI chat loop and the direct sub-command CLI
(qemu-api list, launch, stop, etc.). This is the main entry point
for both modes; ollama_wrapper.py is a thin shim that re-exports from here.
"""

import json
import os
import sys
from typing import List

from rich import box
from rich.panel import Panel
from rich.table import Table

from api.qemu_config  import OVMF, check_profile_compatibility, check_system_capabilities, list_profiles
from .session      import clear_session, load_session, save_session
from .display      import (
    console,
    _print_banner, _render_compat, _render_monitor, _render_profiles,
    _render_snapshots, _render_status, _render_system, _render_vm_list,
)
from .fingerprint  import _tf_report
from .ollama_client import OLLAMA_MODEL, OLLAMA_URL, _call_ollama
from executioner.tool_executor import execute_tool, manager
from preflight.validator    import set_custom_mode

_CFG         = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))
_EXIT_CMDS   = set(_CFG["exit_commands"])
_SHORTCUTS   = _CFG["shortcut_commands"]
_LOOP_MAX    = _CFG["chat"]["tool_loop_max"]
_ACTION_WORDS = set(_CFG["action_words"])

# Tools that require explicit user confirmation before execution.
# Maps tool_name → (arg_field_to_confirm, human-readable verb).
_CONFIRM_REQUIRED: dict = {
    "create_vm":           ("name",         "create VM"),
    "delete_vm":           ("name",         "delete VM"),
    "clone_vm":            ("new_name",     "clone to new VM"),
    "update_config":       ("name",         "update config for VM"),
    "resize_disk":         ("name",         "resize disk for VM"),
    "snapshot_restore":    ("name",         "restore snapshot for VM"),
    "snapshot_delete":     ("name",         "delete snapshot of VM"),
    "delete_network":      ("net_name",     "delete network"),
    "delete_profile":      ("profile_name", "delete profile"),
    "set_resource_limits": ("name",         "set resource limits for VM"),
}


# ── Chat loop ──────────────────────────────────────────────────────────────────

# Runs the interactive Ollama chat REPL: reads input, drives the agentic tool loop (up to 15 rounds), handles clarifications, and saves session.
# In: bool verbose → Out: nothing (blocks until exit)
def chat_loop(verbose: bool = False):
    _print_banner(
        verbose=verbose,
        ollama_url=OLLAMA_URL,
        ollama_model=OLLAMA_MODEL,
        ovmf_available=OVMF["available"],
        ovmf_code=OVMF.get("code", ""),
    )
    messages = load_session()

    while True:
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

        messages.append({"role": "user", "content": user_input})

        _user_wants_action = bool(set(_ui.split()) & _ACTION_WORDS)
        _tools_called_this_turn = False
        _just_clarified_fields: set = set()  # persists across all iterations for this user turn

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

                # ── os_type guard ──────────────────────────────────────────
                # Strip AI-inferred os_type when the user didn't mention an OS
                # in their message — unless they already answered it via the
                # gate this turn (in which case keep what they said).
                if tool_name == "create_vm" \
                        and "os_type" in raw_args \
                        and "os_type" not in _just_clarified_fields:
                    _OS_KEYWORDS = {
                        "linux", "windows", "win", "ubuntu", "debian", "fedora",
                        "arch", "kali", "mint", "centos", "rhel", "macos", "mac",
                        "osx", "android", "freebsd", "openbsd", "other",
                    }
                    if not (_OS_KEYWORDS & set(_ui.split())):
                        raw_args.pop("os_type")
                # ──────────────────────────────────────────────────────────

                # ── Safety confirmation gate ───────────────────────────────
                # Skip if the key field was answered via the clarify gate this
                # turn — the user just confirmed the value moments ago.
                if tool_name in _CONFIRM_REQUIRED and \
                        _CONFIRM_REQUIRED[tool_name][0] not in _just_clarified_fields:
                    field, verb = _CONFIRM_REQUIRED[tool_name]
                    proposed = raw_args.get(field, "")

                    try:
                        vm_names = [v["name"] for v in manager.list_vms()]
                    except Exception:
                        vm_names = []
                    opts_str = "  ".join(f"[{n}]" for n in vm_names[:6])

                    hint = f"[bold]{proposed}[/bold]" if proposed else "[dim]unknown[/dim]"
                    console.print(f"\n[yellow]⚠  {verb}: {hint}[/yellow]")
                    if opts_str:
                        console.print(f"   Available: {opts_str}")
                    console.print(
                        "[dim]Type the name to confirm, a different name to redirect,"
                        " or press Enter to cancel.[/dim]"
                    )
                    try:
                        confirmed = console.input("[bold cyan]Confirm:[/bold cyan] ").strip()
                    except (KeyboardInterrupt, EOFError):
                        console.print("\n[dim]Cancelled.[/dim]")
                        return

                    if not confirmed:
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

                    if confirmed != proposed:
                        raw_args[field] = confirmed
                    _just_clarified_fields.add(field)  # don't ask again this turn
                # ──────────────────────────────────────────────────────────

                result = execute_tool(tool_name, raw_args, verbose)
                messages.append({
                    "role":    "tool",
                    "content": json.dumps(result, default=str),
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
                            filled[f] = clarified
                            messages.append({"role": "user", "content": clarified})
                    _just_clarified_fields.update(filled.keys())
                    _clarify_happened = True
                    _clarify_answer   = str(filled)
                    _clarify_field    = ", ".join(filled.keys())
                    break  # Don't process further tool calls until AI re-plans with the answers

            if _op_cancelled:
                continue  # let AI ask what the user wants to do instead

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

        save_session(messages)


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
            "Add [bold]-cu[/bold] to AI chat to skip product verification for custom machines.",
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

    if argv:
        cli_direct(argv, verbose=verbose)
    else:
        chat_loop(verbose=verbose)

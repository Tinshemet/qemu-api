"""
tool_executor.py — Executor-side tool dispatch.

Owns the _run() dispatch function that routes a pre-validated tool call to the
QemuManager (or the config layer). The QemuManager singleton, the parsed config,
and the revert-tracking state live in context.py; the complex create_vm build is
in create_vm.py. The full orchestrator pipeline (sanitize → gate → preflight →
dispatch) lives in orchestrator/pipeline.py; this module has no orchestrator
imports.

Re-exports the names external callers import from here (manager, _VM_DEFS,
_run, dispatch_tool, _clear_revert / _set_revert) so orchestrator.pipeline,
executor.server, and the tests are unaffected by the split.
"""

import sys
from typing import Any, Dict

from shared.executioner.context import (
    manager,
    _VM_DEFS, _TOOL_DEFS, _CKPT_TAG_PREFIX, _REVERT_AWARE_TOOLS,
    _last_revert_action, _set_revert, _clear_revert,
    console, Panel,
    OVMF, check_system_capabilities, check_profile_compatibility,
    list_profiles, save_custom_profile, delete_custom_profile, tf_report,
    render_compat, render_fleet, render_monitor, render_profiles, render_templates,
    render_snapshots, render_status, render_system, render_vm_failure, render_vm_list,
)
from shared.executioner.create_vm import execute_create_vm as _execute_create_vm


_STUB_PLACEHOLDER_VM_NAMES = frozenset()


def _resolve_iso_stub(p: str) -> str:
    """Identity ISO resolver used in executor-only mode (no orchestrator)."""
    return p


def _preflight_check_stub(*a, **k) -> dict:
    """No-op preflight stub — the orchestrator already validated the args."""
    return {"action": "ok"}


def _show_preflight_warning_stub(*a, **k) -> None:
    """No-op preflight-warning stub for executor-only dispatch."""
    pass


def dispatch_tool(tool_name: str, args: Dict[str, Any], verbose: bool = False) -> Any:
    """Execute a pre-validated tool call — no orchestrator pipeline.

    Entry point for the remote executor server. The orchestrator has already run
    sanitizer, context gate, and preflight; args are clean and VM names are
    resolved before this is called.

    Args:
        tool_name: Name of the tool (e.g. ``"create_vm"``).
        args:      Pre-sanitised argument dict.
        verbose:   When True, suppress Rich console output.

    Returns:
        Tool result dict, always containing ``"success": bool``.

    Example::
        >>> dispatch_tool("list_vms", {})
        [{"name": "my-linux", "status": "stopped", ...}]
    """
    return _run(
        tool_name, args, verbose,
        raw_os_type=args.get("os_type", ""),
        placeholder_vm_names=_STUB_PLACEHOLDER_VM_NAMES,
        resolve_iso=_resolve_iso_stub,
        preflight_check=_preflight_check_stub,
        show_preflight_warning=_show_preflight_warning_stub,
    )


def _run(
    tool_name: str,
    args: Dict[str, Any],
    verbose: bool,
    *,
    raw_os_type: str = "",
    placeholder_vm_names=None,
    resolve_iso=None,
    preflight_check=None,
    show_preflight_warning=None,
) -> Any:
    """Dispatch a pre-pipeline tool call to QemuManager or the config layer.

    Called by dispatch_tool (executor path, with stubs) and by
    orchestrator.pipeline.execute_tool (local-mode path, with real implementations).
    All orchestrator-side concerns (sanitize, gate, name resolution) must be
    completed before calling this function.
    """
    if placeholder_vm_names is None:
        placeholder_vm_names = _STUB_PLACEHOLDER_VM_NAMES
    if resolve_iso is None:
        resolve_iso = _resolve_iso_stub
    if preflight_check is None:
        preflight_check = _preflight_check_stub
    if show_preflight_warning is None:
        show_preflight_warning = _show_preflight_warning_stub

    # A revert action is only meaningful immediately after the call that set
    # it — any unrelated tool call in between means "undo my last action"
    # would target something the caller probably isn't thinking about
    # anymore, so drop it. Tools that manage the state themselves are
    # exempted (they set/clear it explicitly based on their own outcome).
    if tool_name not in _REVERT_AWARE_TOOLS:
        _clear_revert()

    # ── revert ────────────────────────────────────────────────────────────────
    if tool_name == "revert":
        if not _last_revert_action:
            return {"success": False, "error": "No reversible action to revert."}
        rev = dict(_last_revert_action)
        console.print(f"\n[yellow]↩ Revert: {rev['description']}[/yellow]")
        if not sys.stdin.isatty():
            console.print("[dim]Cancelled (no interactive terminal to confirm).[/dim]")
            return {"success": False, "error": "Revert cancelled: not running interactively."}
        try:
            answer = console.input("[bold cyan]Proceed? (y/n):[/bold cyan] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Cancelled.[/dim]")
            return {"success": False, "error": "Revert cancelled by user."}
        if answer != "y":
            return {"success": False, "error": "Revert cancelled by user."}
        _clear_revert()
        return _run(
            rev["tool"], rev["args"], verbose,
            raw_os_type=rev["args"].get("os_type", ""),
            placeholder_vm_names=placeholder_vm_names,
            resolve_iso=resolve_iso,
            preflight_check=preflight_check,
            show_preflight_warning=show_preflight_warning,
        )

    # ── clarify ───────────────────────────────────────────────────────────────
    if tool_name == "clarify":
        return {"clarify": True, "question": args.get("question", ""), "options": args.get("options", [])}

    # ── system info ───────────────────────────────────────────────────────────
    elif tool_name == "check_system":
        caps = check_system_capabilities()
        caps["ovmf_paths"] = OVMF
        if not verbose:
            render_system(caps)
        return caps

    elif tool_name == "scan_isos":
        return manager.scan_isos()

    elif tool_name == "list_vms":
        vms = manager.list_vms(label=args.get("label"))
        if not verbose:
            render_vm_list(vms)
        return vms

    elif tool_name == "add_label":
        return manager.add_label(args["name"], args["label"])

    elif tool_name == "remove_label":
        return manager.remove_label(args["name"], args["label"])

    elif tool_name == "list_labels":
        return manager.list_labels()

    elif tool_name == "mark_as_template":
        return manager.mark_as_template(args["name"])

    elif tool_name == "remove_template":
        return manager.remove_template(args["name"])

    elif tool_name == "list_templates":
        templates = manager.list_templates()
        if not verbose:
            render_templates(templates)
        return templates

    elif tool_name == "list_profiles":
        profiles = list_profiles()
        if not verbose:
            render_profiles(profiles)
        return profiles

    elif tool_name == "check_profile_compatibility":
        result = check_profile_compatibility(args["profile_name"])
        if not verbose:
            render_compat(result)
        return result

    elif tool_name == "create_profile":
        pname = args.pop("profile_name")
        notes = args.pop("notes", "")
        force = args.pop("force", False)
        if notes:
            args["_notes"] = notes

        if not force:
            preflight = preflight_check(
                "create_profile", {"profile_name": pname, **args}, manager, verbose
            )
            action = preflight.get("action", "ok")

            if action == "abort":
                return {
                    "success":    False,
                    "error":      preflight.get("reason", "Pre-flight check failed"),
                    "correction": preflight.get("correction"),
                }

            if action == "ask_user":
                if not verbose:
                    show_preflight_warning(preflight, console)
                return {
                    "success":    False,
                    "clarify":    True,
                    "question":   preflight.get("question"),
                    "options":    preflight.get("options", []),
                    "reason":     preflight.get("reason"),
                    "correction": preflight.get("correction"),
                    "issues":     preflight.get("issues", []),
                    "hint":       "To save anyway, call create_profile again with force=true",
                }

            if action == "auto_fix":
                fixed = preflight.get("fixed_args", {})
                args.update({k: v for k, v in fixed.items() if k not in ("profile_name", "force")})
                if not verbose:
                    console.print(f"  [yellow]⚠ Pre-flight auto-fixed: {preflight.get('reason')}[/yellow]")
                    for w in preflight.get("warnings", []):
                        console.print(f"  [dim]  ↳ {w}[/dim]")

        result = save_custom_profile(pname, args)
        if result["success"]:
            result["compatibility"] = check_profile_compatibility(result["profile_name"])
            _set_revert("delete_profile", {"profile_name": pname}, f"undo create_profile '{pname}'")
        return result

    elif tool_name == "delete_profile":
        _clear_revert()
        return delete_custom_profile(args["profile_name"])

    # ── create_vm ─────────────────────────────────────────────────────────────
    elif tool_name == "create_vm":
        return _execute_create_vm(args, verbose, raw_os_type, placeholder_vm_names, resolve_iso)

    # ── VM lifecycle ──────────────────────────────────────────────────────────
    elif tool_name == "clone_vm":
        result = manager.clone_vm(args["source_name"], args["new_name"])
        if result.get("success"):
            _set_revert("delete_vm", {"name": args["new_name"]}, f"undo clone_vm '{args['new_name']}'")
        return result

    elif tool_name == "launch_vm":
        result = manager.launch_vm(
            args["name"],
            display=args.get("display"),
            dry_run=args.get("dry_run", False),
            vnc_bind_local=args.get("vnc_bind_local"),
        )
        if result.get("success"):
            _set_revert("stop_vm", {"name": args["name"], "force": True}, f"undo launch_vm '{args['name']}'")
        return result

    elif tool_name == "stop_vm":
        if args["name"] == "all":
            _clear_revert()
            return manager.stop_all()
        result = manager.stop_vm(args["name"], force=args.get("force", False))
        if result.get("success"):
            _set_revert("launch_vm", {"name": args["name"]}, f"undo stop_vm '{args['name']}'")
        return result

    elif tool_name == "vm_status":
        result = manager.vm_status(args["name"])
        if not verbose:
            render_status(result)
        return result

    elif tool_name == "run_guest_command":
        result = manager.run_guest_command(
            args["name"], args["command"], timeout=args.get("timeout")
        )
        if not verbose:
            if result.get("success"):
                if result.get("stdout"):
                    console.print(result["stdout"], end="" if result["stdout"].endswith("\n") else "\n")
                if result.get("stderr"):
                    console.print(f"[red]{result['stderr']}[/red]", end="")
                console.print(f"[dim]exit code: {result.get('exit_code')}[/dim]")
            else:
                console.print(f"[red]{result.get('error', 'unknown error')}[/red]")
        return result

    elif tool_name == "guest_ping":
        result = manager.guest_ping(args["name"])
        if not verbose:
            if result.get("success"):
                style = "green" if result.get("alive") else "yellow"
                state = "alive" if result.get("alive") else "not responding"
                console.print(f"[{style}]{args['name']}: guest agent {state}[/{style}]")
            else:
                console.print(f"[red]{result.get('error', 'unknown error')}[/red]")
        return result

    elif tool_name == "guest_probe":
        result = manager.guest_probe(
            args["name"], args["assertion"], args["target"],
            value=args.get("value"), timeout=args.get("timeout")
        )
        if not verbose:
            if result.get("success"):
                holds = result.get("holds")
                style = "green" if holds else "yellow"
                console.print(f"[{style}]{args['name']}: {result['assertion']}"
                              f"({result['target']}) → {'holds' if holds else 'does not hold'}[/{style}]")
            else:
                console.print(f"[red]{result.get('error', 'unknown error')}[/red]")
        return result

    elif tool_name == "claim_finding":
        # A model-proposed, TYPED finding (type from claim_types.json). No-op at the
        # executor — the harness records it into the ledger via the claim_finding
        # yield-schema, and (for a type with an `assertion`) ONLY if guest_probe
        # confirms it. A type without an assertion is the operator opting into an
        # unverified claim (that's their config choice). Here we validate the type
        # and coerce the value.
        try:
            from orchestrator.ai.findings import claim_type as _ct, coerce_value as _cv
            spec = _ct(args.get("type"))
        except Exception:
            spec = None
        if spec is None:
            result = {"success": False, "error": f"unknown claim type '{args.get('type')}'"}
        else:
            try:
                val = _cv(args.get("value"), spec.get("value_type", "string"))
            except (ValueError, TypeError):
                result = {"success": False,
                          "error": f"claim value {args.get('value')!r} is not a {spec.get('value_type')}"}
            else:
                grounded = bool(spec.get("assertion"))
                evidence = (args.get("evidence") or "").strip()
                if not grounded and not evidence:
                    # No probe CAN confirm this type, so a human must — and can't
                    # without knowing where to look. Require the evidence up front.
                    result = {"success": False,
                              "error": f"'{args.get('type')}' can't be probe-verified — "
                                       f"provide `evidence` (where/how you found it) so a human can check it."}
                else:
                    result = {"success": True, "value": val, "type": args.get("type"),
                              "grounded": grounded, "evidence": evidence or None}
                    if not verbose:
                        tag = "pending probe" if grounded else f"UNVERIFIED claim · evidence: {evidence}"
                        console.print(f"[dim]claim {args.get('type')}={val} ({tag})[/dim]")
        return result

    elif tool_name == "fleet":
        result = manager.fleet(
            args["label"], args["action"],
            command=args.get("command"),
            args=args.get("args"),
            timeout=args.get("timeout"),
        )
        if not verbose:
            render_fleet(result)
        return result

    elif tool_name == "generate_guest_agent_setup":
        result = manager.generate_guest_agent_setup(args["name"])
        if not verbose:
            if result.get("success"):
                console.print(
                    f"[green]✓ Guest agent setup script ready: {result['path']}[/green]\n"
                    f"[dim]  Run inside the VM: {result['cmd_template']}[/dim]"
                )
            else:
                console.print(f"[red]{result.get('error', 'unknown error')}[/red]")
        return result

    elif tool_name == "provision_guest_agent_offline":
        result = manager.provision_guest_agent_offline(args["name"])
        if not verbose:
            if result.get("success"):
                console.print(f"[green]✓ Stealth serial-agent provisioned offline on '{args['name']}'[/green]")
            else:
                console.print(f"[red]{result.get('error', 'unknown error')}[/red]")
        return result

    elif tool_name == "monitor_vm":
        if args["name"] == "all":
            result = manager.monitor_all()
            if not verbose:
                for r in result.values():
                    render_monitor(r)
            return result
        result = manager.monitor_vm(args["name"])
        if not verbose:
            render_monitor(result)
        return result

    elif tool_name == "show_config":
        return manager.show_config(args["name"])

    elif tool_name == "update_config":
        # Capture old values before applying so we can revert
        _old_cfg = manager.show_config(args["name"])
        _updates = args.get("updates", {})
        result = manager.update_config(args["name"], _updates)
        if result.get("success") and _old_cfg.get("success"):
            _old_vals = {k: _old_cfg["config"].get(k) for k in _updates}
            _set_revert(
                "update_config",
                {"name": args["name"], "updates": _old_vals},
                f"undo update_config '{args['name']}' fields {list(_updates.keys())}",
            )
        return result

    elif tool_name == "resize_disk":
        _clear_revert()
        return manager.resize_disk(
            args["name"], args.get("disk_index", 0), args["new_size_gb"]
        )

    elif tool_name == "snapshot_create":
        _snap = args.get("snap_name", _TOOL_DEFS["snap_name"])
        result = manager.snapshot_create(args["name"], _snap)
        if result.get("success"):
            _set_revert(
                "snapshot_delete",
                {"name": args["name"], "snap_name": _snap},
                f"undo snapshot_create '{_snap}' on '{args['name']}'",
            )
        return result

    elif tool_name == "snapshot_list":
        result = manager.snapshot_list(args["name"])
        if not verbose:
            render_snapshots(result)
        return result

    elif tool_name == "snapshot_restore":
        _clear_revert()
        return manager.snapshot_restore(args["name"], args["snap_name"])

    elif tool_name == "snapshot_delete":
        _clear_revert()
        return manager.snapshot_delete(args["name"], args["snap_name"])

    # ── checkpoint / rollback (SQL-savepoint-style, base toolset) ──────────────
    # A checkpoint is a NAMED savepoint over VM state: it snapshots the target VM,
    # or the whole fleet, under a reserved tag. rollback restores that tag on each
    # member. Members are DISCOVERED from the tag itself (no separate manifest to
    # persist or drift). Available to every agent — the Doorman for a manual "save
    # point before I do something risky", the autonomous gate-action `checkpoint`
    # for making a destructive-but-authorized step revertible.
    elif tool_name == "checkpoint":
        label   = args["label"]
        snap    = f"{_CKPT_TAG_PREFIX}{label}"
        targets = [args["name"]] if args.get("name") else [
            v["name"] for v in manager.list_vms() if v.get("name")]
        done, errors = [], []
        for vm in targets:
            (done if manager.snapshot_create(vm, snap).get("success") else errors).append(vm)
        _clear_revert()
        return {
            "success": bool(done) or not targets,
            "checkpoint": label, "snapshot": snap, "vms": done, "errors": errors,
            "message": (f"Checkpoint '{label}' saved on {len(done)} VM(s)"
                        + (f"; {len(errors)} failed ({', '.join(errors)})" if errors else "") + "."),
        }

    elif tool_name == "rollback":
        label = args["label"]
        snap  = f"{_CKPT_TAG_PREFIX}{label}"
        if args.get("name"):
            targets = [args["name"]]
        else:                                  # discover members by the checkpoint tag
            targets = []
            for v in manager.list_vms():
                sl = manager.snapshot_list(v.get("name"))
                if sl.get("success") and any(s.get("tag") == snap for s in sl.get("snapshots", [])):
                    targets.append(v["name"])
        if not targets:
            return {"success": False, "error": f"No checkpoint '{label}' found."}
        done, errors = [], []
        for vm in targets:
            (done if manager.snapshot_restore(vm, snap).get("success") else errors).append(vm)
        _clear_revert()
        return {
            "success": bool(done), "rolled_back_to": label, "vms": done, "errors": errors,
            "message": (f"Rolled back {len(done)} VM(s) to '{label}'"
                        + (f"; {len(errors)} failed ({', '.join(errors)})" if errors else "") + "."),
        }

    elif tool_name == "set_resource_limits":
        return manager.set_resource_limits(
            args["name"],
            cpu_percent=args.get("cpu_percent"),
            memory_mb=args.get("memory_mb"),
        )

    elif tool_name == "create_network":
        result = manager.create_network(args["net_name"])
        if result.get("success"):
            _set_revert("delete_network", {"net_name": args["net_name"]}, f"undo create_network '{args['net_name']}'")
        return result

    elif tool_name == "delete_network":
        _clear_revert()
        return manager.delete_network(args["net_name"])

    elif tool_name == "list_networks":
        return manager.list_networks()

    elif tool_name == "add_vm_to_network":
        return manager.add_vm_to_network(args["net_name"], args["vm_name"])

    elif tool_name == "open_display":
        return manager.open_display(args["name"])

    elif tool_name == "open_shell":
        return manager.open_shell(args["name"])

    elif tool_name == "delete_vm":
        _clear_revert()
        return manager.delete_vm(args["name"], delete_disks=True)

    elif tool_name == "check_disk":
        return manager.check_disk(args["name"])

    elif tool_name == "get_vm_logs":
        result = manager.get_vm_logs(args["name"], lines=int(args.get("lines", _TOOL_DEFS["log_lines"])))
        if not verbose:
            render_vm_failure(result)
        return result

    elif tool_name == "print_command":
        result = manager.print_command(args["name"])
        if result.get("success") and not verbose:
            console.print(Panel(result["command"], title="QEMU Command", border_style="cyan"))
            return {"success": True, "command": result["command"]}
        return result

    elif tool_name == "fingerprint_vm":
        return tf_report(args["name"], summary=bool(args.get("summary", False)))

    elif tool_name == "send_monitor_cmd":
        return manager.send_monitor_cmd(args["name"], args.get("cmd", "info status"))

    else:
        return {"success": False, "error": f"Unknown tool: {tool_name}"}

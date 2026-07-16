"""
_vm_fleet.py — Fleet mixin (broadcast one action across a labeled VM group).

Provides _VmFleetMixin.fleet(), composed into QemuManager. Selects members via
list_vms(label=...) — which matches auto-derived flags (stealth / hardened /
bridge / vpn / …) OR user-assigned labels, the same unified namespace the list
filter already uses — then applies one action to each member sequentially and
aggregates per-VM results as ``{vm_name: result}``. That is the shape stop_all /
monitor_all use and the one _vm_guest.py explicitly anticipated for "a future
fleet layer".

Actions:
  * exec    — run a guest command on each member (run_guest_command)
  * ping    — guest-agent liveness per member (guest_ping)
  * status  — vm_status per member
  * stop    — stop_vm per member
  * launch  — launch_vm per member

Every per-member op already returns a structured ``{"success": bool, ...}`` (or,
for vm_status, a plain status dict), so a member that is stopped / agent-disabled
/ missing surfaces as a per-VM failure inside the aggregate rather than aborting
the whole broadcast — partial success is the normal case.
"""

from typing import Any, Dict, List, Optional

# Actions that take/need a command line vs. the lifecycle/observation actions.
FLEET_ACTIONS = ("exec", "ping", "status", "stop", "launch")


def _member_ok(result: Any) -> bool:
    """True when a per-member result counts as a success.

    Command / lifecycle ops carry an explicit ``success`` flag; vm_status returns
    a plain dict with no such flag, so absence of ``success: False`` and of an
    ``error`` key is treated as success (a status read that returned data).
    """
    return (
        isinstance(result, dict)
        and result.get("success", True) is not False
        and not result.get("error")
    )


class _VmFleetMixin:
    """Mixin: broadcast one action across every VM carrying a given label."""

    def fleet(
        self,
        label:   str,
        action:  str,
        command: Optional[str] = None,
        args:    Optional[List[str]] = None,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Apply ``action`` to every VM labeled ``label`` and aggregate results.

        Selection reuses ``list_vms(label=...)`` so a fleet can be addressed by
        either an auto-derived flag (e.g. ``stealth``) or a user label (e.g.
        ``redteam``). Members are processed in listing order; each per-VM result
        is kept verbatim under its name so partial failures are visible.

        Args:
            label:   Flag or user label selecting the fleet members.
            action:  One of ``exec`` / ``ping`` / ``status`` / ``stop`` / ``launch``.
            command: Shell line for ``exec`` (ignored by other actions).
            args:    Optional argv for ``exec`` (presence switches off the shell
                     wrapper in run_guest_command).
            timeout: Per-command timeout for ``exec`` (seconds).

        Returns:
            ``{"success": bool, "label": str, "action": str, "count": int,
            "ok": int, "failed": int, "results": {vm_name: result}}``. ``success``
            is True when at least one member succeeded. On a bad action or an
            empty selection, ``success`` is False with an ``error`` message.

        Example::
            >>> mgr.fleet("redteam", "exec", command="whoami")
            {"success": True, "label": "redteam", "action": "exec", "count": 2,
             "ok": 2, "failed": 0, "results": {"box1": {...}, "box2": {...}}}
        """
        action = (action or "").strip().lower()
        if action not in FLEET_ACTIONS:
            return {"success": False,
                    "error": f"Unknown fleet action '{action}'. "
                             f"Valid actions: {', '.join(FLEET_ACTIONS)}."}
        if action == "exec" and not command:
            return {"success": False, "error": "fleet exec requires a command."}

        members = [vm["name"] for vm in self.list_vms(label=label)]
        if not members:
            return {"success": False,
                    "error": f"No VMs carry the label '{label}'.",
                    "label": label, "action": action,
                    "count": 0, "ok": 0, "failed": 0, "results": {}}

        results: Dict[str, Any] = {}
        for name in members:
            if action == "exec":
                results[name] = self.run_guest_command(name, command, args, timeout)
            elif action == "ping":
                results[name] = self.guest_ping(name)
            elif action == "status":
                results[name] = self.vm_status(name)
            elif action == "stop":
                results[name] = self.stop_vm(name)
            elif action == "launch":
                results[name] = self.launch_vm(name)

        ok = sum(1 for r in results.values() if _member_ok(r))
        return {
            "success": ok > 0,
            "label":   label,
            "action":  action,
            "count":   len(members),
            "ok":      ok,
            "failed":  len(members) - ok,
            "results": results,
        }

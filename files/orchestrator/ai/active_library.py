"""
active_library.py — the Active Library: an in-memory registry/archive of the
entire system state, so the (deliberately weak) local model doesn't have to
re-derive references and relations from chat history every turn.

Driven by fleet control: the AI now acts on *groups* of VMs and must reason
about *relations between them* ("all redteam VMs", "clones of test1",
"everything on the same network as Y"). The Library holds every VM / profile /
network / template and derives relation indices over them, so the model's job
shrinks to intent detection — the facts come pre-computed.

Design (see the gorgon-active-library memory note):
  * ONE full snapshot() at session start (the only expensive fetch).
  * Thereafter, each mutating tool triggers a TARGETED update of just the
    entity/compartment it touched (apply()), keyed by _TOOL_EFFECTS. Read-only
    tools trigger nothing. Never re-scan more than changed.

Two consumers, one source:
  * the AI reads ai_digest() — a compact projection injected into the system
    prompt (terse on purpose, so it doesn't drown a 7B model);
  * the context-assistant reads the full structured objects as ground truth
    (known_names() / resolve() / relation indices), replacing its thin
    name-only registry.

Local mode pulls from the in-process manager; remote-split mode should refresh
from /sync (seam marked below — not yet wired).
"""

import time
from typing import Any, Dict, List, Optional, Set


def _local_manager():
    """The in-process QEMU manager (local mode). None if unavailable."""
    try:
        from shared.executioner.tool_executor import manager
        return manager
    except Exception:
        return None


# ── Tool metadata — DERIVED from the canonical registry (executor/command_catalog) ─
# _TOOL_EFFECTS = which compartment each mutating tool refreshes (read-only tools
# absent → no update); _VM_NAME_ARG = which arg names the VM its effect targets
# (clone writes a new name). Both are tool metadata → single-sourced in the
# registry, not hand-kept here (the effect map used to miss `fleet`). Guarded so an
# orchestrator-only checkout (no executor/) degrades gracefully.
try:
    from executor.command_catalog import TOOL_EFFECTS as _TOOL_EFFECTS, TOOL_NAME_ARG as _VM_NAME_ARG
except ImportError:
    _TOOL_EFFECTS, _VM_NAME_ARG = {}, {"clone_vm": "new_name"}


class ActiveLibrary:
    """In-memory snapshot of all system objects + derived relation indices."""

    def __init__(self) -> None:
        self._vms:       Dict[str, Dict[str, Any]] = {}
        self._profiles:  Dict[str, Dict[str, Any]] = {}
        self._networks:  Dict[str, Dict[str, Any]] = {}
        self._templates: Dict[str, Dict[str, Any]] = {}
        self._isos:      List[Dict[str, Any]]       = []
        # Append-only session transaction/event log — what HAPPENED, alongside the
        # current-state registry above (what IS). The seed of the Conductor ledger;
        # lives here until the ledger exists. Every apply() records one entry.
        self._transactions: List[Dict[str, Any]] = []
        self.built = False

    # ── record builders ────────────────────────────────────────────────────────
    @staticmethod
    def _vm_record(name: str, mgr) -> Optional[Dict[str, Any]]:
        """Full per-VM record (config relations + runtime status) for one VM.

        Returns None if the VM's config can't be loaded (e.g. just deleted).
        """
        try:
            from executor.api.qemu_config import MachineConfig
            from executor.api._vm_monitoring import _config_flags
            cfg = MachineConfig.load(name)
        except Exception:
            return None
        try:
            status = mgr.vm_status(name).get("state", "unknown")
        except Exception:
            status = "unknown"
        return {
            "name":        name,
            "os_type":     cfg.os_type,
            "os_name":     cfg.os_name or cfg.os_type,
            "arch":        cfg.machine_arch,
            "status":      status,
            "cpu_cores":   cfg.cpu_cores,
            "memory_mb":   cfg.memory_mb,
            "disks":       len(cfg.disks),
            "labels":      list(cfg.labels),
            "flags":       _config_flags(cfg),
            "template":    cfg.template,          # golden image this VM cloned from
            "guest_agent": cfg.guest_agent,
        }

    # ── full build (session start) ─────────────────────────────────────────────
    def snapshot(self, manager=None) -> "ActiveLibrary":
        """Build the whole registry from scratch. Call once at session start."""
        mgr = manager or _local_manager()
        self._vms, self._profiles, self._networks, self._templates, self._isos = {}, {}, {}, {}, []
        self._transactions = []
        if mgr is None:
            self.built = False   # remote/unavailable — callers fall back to a live query
            return self
        self.built = True
        try:
            for row in mgr.list_vms():
                rec = self._vm_record(row["name"], mgr)
                if rec:
                    self._vms[row["name"]] = rec
        except Exception:
            pass
        self._refresh_profiles()
        self._refresh_networks(mgr)
        self._refresh_templates(mgr)
        return self

    def _refresh_profiles(self) -> None:
        try:
            from executor.api.profiles import get_all_profiles
            self._profiles = dict(get_all_profiles())
        except Exception:
            self._profiles = {}

    def _refresh_networks(self, mgr) -> None:
        try:
            self._networks = {n["name"]: n for n in mgr.list_networks()}
        except Exception:
            self._networks = {}

    def _refresh_templates(self, mgr) -> None:
        try:
            self._templates = {t["name"]: t for t in mgr.list_templates()}
        except Exception:
            self._templates = {}

    def _refresh_isos(self, mgr) -> None:
        try:
            r = mgr.scan_isos()
            self._isos = r.get("isos", r) if isinstance(r, dict) else (r or [])
        except Exception:
            pass

    # ── targeted updates ───────────────────────────────────────────────────────
    def update_vm(self, name: str, manager=None) -> None:
        """Reload one VM's full record (add on create, refresh on change)."""
        mgr = manager or _local_manager()
        if mgr is None:
            return
        rec = self._vm_record(name, mgr)
        if rec:
            self._vms[name] = rec
        else:
            self._vms.pop(name, None)      # config gone → treat as removed

    def update_vm_status(self, name: str, manager=None) -> None:
        """Refresh only the status field of one VM (cheap — launch/stop)."""
        mgr = manager or _local_manager()
        if name not in self._vms:
            return self.update_vm(name, mgr)   # not tracked yet → full add
        if mgr is None:
            return
        try:
            self._vms[name]["status"] = mgr.vm_status(name).get("state", "unknown")
        except Exception:
            pass

    def remove_vm(self, name: str) -> None:
        self._vms.pop(name, None)

    def apply(self, tool_name: str, args: Dict[str, Any], manager=None, result=None) -> bool:
        """Post-execution hook: record the transaction + targeted state update.

        Records EVERY executed tool call (incl. read-only ones) in the session
        transaction log, then applies the targeted compartment update. Returns True
        if the Library's STATE was updated, False for read-only/unknown tools (the
        transaction is still logged either way).
        """
        self._record_transaction(tool_name, args, result)
        effects = _TOOL_EFFECTS.get(tool_name)
        if not effects:
            return False
        mgr = manager or _local_manager()
        name = args.get(_VM_NAME_ARG.get(tool_name, "name"))
        for effect in effects:
            if effect == "vm_reload" and name:
                self.update_vm(name, mgr)
            elif effect == "vm_status" and name:
                self.update_vm_status(name, mgr)
            elif effect == "vm_remove" and name:
                self.remove_vm(name)
            elif effect == "profiles":
                self._refresh_profiles()
            elif effect == "networks":
                self._refresh_networks(mgr)
            elif effect == "templates":
                self._refresh_templates(mgr)
            elif effect == "isos":
                self._refresh_isos(mgr)
            elif effect == "fleet_members" and mgr:
                # A fleet stop/launch changes the status of every VM carrying the
                # label — refresh just those, keyed off the label (fleet has no
                # single "name" arg).
                label = args.get("label")
                if label:
                    try:
                        for _vm in mgr.list_vms(label=label):
                            self.update_vm_status(_vm["name"], mgr)
                    except Exception:
                        pass
        return True

    # ── transaction / event log (what HAPPENED) ────────────────────────────────
    _TX_ARG_KEYS = ("name", "new_name", "label", "action", "command", "os_type")

    def _record_transaction(self, tool_name: str, args: Dict[str, Any], result: Any) -> None:
        """Append one executed tool call to the session log (compact, whitelisted args)."""
        ok = None
        err = None
        if isinstance(result, dict):
            ok = result.get("success", True) is not False and not result.get("error")
            err = result.get("error")
        entry: Dict[str, Any] = {
            "t":    time.time(),
            "tool": tool_name,
            "args": {k: v for k, v in (args or {}).items() if k in self._TX_ARG_KEYS},
            "ok":   ok,
        }
        if err:
            entry["error"] = err
        self._transactions.append(entry)

    def transactions(self) -> List[Dict[str, Any]]:
        """The full session transaction/event log, oldest first."""
        return self._transactions

    def recent_transactions(self, n: int = 8) -> List[Dict[str, Any]]:
        """The last n transactions (most recent last)."""
        return self._transactions[-n:]

    # ── relation indices (derived on demand) ───────────────────────────────────
    def fleets(self) -> Dict[str, List[str]]:
        """label/flag → member VM names (the fleet groupings)."""
        out: Dict[str, List[str]] = {}
        for name, r in self._vms.items():
            for tag in set(r.get("labels", [])) | set(r.get("flags", [])):
                out.setdefault(tag, []).append(name)
        return {k: sorted(v) for k, v in out.items()}

    def by_os(self) -> Dict[str, List[str]]:
        """os_type → VM names."""
        out: Dict[str, List[str]] = {}
        for name, r in self._vms.items():
            out.setdefault(r.get("os_type", "?"), []).append(name)
        return out

    def template_instances(self) -> Dict[str, List[str]]:
        """template (golden image) → VM names cloned from it."""
        out: Dict[str, List[str]] = {}
        for name, r in self._vms.items():
            t = r.get("template")
            if t:
                out.setdefault(t, []).append(name)
        return out

    def by_network(self) -> Dict[str, List[str]]:
        """network → member VM names."""
        return {net: list(rec.get("members", [])) for net, rec in self._networks.items()}

    # ── accessors for the context-assistant (full ground truth) ────────────────
    def known_names(self) -> Set[str]:
        return set(self._vms)

    def resolve(self, ref: str) -> Optional[Dict[str, Any]]:
        """Return a VM record by name (case-insensitive), or None."""
        if ref in self._vms:
            return self._vms[ref]
        low = ref.lower()
        for name, rec in self._vms.items():
            if name.lower() == low:
                return rec
        return None

    def vms(self) -> Dict[str, Dict[str, Any]]:
        return self._vms

    # ── projection for the AI (compact system-prompt digest) ───────────────────
    def ai_digest(self) -> str:
        """A terse, weak-model-friendly view of current state + relations."""
        if not self._vms and not self.built:
            return ""
        lines: List[str] = []
        lines.append("KNOWN VMS — resolve any VM reference (e.g. \"same OS as test1\") "
                     "against this list; never invent VM names or OSes:")
        if self._vms:
            for name in sorted(self._vms):
                r = self._vms[name]
                extra = ""
                tags = sorted(set(r.get("labels", [])) | set(r.get("flags", [])))
                if tags:
                    extra += f" tags={','.join(tags)}"
                if r.get("template"):
                    extra += f" from={r['template']}"
                lines.append(f"- {name}: os={r.get('os_name')} type={r.get('os_type')} "
                             f"status={r.get('status')}{extra}")
        else:
            lines.append("- (none)")

        fleets = self.fleets()
        if fleets:
            lines.append("FLEETS (label/flag → members): " +
                         "; ".join(f"{k}=[{', '.join(v)}]" for k, v in sorted(fleets.items())))
        nets = {n: m for n, m in self.by_network().items() if m}
        if nets:
            lines.append("NETWORKS (network → members): " +
                         "; ".join(f"{k}=[{', '.join(v)}]" for k, v in sorted(nets.items())))
        # Profiles are intentionally omitted here — the system prompt already
        # injects the profile catalogue, and duplicating ~50 names would bloat a
        # weak model's context for no gain. The full profile set stays in the
        # Library (self._profiles) for the context-assistant's ground truth.
        if self._templates:
            lines.append("TEMPLATES: " + ", ".join(sorted(self._templates)))
        if self._transactions:
            def _fmt(e: Dict[str, Any]) -> str:
                a = e.get("args", {})
                label = a.get("name") or a.get("new_name") or a.get("label") or a.get("action") or ""
                mark = "" if e.get("ok") in (True, None) else "✗"
                return f"{e['tool']}({label}){mark}" if label else f"{e['tool']}{mark}"
            lines.append("RECENT ACTIONS this session (oldest→newest, ✗=failed): " +
                         " · ".join(_fmt(e) for e in self._transactions[-6:]))
        return "\n".join(lines)


# Process-wide singleton — the one source both the AI and the assistant read.
LIBRARY = ActiveLibrary()

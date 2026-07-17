"""
command_catalog.py — Single source of truth for user-facing commands.

One hand-written list drives BOTH help surfaces:
  - the terminal help (`gorgon help`): command + args + description, filtered
    to the tools the executor currently allows.
  - the AI-chat CLI help: the same, plus a short example prompt per command.

Each entry is a dict:
  command     verb typed in the terminal ("launch"); "" for AI-only capabilities.
  tools       executor tool name(s) this maps to; used to filter the list against
              the allowed-tools list. Empty => client-side op (fetch/bundle), always shown.
  args        argument syntax shown after the verb.
  desc        one line: what it does / how it works.
  related     alias / trigger words (also fed to the shortcut matcher).
  ai_example  a short natural-language prompt for the AI (shown only in the CLI help).
  category    grouping header.
  feature     non-standard VM parameter this command requires (e.g. "stealth"), or None.
  terminal    False for AI-only capabilities that have no terminal verb (e.g. create).

Keep this list in sync with the dispatch tables in client/cli/commands.py and
orchestrator/ai/direct_cli.py — it is the authored list they should both render.
"""
from typing import Any, Dict, List

COMMAND_CATALOG: List[Dict[str, Any]] = [
    # ── VM lifecycle ────────────────────────────────────────────────────────
    {"command": "create", "tools": ["create_vm"], "args": "<name> …", "terminal": False,
     "desc": "Create a new VM (AI chat only — describe the machine you want).",
     "related": ["create", "new", "make", "build", "spin up"],
     "ai_example": "create a Ubuntu VM called dev with 4GB RAM", "category": "VM lifecycle"},
    {"command": "list", "tools": ["list_vms"], "args": "",
     "desc": "List all VMs on the server.",
     "related": ["list", "vms", "ls", "list vms", "show vms", "show all"],
     "ai_example": "list my vms", "category": "VM lifecycle"},
    {"command": "status", "tools": ["vm_status"], "args": "<vm>",
     "desc": "Show one VM's status and resource usage.",
     "related": ["status", "info", "state"],
     "ai_example": "what's the status of dev", "category": "VM lifecycle"},
    {"command": "monitor", "tools": ["monitor_vm"], "args": "[vm|all]",
     "desc": "Live resource monitor (defaults to all VMs).",
     "related": ["monitor", "watch", "top"],
     "ai_example": "monitor dev", "category": "VM lifecycle"},
    {"command": "launch", "tools": ["launch_vm"], "args": "<vm> [sdl|vnc]",
     "desc": "Start a VM, optionally choosing the display backend.",
     "related": ["launch", "start", "boot", "run", "power on"],
     "ai_example": "launch dev", "category": "VM lifecycle"},
    {"command": "stop", "tools": ["stop_vm"], "args": "<vm>",
     "desc": "Gracefully stop a running VM.",
     "related": ["stop", "shutdown", "halt", "power off", "kill"],
     "ai_example": "stop dev", "category": "VM lifecycle"},
    {"command": "clone", "tools": ["clone_vm"], "args": "<src> <dst>",
     "desc": "Copy an existing VM to a new name.",
     "related": ["clone", "copy", "duplicate"],
     "ai_example": "clone dev as dev-backup", "category": "VM lifecycle"},
    {"command": "", "tools": ["mark_as_template"], "args": "<vm>", "terminal": False,
     "desc": "Turn a stopped VM into a reusable golden-image template (AI chat only).",
     "related": ["mark as template", "make template", "save as template"],
     "ai_example": "mark vm_perfect_kali as a template", "category": "VM lifecycle"},
    {"command": "", "tools": ["remove_template"], "args": "<template>", "terminal": False,
     "desc": "Delete a template's golden disk copy and un-tag the source VM (asks to confirm).",
     "related": ["remove template", "delete template", "unmark template"],
     "ai_example": "remove the template mark from vm_perfect_kali", "category": "VM lifecycle"},
    {"command": "delete", "tools": ["delete_vm"], "args": "<vm>",
     "desc": "Delete a VM and its disk (asks to confirm).",
     "related": ["delete", "remove", "destroy", "wipe"],
     "ai_example": "delete dev", "category": "VM lifecycle"},

    # ── Fleet ───────────────────────────────────────────────────────────────
    {"command": "label", "tools": ["add_label", "remove_label", "list_labels"],
     "args": "add|remove <vm> <label>  |  list",
     "desc": "Add or remove a VM's fleet label, or list all labels and their members.",
     "related": ["label", "tag", "untag", "unlabel", "group vm", "add label", "remove label"],
     "ai_example": "tag hackerman with redteam", "category": "Fleet"},
    {"command": "fleet", "tools": ["fleet"],
     "args": "[label] [exec <cmd> | stop | launch | ping | status]",
     "desc": "Broadcast one action across every VM in a labeled fleet; no args lists your fleets.",
     "related": ["fleet", "broadcast", "group", "run on all", "act on all", "whole fleet"],
     "ai_example": "run 'uptime' on all my redteam VMs", "category": "Fleet"},

    # ── Disk & snapshots ────────────────────────────────────────────────────
    {"command": "resize", "tools": ["resize_disk"], "args": "<vm> <gb>",
     "desc": "Grow the VM's disk to <gb> GB.",
     "related": ["resize", "grow", "expand", "enlarge disk"],
     "ai_example": "resize dev to 80gb", "category": "Disk & snapshots"},
    {"command": "snapshot", "tools": ["snapshot_create", "snapshot_list",
                                      "snapshot_restore", "snapshot_delete"],
     "args": "list|create|restore|delete <vm> [tag]",
     "desc": "List, create, restore, or delete VM snapshots.",
     "related": ["snapshot", "snap", "checkpoint"],
     "ai_example": "create snapshot of dev called pre-update", "category": "Disk & snapshots"},

    # ── Networking ──────────────────────────────────────────────────────────
    {"command": "network", "tools": ["list_networks", "create_network",
                                     "delete_network", "add_vm_to_network"],
     "args": "list|create|delete|add [args]",
     "desc": "Manage virtual networks and attach VMs to them.",
     "related": ["network", "net", "networking"],
     "ai_example": "attach dev to the isolated network", "category": "Networking"},

    # ── Inspect ─────────────────────────────────────────────────────────────
    {"command": "config", "tools": ["show_config"], "args": "<vm>",
     "desc": "Show the VM's config JSON.",
     "related": ["config", "show config", "settings"],
     "ai_example": "show dev's config", "category": "Inspect"},
    {"command": "show-cmd", "tools": ["print_command"], "args": "<vm>",
     "desc": "Print the full QEMU command for a VM.",
     "related": ["show-cmd", "qemu command", "command line"],
     "ai_example": "show me the qemu command for dev", "category": "Inspect"},
    {"command": "system", "tools": ["check_system"], "args": "",
     "desc": "Show host capabilities (KVM, CPU, RAM, arch).",
     "related": ["system", "system info", "check system", "capabilities"],
     "ai_example": "check the system", "category": "Inspect"},
    {"command": "isos", "tools": ["scan_isos"], "args": "",
     "desc": "List available install ISOs.",
     "related": ["isos", "images", "list isos"],
     "ai_example": "what isos are available", "category": "Inspect"},
    {"command": "profiles", "tools": ["list_profiles"], "args": "",
     "desc": "List hardware profiles.",
     "related": ["profiles", "list profiles", "show profiles"],
     "ai_example": "list the hardware profiles", "category": "Inspect"},
    {"command": "templates", "tools": ["list_templates"], "args": "",
     "desc": "List golden-image templates.",
     "related": ["templates", "list templates", "show templates"],
     "ai_example": "what templates do I have", "category": "Inspect"},
    {"command": "check-profile", "tools": ["check_profile_compatibility"], "args": "<name>",
     "desc": "Check a hardware profile against this host.",
     "related": ["check-profile", "profile compatibility"],
     "ai_example": "is the dell_g15 profile compatible", "category": "Inspect"},

    # ── Stealth (stealth VMs only) ──────────────────────────────────────────
    # guest-setup / setup-done are client-side ops (manager-direct, not server-
    # dispatched tools) → tools:[] like bundle/fetch, so they don't claim a tool
    # name absent from the registry.
    {"command": "guest-setup", "tools": [], "args": "<vm>",
     "desc": "Generate/serve the in-guest stealth script (stealth VMs only).",
     "related": ["guest-setup", "stealth script", "guest stealth"],
     "ai_example": "generate the guest stealth setup for work-laptop",
     "category": "Stealth", "feature": "stealth"},
    {"command": "setup-done", "tools": [], "args": "<vm>",
     "desc": "Mark in-guest stealth setup complete (stealth VMs only).",
     "related": ["setup-done", "stealth done", "mark done"],
     "ai_example": "mark stealth setup done for work-laptop",
     "category": "Stealth", "feature": "stealth"},

    # ── Transfer (client-side; not executor tools) ──────────────────────────
    {"command": "fetch", "tools": [], "args": "<vm> [dest]",
     "desc": "Download a VM disk from the server (SHA256 verified).",
     "related": ["fetch", "download", "pull disk"],
     "ai_example": "download dev's disk", "category": "Transfer"},
    {"command": "bundle", "tools": [], "args": "<vm> [dest_dir]",
     "desc": "Download an entire VM folder (disk + config) as a zip.",
     "related": ["bundle", "export", "download folder"],
     "ai_example": "bundle dev to my desktop", "category": "Transfer"},
]

# Header order for grouping in the rendered help.
CATEGORY_ORDER: List[str] = [
    "VM lifecycle", "Fleet", "Disk & snapshots", "Networking",
    "Inspect", "Stealth", "Transfer",
]


# ── CANONICAL TOOL REGISTRY (single source of truth for the tool regime) ─────────
# One authored place for every executor tool + its metadata. Everything that used
# to keep its own hand-maintained copy (server _KNOWN_TOOLS, executor_client
# _VM_TOOLS, active_library _TOOL_EFFECTS, tool_executor _REVERT_AWARE_TOOLS, the
# gate's required-fields, the confirm maps) DERIVES from this via the accessors
# below — add a tool HERE once and every consumer updates. Keys MUST match
# tool_executor._run's dispatch exactly (asserted by tests/test_tool_registry.py).
#
# Per tool:  req = required arg names · vm = operates on a specific existing VM
# (allowlist-scoped) · effect = Active-Library compartment to refresh after it runs
# (None = read-only) · rev = revert-aware (mutating) · confirm = chat confirmation
# policy ("yn" | "name" | "critical" | "fleet" | None).
TOOL_SPECS: Dict[str, Dict[str, Any]] = {
    "add_label":                     {"req": ["name", "label"],          "vm": True,  "effect": ("vm_reload",),            "rev": False, "confirm": None},
    "add_vm_to_network":             {"req": ["net_name", "vm_name"],    "vm": True,  "effect": ("networks",),            "rev": False, "confirm": None},
    "check_disk":                    {"req": ["name"],                    "vm": True,  "effect": None,                     "rev": False, "confirm": None},
    "check_profile_compatibility":   {"req": ["profile_name"],           "vm": False, "effect": None,                     "rev": False, "confirm": None},
    "check_system":                  {"req": [],                          "vm": False, "effect": None,                     "rev": False, "confirm": None},
    "clarify":                       {"req": ["question"],               "vm": False, "effect": None,                     "rev": False, "confirm": None},
    "clone_vm":                      {"req": ["source_name", "new_name"],"vm": True,  "effect": ("vm_reload",),            "rev": True,  "confirm": "yn"},
    "create_network":                {"req": ["net_name"],               "vm": False, "effect": ("networks",),            "rev": True,  "confirm": None},
    "create_profile":                {"req": ["profile_name", "description"], "vm": False, "effect": ("profiles",),        "rev": True,  "confirm": None},
    "create_vm":                     {"req": ["name", "os_type"],        "vm": False, "effect": ("vm_reload",),            "rev": True,  "confirm": "yn"},
    "delete_network":                {"req": ["net_name"],               "vm": False, "effect": ("networks",),            "rev": True,  "confirm": "name"},
    "delete_profile":                {"req": ["profile_name"],           "vm": False, "effect": ("profiles",),            "rev": False, "confirm": "name"},
    "delete_vm":                     {"req": ["name"],                    "vm": True,  "effect": ("vm_remove",),            "rev": True,  "confirm": "critical"},
    "fingerprint_vm":                {"req": ["name"],                    "vm": True,  "effect": None,                     "rev": False, "confirm": None},
    "fleet":                         {"req": ["label", "action"],        "vm": False, "effect": ("fleet_members",),        "rev": False, "confirm": "fleet"},
    "generate_guest_agent_setup":    {"req": ["name"],                    "vm": True,  "effect": None,                     "rev": False, "confirm": None},
    "get_vm_logs":                   {"req": ["name"],                    "vm": True,  "effect": None,                     "rev": False, "confirm": None},
    "guest_ping":                    {"req": ["name"],                    "vm": True,  "effect": None,                     "rev": False, "confirm": None},
    "launch_vm":                     {"req": ["name"],                    "vm": True,  "effect": ("vm_status",),            "rev": True,  "confirm": "yn"},
    "list_labels":                   {"req": [],                          "vm": False, "effect": None,                     "rev": False, "confirm": None},
    "list_networks":                 {"req": [],                          "vm": False, "effect": None,                     "rev": False, "confirm": None},
    "list_profiles":                 {"req": [],                          "vm": False, "effect": None,                     "rev": False, "confirm": None},
    "list_templates":                {"req": [],                          "vm": False, "effect": None,                     "rev": False, "confirm": None},
    "list_vms":                      {"req": [],                          "vm": False, "effect": None,                     "rev": False, "confirm": None},
    "mark_as_template":              {"req": ["name"],                    "vm": True,  "effect": ("vm_reload", "templates"),"rev": False, "confirm": None},
    "monitor_vm":                    {"req": ["name"],                    "vm": True,  "effect": None,                     "rev": False, "confirm": None},
    "open_display":                  {"req": ["name"],                    "vm": True,  "effect": None,                     "rev": False, "confirm": None},
    "open_shell":                    {"req": ["name"],                    "vm": True,  "effect": None,                     "rev": False, "confirm": None},
    "print_command":                 {"req": ["name"],                    "vm": True,  "effect": None,                     "rev": False, "confirm": None},
    "provision_guest_agent_offline": {"req": ["name"],                    "vm": True,  "effect": ("vm_reload",),            "rev": False, "confirm": None},
    "remove_label":                  {"req": ["name", "label"],          "vm": True,  "effect": ("vm_reload",),            "rev": False, "confirm": None},
    "remove_template":               {"req": ["name"],                    "vm": True,  "effect": ("vm_reload", "templates"),"rev": False, "confirm": None},
    "resize_disk":                   {"req": ["name", "new_size_gb"],    "vm": True,  "effect": ("vm_reload",),            "rev": True,  "confirm": "yn"},
    "revert":                        {"req": [],                          "vm": False, "effect": None,                     "rev": True,  "confirm": None},
    "run_guest_command":             {"req": ["name", "command"],        "vm": True,  "effect": None,                     "rev": False, "confirm": None},
    "scan_isos":                     {"req": [],                          "vm": False, "effect": ("isos",),                "rev": False, "confirm": None},
    "send_monitor_cmd":              {"req": ["name", "cmd"],            "vm": True,  "effect": None,                     "rev": False, "confirm": None},
    "set_resource_limits":           {"req": ["name"],                    "vm": True,  "effect": ("vm_reload",),            "rev": False, "confirm": "yn"},
    "show_config":                   {"req": ["name"],                    "vm": True,  "effect": None,                     "rev": False, "confirm": None},
    "snapshot_create":               {"req": ["name", "snap_name"],      "vm": True,  "effect": None,                     "rev": True,  "confirm": None},
    "snapshot_delete":               {"req": ["name", "snap_name"],      "vm": True,  "effect": None,                     "rev": True,  "confirm": "name"},
    "snapshot_list":                 {"req": ["name"],                    "vm": True,  "effect": None,                     "rev": False, "confirm": None},
    "snapshot_restore":              {"req": ["name", "snap_name"],      "vm": True,  "effect": None,                     "rev": True,  "confirm": "name"},
    "stop_vm":                       {"req": ["name"],                    "vm": True,  "effect": ("vm_status",),            "rev": True,  "confirm": "yn"},
    "update_config":                 {"req": ["name", "updates"],        "vm": True,  "effect": ("vm_reload",),            "rev": True,  "confirm": "yn"},
    "vm_status":                     {"req": ["name"],                    "vm": True,  "effect": None,                     "rev": False, "confirm": None},
}

# Derived views — consumers import THESE, never hand-maintained copies.
KNOWN_TOOLS:      frozenset       = frozenset(TOOL_SPECS)
VM_SCOPED_TOOLS:  frozenset       = frozenset(t for t, s in TOOL_SPECS.items() if s["vm"])
REVERT_TOOLS:     frozenset       = frozenset(t for t, s in TOOL_SPECS.items() if s["rev"])
TOOL_EFFECTS:     Dict[str, Any]  = {t: s["effect"] for t, s in TOOL_SPECS.items() if s["effect"]}
REQUIRED_FIELDS:  Dict[str, list] = {t: s["req"] for t, s in TOOL_SPECS.items() if s["req"]}

# Single-source link, enforced: TOOL_SPECS is THE authority for which tools exist;
# COMMAND_CATALOG only REFERENCES them. A command may name only registry tools
# (client-side commands use tools:[]). Fails LOUD at import if the catalog ever
# drifts from the registry — you can't reference a tool that isn't real.
_unknown_refs = {t for e in COMMAND_CATALOG for t in e.get("tools", []) if t not in TOOL_SPECS}
assert not _unknown_refs, f"command_catalog references non-registry tools: {sorted(_unknown_refs)}"

# Trigger words per TOOL, derived from each command's `related` words mapped
# through its tools — the single source for the context-assistant's tool hints
# (so adding a command's alias updates the assistant automatically).
def tool_trigger_words() -> Dict[str, list]:
    """command `related` words → per-tool trigger lists (derived, not hand-kept)."""
    out: Dict[str, list] = {}
    for e in COMMAND_CATALOG:
        words = e.get("related") or []
        for t in (e.get("tools") or []):
            out.setdefault(t, [])
            for w in words:
                if w not in out[t]:
                    out[t].append(w)
    return out

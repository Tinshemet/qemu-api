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
    {"command": "guest-setup", "tools": ["generate_guest_setup"], "args": "<vm>",
     "desc": "Generate/serve the in-guest stealth script (stealth VMs only).",
     "related": ["guest-setup", "stealth script", "guest stealth"],
     "ai_example": "generate the guest stealth setup for work-laptop",
     "category": "Stealth", "feature": "stealth"},
    {"command": "setup-done", "tools": ["setup_done"], "args": "<vm>",
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

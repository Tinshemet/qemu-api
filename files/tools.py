"""
tools.py — AI Tool Schema Definitions Layer

The JSON schema list passed to Ollama so the model knows what functions
it can call. One entry per tool — no duplicates.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "clarify",
            "description": "Ask the user for a specific missing required piece of information. Only use for truly required fields that cannot be defaulted. Ask ONE thing at a time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options":  {"type": "array", "items": {"type": "string"}},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_system",
            "description": "Check host system capabilities: KVM, OVMF, QEMU, CPU, RAM, disk space.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_isos",
            "description": "Scan common directories for ISO files the user might want to use.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_vms",
            "description": "List all VMs with status.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_profiles",
            "description": "List all hardware profiles including custom ones.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_profile_compatibility",
            "description": "Check if a hardware profile is compatible with this system.",
            "parameters": {
                "type": "object",
                "properties": {"profile_name": {"type": "string"}},
                "required": ["profile_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_profile",
            "description": "Create and save a custom hardware profile.",
            "parameters": {
                "type": "object",
                "properties": {
                    "profile_name":  {"type": "string"},
                    "description":   {"type": "string"},
                    "machine_class": {"type": "string"},
                    "machine_type":  {"type": "string"},
                    "machine_arch":  {"type": "string"},
                    "qemu_binary":   {"type": "string"},
                    "cpu_model":     {"type": "string"},
                    "cpu_cores":     {"type": "integer"},
                    "cpu_threads":   {"type": "integer"},
                    "memory_mb":     {"type": "integer"},
                    "gpu":           {"type": "string"},
                    "audio":         {"type": "string"},
                    "display":       {"type": "string"},
                    "battery":       {"type": "boolean"},
                    "uefi":          {"type": "boolean"},
                    "bios":          {"type": "string"},
                    "manufacturer":  {"type": "string"},
                    "product_name":  {"type": "string"},
                    "bios_vendor":   {"type": "string"},
                    "bios_version":  {"type": "string"},
                    "notes":         {"type": "string"},
                },
                "required": ["profile_name", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_profile",
            "description": "Delete a custom hardware profile.",
            "parameters": {
                "type": "object",
                "properties": {"profile_name": {"type": "string"}},
                "required": ["profile_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_vm",
            "description": "Create a new VM. Use clarify if name is missing. All other fields have defaults.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":         {"type": "string"},
                    "os_type":      {"type": "string"},
                    "os_name":      {"type": "string"},
                    "profile":      {"type": "string"},
                    "cpu_cores":    {"type": "integer"},
                    "cpu_threads":  {"type": "integer"},
                    "memory_mb":    {"type": "integer"},
                    "disk_size_gb": {"type": "integer"},
                    "disk_format":  {"type": "string"},
                    "iso_path":     {"type": "string"},
                    "display":      {"type": "string"},
                    "gpu":          {"type": "string"},
                    "audio":        {"type": "string"},
                    "network_mode": {"type": "string"},
                    "bridge_iface": {"type": "string"},
                    "mac_address":  {"type": "string"},
                    "cpu_model":    {"type": "string"},
                    "machine_type": {"type": "string"},
                    "manufacturer": {"type": "string"},
                    "product_name": {"type": "string"},
                    "uefi":         {"type": "boolean"},
                    "kvm":          {"type": "boolean"},
                    "battery":      {"type": "boolean"},
                    "hugepages":    {"type": "boolean"},
                    "description":  {"type": "string"},
                    "extra_args":   {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "os_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clone_vm",
            "description": "Clone an existing VM into a new VM with copy-on-write disks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_name": {"type": "string"},
                    "new_name":    {"type": "string"},
                },
                "required": ["source_name", "new_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "launch_vm",
            "description": "Start a VM. Optionally override display mode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":    {"type": "string"},
                    "display": {"type": "string"},
                    "dry_run": {"type": "boolean", "description": "Print command without running"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_vm",
            "description": "Stop a running VM. Use name='all' to stop all VMs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":  {"type": "string"},
                    "force": {"type": "boolean"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vm_status",
            "description": "Get status of a VM.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "monitor_vm",
            "description": "Deep activity report. Use name='all' for all VMs.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_config",
            "description": "Show full VM configuration.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_config",
            "description": "Update config fields of a stopped VM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":    {"type": "string"},
                    "updates": {"type": "object"},
                },
                "required": ["name", "updates"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resize_disk",
            "description": "Resize a VM disk (VM must be stopped).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":        {"type": "string"},
                    "disk_index":  {"type": "integer"},
                    "new_size_gb": {"type": "integer"},
                },
                "required": ["name", "new_size_gb"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snapshot_create",
            "description": "Create a snapshot of a running VM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":      {"type": "string"},
                    "snap_name": {"type": "string"},
                },
                "required": ["name", "snap_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snapshot_list",
            "description": "List all snapshots for a VM.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snapshot_restore",
            "description": "Restore a VM snapshot by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":      {"type": "string"},
                    "snap_name": {"type": "string"},
                },
                "required": ["name", "snap_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "snapshot_delete",
            "description": "Delete a snapshot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":      {"type": "string"},
                    "snap_name": {"type": "string"},
                },
                "required": ["name", "snap_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_resource_limits",
            "description": "Limit CPU% or memory of a running VM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":        {"type": "string"},
                    "cpu_percent": {"type": "integer", "description": "0-100"},
                    "memory_mb":   {"type": "integer"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_network",
            "description": "Create a private isolated network between VMs (no internet access).",
            "parameters": {
                "type": "object",
                "properties": {"net_name": {"type": "string"}},
                "required": ["net_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_network",
            "description": "Delete an isolated network.",
            "parameters": {
                "type": "object",
                "properties": {"net_name": {"type": "string"}},
                "required": ["net_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_networks",
            "description": "List all isolated VM networks.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_vm_to_network",
            "description": "Add a VM to an isolated network.",
            "parameters": {
                "type": "object",
                "properties": {
                    "net_name": {"type": "string"},
                    "vm_name":  {"type": "string"},
                },
                "required": ["net_name", "vm_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_display",
            "description": "Open the display viewer for a running VM.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_shell",
            "description": "Open a serial console in a terminal.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_vm",
            "description": "Delete and remove a VM configuration and optionally its disk files. Use delete_disks=true to also remove disk images. Call this when user says delete, remove, kill, or destroy a VM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":         {"type": "string"},
                    "delete_disks": {"type": "boolean"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_vm_logs",
            "description": (
                "Read a VM launch log and diagnose why it failed. "
                "ALWAYS call this when a VM is stopped unexpectedly, fails to launch, "
                "crashes immediately, or the user asks why a VM failed. "
                "Returns parsed errors, root cause diagnosis, and fix suggestions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name":  {"type": "string", "description": "VM name"},
                    "lines": {"type": "integer", "description": "Log lines to read (default 50)"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "print_command",
            "description": "Print the full QEMU launch command without running it.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_monitor_cmd",
            "description": "Send a raw QEMU monitor command.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "cmd":  {"type": "string"},
                },
                "required": ["name", "cmd"],
            },
        },
    },
]

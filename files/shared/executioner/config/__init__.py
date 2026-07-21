"""
executioner/config — the executor tool layer's config (loader + config.json).

Parses config.json once and exposes each value as a named constant, so the
tool handlers refer to config.VM_DEFS / config.VM_BASE rather than re-parsing or
hardcoding. One folder to read to see everything the executor tool layer is
configured with.
"""

import json
import os

_CFG = json.load(open(os.path.join(os.path.dirname(__file__), "config.json")))

VM_BASE             = _CFG.get("vm_base", "~/.qemu_vms")
VM_DEFS             = _CFG["create_vm_defaults"]
TOOL_DEFS           = _CFG["tool_defaults"]
VALID_MACHINE_TYPES = set(_CFG["valid_machine_types"])
ARM_CPU_PREFIXES    = tuple(_CFG["arm_cpu_prefixes"])
GENERIC_OS_NAMES    = set(_CFG["generic_os_names"])
ISO_ARM_KEYWORDS    = tuple(_CFG.get("arm_iso_keywords", ["arm64", "aarch64", "arm_"]))
ISO_X86_KEYWORDS    = tuple(_CFG.get("x86_iso_keywords", ["amd64", "x86_64", "x64", "i386", "i686"]))

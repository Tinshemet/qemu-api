"""
tool_executor.py — Executor-side tool dispatch.

Owns the QemuManager singleton and the _run() dispatch function.
The full orchestrator pipeline (sanitize → gate → preflight → dispatch)
lives in orchestrator/pipeline.py; this module contains only what the
executor needs, with no orchestrator imports.
"""

import json
import os
import random
import re
import sys
from typing import Any, Dict

# Dedicated RNG for all stealth randomness (persona, serial, disk model, BIOS/
# RAM/CPU variance) so create_vm never perturbs the global `random` state that
# callers and the test suite depend on — global-RNG pollution from library code
# makes downstream randomness order-dependent.
_STEALTH_RNG = random.Random()

with open(os.path.join(os.path.dirname(__file__), "config.json")) as _f:
    _CFG = json.load(_f)
_VM_DEFS             = _CFG["create_vm_defaults"]
_TOOL_DEFS           = _CFG["tool_defaults"]
_VALID_MACHINE_TYPES = set(_CFG["valid_machine_types"])
_ARM_CPU_PREFIXES    = tuple(_CFG["arm_cpu_prefixes"])
_GENERIC_OS_NAMES    = set(_CFG["generic_os_names"])

_ISO_ARM_KEYWORDS    = tuple(_CFG.get("arm_iso_keywords", ["arm64", "aarch64", "arm_"]))
_ISO_X86_KEYWORDS    = tuple(_CFG.get("x86_iso_keywords", ["amd64", "x86_64", "x64", "i386", "i686"]))

from executor.api.qemu_config import (
    MachineConfig, DiskConfig, NetworkConfig,
    OVMF, apply_profile, check_profile_compatibility,
    check_system_capabilities, delete_custom_profile,
    get_all_profiles, list_profiles, save_custom_profile,
)
from executor.api.qemu_manager import QemuManager

# This module has no orchestrator imports — it runs on executor-only machines.
from shared.display import (
    console,
    render_compat, render_monitor, render_profiles,
    render_snapshots, render_status, render_system,
    render_vm_failure, render_vm_list,
)
from executor.fingerprint import tf_report
from rich.panel import Panel

manager = QemuManager()

# Stores the inverse action for the last reversible tool call.
# None means nothing to revert (either no tool ran yet or last tool was irreversible).
_last_revert_action: Dict[str, Any] = {}

# Tools that manage _last_revert_action themselves (set it on success, or
# explicitly clear it) — excluded from the blanket clear below so a failed
# attempt doesn't wipe out a still-valid revert from an earlier success.
_REVERT_AWARE_TOOLS = {
    "revert", "create_vm", "clone_vm", "launch_vm", "stop_vm",
    "create_profile", "update_config", "snapshot_create", "create_network",
    "resize_disk", "snapshot_restore", "snapshot_delete", "delete_network",
    "delete_vm",
}

# ── Stealth persona rotation ──────────────────────────────────────────────────
# Machine classes that are NOT plausible "real computer" personas (Raspberry Pi,
# bare 'minimal' configs) — excluded from the random stealth-persona pool.
_STEALTH_EXCLUDED_CLASSES = {"custom", ""}

# Corporate suffixes / filler that must never be treated as a product-match token
# (otherwise every "…Inc." request collides with every "…Inc." profile).
_IDENTITY_STOPWORDS = {
    "inc", "inc.", "corp", "corp.", "corporation", "ltd", "ltd.", "llc",
    "co", "co.", "company", "the", "international", "gmbh", "technologies",
}


def _plausible_bios_version(manufacturer: str) -> str:
    """Return a vendor-plausible BIOS version for a user-named model not in the
    library — so a coherent firmware version can be synthesised rather than
    borrowed from an unrelated profile.

    Example::

        _plausible_bios_version("Dell Inc.")  # → "1.17.0"
    """
    return f"1.{_STEALTH_RNG.randint(4, 28)}.{_STEALTH_RNG.randint(0, 9)}"


def _apply_within_model_variance(cfg, profile_data: dict, args: dict) -> None:
    """Vary the fields that real units of a model actually differ on — BIOS
    version, installed RAM, CPU core count — within that model's real option
    set, so no two stealth VMs of the same model are byte-identical (beyond the
    already-unique serial and MAC).

    Values stay coherent because they are drawn from the profile's curated
    option lists (real configs that model shipped in), never generated blindly.
    Only fields the caller did not set explicitly are touched.

    Args:
        cfg:          MachineConfig being built (mutated in place).
        profile_data: The applied profile's dict (may carry ``bios_versions``,
                      ``memory_options_mb``, ``cpu_variants``).
        args:         Raw create_vm args — an explicit value here is never overridden.
    """
    import os
    import psutil
    pd = profile_data or {}
    if pd.get("bios_versions") and not args.get("bios_version"):
        cfg.bios_version = _STEALTH_RNG.choice(pd["bios_versions"])
    # RAM: pick from the model's real options, but never exceed ~50% of host RAM.
    # A guest claiming more memory than the host has thrashes and won't boot.
    if pd.get("memory_options_mb") and not args.get("memory_mb"):
        try:
            host_mb = psutil.virtual_memory().total // (1024 * 1024)
        except Exception:
            host_mb = 8192
        cap  = max(2048, int(host_mb * 0.5))
        opts = [m for m in pd["memory_options_mb"] if m <= cap] or [min(pd["memory_options_mb"])]
        cfg.memory_mb = min(_STEALTH_RNG.choice(opts), cap)
    # CPU: cpu_variants are [cores, total_threads]; -smp wants threads PER CORE,
    # so convert (else [4, 8] became 4*8 = 32 vCPUs). Cap total vCPUs to host CPUs
    # to avoid heavy oversubscription.
    if pd.get("cpu_variants") and not args.get("cpu_cores"):
        cores, total = _STEALTH_RNG.choice(pd["cpu_variants"])
        tpc       = max(1, total // cores)
        host_cpus = os.cpu_count() or 4
        if cores * tpc > host_cpus:
            cores = max(1, host_cpus // tpc)
        cfg.cpu_cores, cfg.cpu_threads = cores, tpc


# Real SATA SSD model strings — a stealth disk (SATA ide-hd, which supports
# model=) reports one of these so lsblk / inxi -D / smartctl show a believable
# drive instead of "QEMU NVMe Ctrl" (NVMe's model is a fixed, unspoofable tell)
# or a virtio /dev/vd* device.
_STEALTH_DISK_MODELS = [
    "Samsung SSD 870 EVO 500GB", "Samsung SSD 860 EVO 1TB", "Samsung SSD 870 QVO 1TB",
    "CT500MX500SSD1", "CT1000MX500SSD1", "WDC WDS500G2B0A-00SM50",
    "KINGSTON SA400S37480G", "SanDisk SDSSDH3-1T00G", "SK hynix SC401 SATA 512GB",
    "INTEL SSDSC2KW256G8", "Micron_1100_MTFDDAK512TBN", "TOSHIBA THNSNK256GVN8",
]


def _generate_disk_model() -> str:
    """Pick a random real SATA SSD model string for a stealth VM's disk, so the
    drive reads as believable hardware to lsblk / inxi -D / smartctl."""
    return _STEALTH_RNG.choice(_STEALTH_DISK_MODELS)


def _pick_stealth_persona(form_factor: str = "", os_type: str = "") -> str:
    """Pick a random realistic hardware-profile name for a stealth VM.

    Assigning a fresh persona per VM is what lets rotation defeat long-term
    fingerprinting: each stealth VM presents as a different real machine rather
    than one recognisable identity.

    Args:
        form_factor: Optional constraint — "laptop", "desktop", or "server".
                     Empty / "any" / "random" picks across all form factors.
        os_type:     Guest OS — skips personas that pin a different OS (e.g. a
                     Mac mini persona is not handed to a Linux guest).

    Returns:
        A profile name from ``get_all_profiles()``, or "" if none qualify.

    Example::

        _pick_stealth_persona()          # → "office_laptop"  (random each call)
        _pick_stealth_persona("server")  # → "hpe_proliant_dl380"
    """
    ff = (form_factor or "").lower().strip()
    if ff in ("", "any", "random"):
        ff = ""
    guest_os = (os_type or "").lower().strip()
    pool = []
    for name, pd in get_all_profiles().items():
        mc = (pd.get("machine_class") or "").lower()
        if mc in _STEALTH_EXCLUDED_CLASSES:
            continue
        if not pd.get("manufacturer") or not pd.get("product_name"):
            continue
        p_os = (pd.get("os_type") or "").lower()
        if p_os and guest_os and p_os != guest_os:
            continue   # don't present macOS hardware under a Linux/Windows guest
        if ff and mc != ff:
            continue
        pool.append(name)
    return _STEALTH_RNG.choice(pool) if pool else ""


def _generate_stealth_serial(manufacturer: str) -> str:
    """Generate a realistic per-unit serial number in the vendor's format.

    Unique serials per VM are essential for rotation — two VMs of the same model
    must never share a serial (an obvious fingerprinting giveaway).

    Args:
        manufacturer: Vendor name (e.g. "Dell Inc.", "Lenovo", "HP").

    Returns:
        A serial string shaped like that vendor's real serials.

    Example::

        _generate_stealth_serial("Dell Inc.")  # → "7X9QRT2"    (7-char service tag)
        _generate_stealth_serial("HP")         # → "412CD9A7BK" (10-char)
    """
    import string
    m      = (manufacturer or "").lower()
    upper  = string.ascii_uppercase
    alnum  = string.ascii_uppercase + string.digits
    digits = string.digits
    pick   = lambda alpha, n: "".join(_STEALTH_RNG.choice(alpha) for _ in range(n))
    if "dell" in m:
        return pick(alnum, 7)                                     # Dell service tag
    if "lenovo" in m:
        return pick(upper, 2) + pick(alnum, 6)                    # Lenovo: 2 letters + 6
    if "hp" in m or "hewlett" in m or "proliant" in m:
        return pick(digits, 3) + pick(upper, 2) + pick(alnum, 5)  # HP: 10 chars
    if "apple" in m:
        return "C02" + pick(alnum, 8)                            # Apple 11-char
    if "supermicro" in m:
        return "S" + pick(digits, 9)                             # Supermicro
    return pick(alnum, 8)                                        # generic fallback


def _set_revert(tool: str, args: dict, description: str) -> None:
    """Record an inverse action so the next 'revert' call can undo the current tool."""
    global _last_revert_action
    _last_revert_action = {"tool": tool, "args": args, "description": description}


def _clear_revert() -> None:
    """Clear any pending revert action (called before irreversible operations)."""
    global _last_revert_action
    _last_revert_action = {}


_STUB_PLACEHOLDER_VM_NAMES = frozenset()


def _resolve_iso_stub(p: str) -> str:
    return p


def _preflight_check_stub(*a, **k) -> dict:
    return {"action": "ok"}


def _show_preflight_warning_stub(*a, **k) -> None:
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
        vms = manager.list_vms()
        if not verbose:
            render_vm_list(vms)
        return vms

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
        raw_name = args.get("name", "") or ""
        name     = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(raw_name).strip())
        if not name or name.lower() in placeholder_vm_names:
            return {
                "success": False,
                "clarify": True,
                "question": "What would you like to name this VM?",
                "options":  ["my-windows-vm", "dev-machine", "test-ubuntu"],
                "error":    "VM name is required — please provide a unique name.",
                "needs_clarification": "name",
            }
        args["name"] = name

        # Handle overwrite: delete the existing VM before recreating.
        if args.get("overwrite"):
            vm_dir = os.path.expanduser(f"~/.qemu_vms/{name}")
            if os.path.exists(vm_dir):
                result = manager.delete_vm(name, delete_disks=True)
                if not result.get("success"):
                    return {"success": False, "error": f"Could not overwrite '{name}': {result.get('error')}"}

        cfg = MachineConfig(
            name=name,
            os_type=args.get("os_type", "linux"),
            os_name=args.get("os_name", ""),
            description=args.get("description", ""),
        )

        # When explicit SMBIOS fingerprinting fields are present the user is doing
        # manual identity — suppress ALL profile application (auto-matched or AI-passed)
        # so the profile can't override machine_type, cpu_model, or other settings.
        _manual_smbios = any(args.get(f) for f in ("serial_number", "bios_vendor", "chassis_type", "smbios_type"))
        profile = None if _manual_smbios else args.get("profile")
        if not profile and not _manual_smbios:
            # Match a library profile ONLY on a genuine product-name token — never
            # on the manufacturer or "Inc."-style stopwords. The old logic matched
            # any >3-char token, so "MacBook Pro Apple Inc." borrowed the Dell
            # profile via "inc.", yielding an Apple machine with a Dell BIOS.
            req_mfr  = args.get("manufacturer", "").lower()
            prod_kws = [kw for kw in args.get("product_name", "").lower().split()
                        if len(kw) > 2 and kw not in _IDENTITY_STOPWORDS]
            for pname, pdata in get_all_profiles().items():
                p_mfr  = (pdata.get("manufacturer") or "").lower()
                p_prod = (pdata.get("product_name") or "").lower()
                if not (prod_kws and any(kw in p_prod for kw in prod_kws)):
                    continue
                # Vendor must be consistent when the user named one.
                if req_mfr and p_mfr and req_mfr.split()[0] not in p_mfr \
                        and p_mfr.split()[0] not in req_mfr:
                    continue
                profile = pname
                break
        # Stealth with no explicit identity → assign a RANDOM realistic persona so
        # each stealth VM looks like a different real machine. Rotating personas
        # (plus the unique serial/MAC generated below) is what defeats long-term
        # fingerprinting. An optional stealth_persona arg pins the form factor.
        _auto_persona = False
        if not profile and not _manual_smbios and args.get("stealth") \
                and not args.get("manufacturer") and not args.get("product_name"):
            profile = _pick_stealth_persona(args.get("stealth_persona", ""), cfg.os_type)
            _auto_persona = True
        if profile:
            try:
                cfg = apply_profile(cfg, profile)
            except ValueError as e:
                return {"success": False, "error": str(e)}
            # A stealth persona describes GUEST hardware; the host-side display
            # mode (sdl/gtk/vnc/none) is not a fingerprint, so a randomly-picked
            # persona shouldn't dictate it — several personas carry
            # display="none", which would non-deterministically make the VM
            # headless. Keep the deterministic default unless the user explicitly
            # asked for a display.
            if _auto_persona and "display" not in args:
                cfg.display = type(cfg).__dataclass_fields__["display"].default

        for f in ("machine_class", "cpu_model", "cpu_cores", "cpu_threads", "memory_mb",
                  "display", "gpu", "audio", "manufacturer", "product_name", "bios_version",
                  "serial_number", "board_product", "bios_vendor", "smbios_type",
                  "uefi", "kvm", "battery", "hugepages", "machine_type", "os_type", "os_name",
                  "hardened", "stealth", "tpm", "bios"):
            if f in args and args[f] is not None and args[f] != "":
                setattr(cfg, f, args[f])

        if args.get("chassis_type"):
            cfg.smbios_type = args["chassis_type"]

        if args.get("extra_args"):
            cfg.extra_args = args["extra_args"]

        # Opt-in GPU passthrough (vfio-pci) — gives the guest a real GPU's PCI IDs,
        # the only way to defeat the /sys "VMware SVGA" tell. Needs host IOMMU +
        # the GPU bound to vfio-pci. Off unless a host PCI address is supplied.
        if args.get("passthrough_pci"):
            _pt = str(args["passthrough_pci"]).strip()
            # A discrete GPU is usually two functions: .0 (video) + .1 (HDMI audio),
            # in the same IOMMU group. If the caller gives a lone video function,
            # auto-add its audio companion. An explicit comma-list is used as-is.
            if "," not in _pt and _pt.endswith(".0"):
                _pt = f"{_pt},{_pt[:-2]}.1"
            cfg.gpu_passthrough_pci = _pt

        # Opt-in unattended Windows install — attaches a generated autounattend.xml
        # CD (built after create below). WIPES the target disk + creates a local
        # admin account, so it is off unless explicitly requested.
        if args.get("unattended"):
            cfg.unattended           = True
            cfg.unattended_username  = args.get("unattended_username", "")
            cfg.unattended_password  = args.get("unattended_password", "")
            cfg.unattended_locale    = args.get("unattended_locale", "")
            if "unattended_autologon" in args:
                cfg.unattended_autologon = bool(args["unattended_autologon"])

        # stealth implies hardened — __post_init__ only runs at construction so
        # stealth applied via setattr or profile won't have triggered it yet.
        if cfg.stealth:
            cfg.hardened = True
            # The GPU disguise (vmware-svga on Linux, std VGA on Windows) in
            # qemu_arg_builder only fires when gpu=="none" — force it unless
            # the caller explicitly asked for a specific GPU, otherwise every
            # stealth VM silently keeps the default virtio-vga (a VM tell that
            # the guest lspci wrapper's "VMware SVGA II" replacement can't match).
            if "gpu" not in args:
                cfg.gpu = "none"
            # Vary BIOS version / RAM / CPU within this model's real options so no
            # two units of the same model are identical (rotation anti-fingerprint).
            if profile:
                _apply_within_model_variance(cfg, get_all_profiles().get(profile, {}), args)
            # Unique per-unit serial so two rotated VMs of the same model still
            # differ — a serial shared across VMs is itself a fingerprint.
            if not cfg.serial_number:
                cfg.serial_number = _generate_stealth_serial(cfg.manufacturer)
            # Coherent fallback for a user-named model NOT in the library: a Dell
            # must report a Dell BIOS, an Apple an Apple BIOS — never borrow another
            # vendor's firmware (a MacBook with a Dell BIOS is a dead giveaway).
            if cfg.manufacturer and not cfg.bios_vendor:
                cfg.bios_vendor = cfg.manufacturer
            if cfg.manufacturer and not cfg.bios_version:
                cfg.bios_version = _plausible_bios_version(cfg.manufacturer)

        # Windows 11 requires TPM 2.0 — auto-enable unless explicitly disabled.
        if "windows" in cfg.os_type.lower() and not args.get("tpm") is False:
            cfg.tpm = True

        # hardened mode requires q35 (smm=off is only valid on q35);
        # also persist the settings that _harden() enforces at build time.
        if cfg.hardened:
            cfg.machine_type = "q35"
            cfg.balloon      = False
            cfg.hugepages    = False

        # Reject profile names used as machine_type
        if cfg.machine_type:
            mt = cfg.machine_type.lower().split(",")[0].strip()
            if mt not in _VALID_MACHINE_TYPES and not mt.startswith("pc-"):
                cfg.machine_type = "q35"

        # Windows 11 requires UEFI + q35
        if "windows" in cfg.os_type.lower() or "windows" in cfg.os_name.lower():
            cfg.uefi = True
            # Preserve ovmf_ms (Secure Boot) if explicitly set; fall back to plain ovmf
            if cfg.bios not in ("ovmf", "ovmf_ms"):
                cfg.bios = "ovmf"
            if cfg.machine_type not in ("q35",):
                cfg.machine_type = "q35"

        # Reject ARM CPU on x86 VM
        if cfg.machine_arch == "x86_64" and any(
            cfg.cpu_model.lower().startswith(p) for p in _ARM_CPU_PREFIXES
        ):
            cfg.cpu_model = "host"

        # Auto-detect architecture from ISO filename
        iso_hint = args.get("iso_path", "")
        if iso_hint:
            iso_lower = os.path.basename(iso_hint).lower()
            if any(k in iso_lower for k in _ISO_ARM_KEYWORDS):
                cfg.machine_arch  = "aarch64"
                cfg.qemu_binary   = "qemu-system-aarch64"
                cfg.kvm           = False
                cfg.machine_type  = cfg.machine_type if cfg.machine_type in ("virt", "raspi3b") else "virt"
                cfg.bios          = "seabios"
                cfg.uefi          = False
                cfg.hugepages     = False
                if not verbose:
                    console.print("  [yellow]⚠ ARM64 ISO detected — switched to aarch64 VM[/yellow]")

        # Block cross-arch ISO/VM mismatch
        iso_hint = args.get("iso_path", "")
        if iso_hint:
            iso_lower  = os.path.basename(iso_hint).lower()
            is_iso_arm = any(k in iso_lower for k in _ISO_ARM_KEYWORDS)
            is_iso_x86 = any(k in iso_lower for k in _ISO_X86_KEYWORDS)
            if is_iso_arm and cfg.machine_arch == "x86_64":
                return {
                    "success": False,
                    "error": (
                        f"Architecture mismatch — '{os.path.basename(iso_hint)}' is an ARM64 ISO "
                        f"but this VM is x86_64. "
                        f"Either use an x86_64 Windows 11 ISO or create an aarch64 VM."
                    ),
                }
            if is_iso_x86 and cfg.machine_arch in ("aarch64", "arm"):
                return {
                    "success": False,
                    "error": (
                        f"Architecture mismatch — '{os.path.basename(iso_hint)}' is an x86_64 ISO "
                        f"but this VM is ARM. Use an ARM64 ISO instead."
                    ),
                }

        disk_size   = int(args.get("disk_size_gb", _VM_DEFS["disk_size_gb"]))
        disk_format = args.get("disk_format", _VM_DEFS["disk_format"])
        disk_path   = os.path.expanduser(f"~/.qemu_vms/{cfg.name}/disk0.{disk_format}")
        is_windows  = "windows" in cfg.os_type.lower() or "windows" in cfg.os_name.lower()
        # Stealth uses SATA with a spoofed real SSD model: ide-hd exposes model=
        # (so lsblk / inxi -D / smartctl show e.g. "Samsung SSD 870 EVO"), whereas
        # NVMe's controller identify is a fixed "QEMU NVMe Ctrl" tell and virtio-blk
        # exposes /dev/vd* + 1af4 PCI IDs. SATA SSDs are common across real laptops,
        # desktops and servers, so the device type itself stays plausible.
        if args.get("disk_bus"):
            disk_bus = args["disk_bus"]
        elif cfg.stealth:
            disk_bus = "sata"
        elif is_windows:
            disk_bus = "sata"
        else:
            disk_bus = _VM_DEFS.get("disk_bus", "virtio")
        disk_model = args.get("disk_model", "")
        if cfg.stealth and not disk_model:
            disk_model = _generate_disk_model()
        cfg.disks = [DiskConfig(
            path=disk_path, size_gb=disk_size, format=disk_format,
            bus=disk_bus, disk_model=disk_model,
        )]

        net = NetworkConfig(
            mode=args.get("network_mode", _VM_DEFS["network_mode"]),
            bridge=args.get("bridge_iface", _VM_DEFS["bridge"]) or _VM_DEFS["bridge"],
            manufacturer_hint=cfg.manufacturer or args.get("manufacturer", ""),
        )
        if args.get("mac_address"):
            net.mac = args["mac_address"]
        # Stealth NAT: hand the guest a home-router-looking subnet instead of the
        # default 10.0.2.0/24 that betrays QEMU user-mode networking. Only affects
        # NAT mode — bridge already puts the guest on the real LAN. An explicit
        # slirp_subnet arg overrides the stealth default.
        if args.get("slirp_subnet"):
            net.slirp_subnet = args["slirp_subnet"]
        elif cfg.stealth and net.mode == "nat":
            net.slirp_subnet = _VM_DEFS.get("stealth_slirp_subnet", "192.168.1.0/24")
        cfg.networks = [net]

        # Auto-find ISO from distro name when no iso_path was given.
        # os_name is preferred; fall back to the raw os_type before alias conversion
        # (e.g. the AI passes os_type="mint" which sanitizer converts to "linux").
        _distro_hint = (cfg.os_name or raw_os_type or "").lower().strip()
        if _distro_hint and _distro_hint not in _GENERIC_OS_NAMES and not cfg.os_name:
            cfg.os_name = _distro_hint
        if not args.get("iso_path") and _distro_hint and _distro_hint not in _GENERIC_OS_NAMES:
            resolved = resolve_iso(_distro_hint)
            if resolved and os.path.exists(resolved):
                _fname = os.path.basename(resolved).lower()
                _arm_markers = ("arm64", "aarch64", "_arm_", "-arm-")
                _x86_markers = ("amd64", "x86_64", "x64", "i386", "i686", "64bit")
                _iso_is_arm  = any(m in _fname for m in _arm_markers)
                _iso_is_x86  = any(m in _fname for m in _x86_markers)
                _arch_ok = not (
                    (cfg.machine_arch == "x86_64" and _iso_is_arm) or
                    (cfg.machine_arch in ("aarch64", "arm") and _iso_is_x86)
                )
                if _arch_ok:
                    cfg.iso_path = resolved
                    if not verbose:
                        console.print(
                            f"  [cyan]↳ Auto-found ISO for '{_distro_hint}': "
                            f"{os.path.basename(resolved)}[/cyan]"
                        )

        if args.get("iso_path"):
            cfg.iso_path = resolve_iso(args["iso_path"])
        if cfg.machine_class == "laptop" or args.get("battery"):
            cfg.battery = True
        if "windows" in cfg.os_type.lower() and not profile:
            if cfg.bios not in ("ovmf", "ovmf_ms"):
                cfg.bios = "ovmf"
            cfg.uefi = True

        result = manager.create_vm(cfg)
        if not verbose:
            if result.get("success"):
                console.print(f"[green]✓ VM '{result['name']}' created at {result['vm_dir']}[/green]")
                if cfg.stealth:
                    manager.generate_guest_setup(name)
                    console.print(
                        "[dim]  Stealth guest setup script ready"
                        " — will prompt automatically on first launch.[/dim]"
                    )
            else:
                console.print(f"[red]✗ create_vm failed: {result.get('error', 'unknown error')}[/red]")
        if result.get("success"):
            # Opt-in unattended Windows install — build the answer-file CD now
            # (Windows only). The builder attaches it on launch.
            if cfg.unattended:
                is_win = "windows" in cfg.os_type.lower() or "windows" in cfg.os_name.lower()
                if is_win:
                    try:
                        from executor.api._unattend import generate_autounattend_iso
                        generate_autounattend_iso(
                            result["vm_dir"], computer_name=cfg.name,
                            username=cfg.unattended_username, password=cfg.unattended_password,
                            locale=cfg.unattended_locale, autologon=cfg.unattended_autologon,
                        )
                        if not verbose:
                            console.print("[dim]  Unattended answer-file CD generated — "
                                          "Windows installs hands-off on first boot.[/dim]")
                    except Exception as e:
                        console.print(f"[yellow]⚠ unattended CD not generated: {e}[/yellow]")
                elif not verbose:
                    console.print("[yellow]⚠ unattended=true ignored — Windows only.[/yellow]")
            _set_revert("delete_vm", {"name": name}, f"undo create_vm '{name}'")
        return result

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

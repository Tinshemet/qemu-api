"""
_vm_constants.py — Shared constants for QemuManager and its mixins.

Loaded once at import time from config.json in the same directory.
All mixin files import their constants from here to avoid re-parsing.
"""
import json
import os

with open(os.path.join(os.path.dirname(__file__), "config.json")) as _f:
    _CFG = json.load(_f)

_TIMEOUTS              = _CFG["timeouts"]
_BUFFERS               = _CFG["buffers"]
_MACOS_OVMF            = _CFG["ovmf_macos_vars_paths"]
_WIN_OVMF              = _CFG["ovmf_win_vars_paths"]
_LOG_ERROR_PATTERNS    = [tuple(p) for p in _CFG["log_error_patterns"]]
_VALID_MACHINE_TYPES   = set(_CFG["valid_machine_types"])
_UPDATE_ALLOWED_FIELDS = frozenset(_CFG["update_allowed_fields"])
_MONITOR_ALLOWED_CMDS  = tuple(_CFG["monitor_allowed_cmds"])
_LINUX_DISTROS         = _CFG["linux_distros"]
_LOG_DEFAULT_LINES     = _CFG["log_default_lines"]
VM_BASE_DIR            = os.path.expanduser(_CFG["dirs"]["vm_base"])
TEMPLATES_DIR          = os.path.expanduser(_CFG["dirs"]["templates"])
TEMPLATE_LABEL         = _CFG.get("template_label", "template")

_ISO_OS_KEYWORDS: dict = _CFG.get("iso_os_keywords", {})
_WIN_ISO_NAMES:   list = _ISO_OS_KEYWORDS.get("windows", [])
_MACOS_ISO_NAMES: list = _ISO_OS_KEYWORDS.get("macos", [])


def infer_os_name(iso_path: "str | None", os_type: str) -> str:
    """Derive a human-readable OS name from an ISO filename.

    Checks the ISO basename against per-type keyword lists loaded from
    config.json so adding a new distro, Windows version, or macOS release
    only requires editing the config — no code change needed.

    Args:
        iso_path: Path to the attached ISO, or ``None``.
        os_type:  Broad OS type (``"linux"``, ``"windows"``, ``"macos"``).

    Returns:
        Specific name (``"ubuntu"``, ``"windows 11"``, ``"macos sonoma"``)
        or ``os_type`` when no keyword matches.
    """
    if not iso_path:
        return os_type
    needle = os.path.basename(iso_path).lower()
    if os_type == "linux":
        for distro in _LINUX_DISTROS:
            if distro in needle:
                return "mint" if distro == "linuxmint" else distro
    elif os_type == "windows":
        for kw in _WIN_ISO_NAMES:
            if kw in needle:
                # Map "win11"/"win10" to pretty names; plain "windows" → "windows"
                if kw in ("win11", "windows11"):
                    return "windows 11"
                if kw in ("win10", "windows10"):
                    return "windows 10"
                return "windows"
    elif os_type == "macos":
        for kw in _MACOS_ISO_NAMES:
            if kw in needle and kw not in ("macos", "mac", "osx", "darwin"):
                return f"macos {kw}"
        return "macos"
    return os_type

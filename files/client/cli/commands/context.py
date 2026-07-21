"""
commands/context.py — shared machinery for the direct-CLI commands.

Every Command pulls what it needs from here: the console + renderers, the local
QEMU manager (or None on a client-only checkout), connection settings, the
operator-auth helpers, and small utilities. Centralised so each command file
stays focused on its own logic. The names keep their original spellings (e.g.
``_SERVER``, ``_require_manager``) so the extracted command bodies read verbatim.
"""

import getpass

import requests

from rich import box
from rich.panel import Panel
from rich.table import Table

try:
    # Same defensive-import reasoning as the `manager` import below — this
    # package runs on client-only checkouts too, where orchestrator/ may be
    # absent. See operator_gate_ok(): unavailable means "degrade open".
    from orchestrator.auth import store as _auth_store, sessions as _auth_sessions
except ImportError:
    _auth_store    = None
    _auth_sessions = None

try:
    # shared/ isn't part of a true client-only checkout — fall back to plain rich
    # output instead of crashing the whole direct-CLI (even "gorgon help" needs this).
    from shared.display import (
        console,
        render_vm_list,
        render_status,
        render_monitor,
        render_profiles,
        render_templates,
        render_compat,
        render_snapshots,
        render_system,
        render_fleet,
        render_fleets,
    )
except ImportError:
    from rich.console import Console
    console = Console()

    def _render_json(data: object, *_a, **_kw) -> None:
        """Fallback renderer — dump JSON when shared.display isn't importable."""
        console.print_json(data=data, default=str)

    render_vm_list   = _render_json
    render_status    = _render_json
    render_monitor   = _render_json
    render_profiles  = _render_json
    render_templates = _render_json
    render_compat    = _render_json
    render_snapshots = _render_json
    render_system    = _render_json
    render_fleet     = _render_json
    render_fleets    = _render_json

# Connection settings from the shared loader (client/config).
from client import config as _cfg
_SERVER, _TOKEN, _TIMEOUT = _cfg.SERVER, _cfg.TOKEN, _cfg.TIMEOUT
_CA_CERT, _VERIFY, _HEADERS = _cfg.CA_CERT, _cfg.VERIFY, _cfg.HEADERS
_VNC_VIEWERS, _IO_CHUNK = _cfg.VNC_VIEWERS, _cfg.IO_CHUNK

try:
    from executor.api.qemu_config import (
        OVMF,
        check_profile_compatibility,
        check_system_capabilities,
        list_profiles,
    )
except ImportError:
    OVMF = {"available": False}
    # Fallbacks when the executor package isn't in a client-only checkout —
    # return empty data so the direct CLI still imports and runs (server path).
    def list_profiles() -> list:                          # type: ignore[misc]
        """No local profiles when the executor package is absent."""
        return []
    def check_profile_compatibility(*a, **kw) -> dict:    # type: ignore[misc]
        """No local compat data when the executor package is absent."""
        return {}
    def check_system_capabilities() -> dict:              # type: ignore[misc]
        """No local capability data when the executor package is absent."""
        return {}

try:
    from shared.executioner.tool_executor import manager
except ImportError:
    manager = None

# Helpers extracted alongside run() into commands_helpers (client_wrapper imports
# the flag handlers; the command bodies use require_manager / remote / popup).
from client.cli.commands_helpers import (
    _require_manager, _remote_execute, _show_stealth_popup,
    set_custom_mode_flag, fingerprint_report, clear_session_flag,
)


def _allowed_tools() -> "set | None":
    """Return the executor's allowed-tools set for help filtering, or None if unrestricted."""
    try:
        from orchestrator.executor_client import _ALLOWED_TOOLS
        return set(_ALLOWED_TOOLS) or None
    except Exception:
        return None


# login/logout bypass the gate itself; everything else — including "operator"
# management — is held to it. Mirrors orchestrator/ai/direct_cli.py's
# _operator_gate_ok exactly: this is the OTHER in-process, unauthenticated-by-
# default path to `manager` (client_wrapper's `gorgon <cmd>` uses THIS package).
_AUTH_EXEMPT_COMMANDS = {"login", "logout"}


def _operator_gate_ok(cmd: str) -> bool:
    """True if cmd may dispatch: the auth package isn't available (pure client-only
    checkout — degrade open), no operator accounts exist yet (pre-bootstrap), or
    this box holds a valid, unexpired login."""
    if _auth_store is None:
        return True
    if cmd in _AUTH_EXEMPT_COMMANDS:
        return True
    if not _auth_store.operators_exist():
        return True
    return _auth_sessions.current_username() is not None


def _require_operator_password(action: str) -> bool:
    """Re-authenticate the operator for a HIGH-IMPACT change (forging/signing a
    contract, switching the active agent). Stronger than _operator_gate_ok: an
    active session isn't enough — the operator must re-enter their password, so a
    walk-up to an unlocked terminal can't reassign contracts or agents.

    Degrades open only where auth genuinely can't apply: no auth package (client-
    only checkout) or pre-bootstrap (no operators yet). Otherwise it needs a
    logged-in operator AND a correct password. Returns True to proceed.
    """
    if _auth_store is None or not _auth_store.operators_exist():
        return True
    user = _auth_sessions.current_username()
    if not user:
        console.print("[bold red]Login required.[/bold red] Run [cyan]gorgon login[/cyan] first.")
        return False
    pw = getpass.getpass(f"Operator password to {action}: ")
    if _auth_store.verify_password(user, pw):
        return True
    console.print("[bold red]Password incorrect — aborted.[/bold red]")
    return False


def pp(data: object, verbose: bool) -> None:
    """Echo the raw JSON result when running in verbose mode."""
    if verbose:
        import json
        console.print_json(json.dumps(data, default=str))

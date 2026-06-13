"""
ollama_wrapper.py — backward-compatibility shim

All logic has been moved to the layer modules. This file re-exports
everything so existing imports (from ollama_wrapper import ...) keep
working without changes.
"""

from cli           import chat_loop, cli_direct     # noqa: F401
from display       import console, _print_banner    # noqa: F401
from fingerprint   import _tf_report                # noqa: F401
from ollama_client import OLLAMA_URL, OLLAMA_MODEL, _call_ollama, _build_system_prompt  # noqa: F401
from sanitizer     import _sanitise_args, _resolve_iso, _resolve_vm_name               # noqa: F401
from session       import load_session, save_session, clear_session                    # noqa: F401
from tool_executor import execute_tool, manager                                        # noqa: F401
from tools         import TOOLS                                                        # noqa: F401
from validator     import (                                                             # noqa: F401
    _CUSTOM_MODE, set_custom_mode,
    _validate_with_internet, _validate_profile_for_host,
    _preflight_check, _show_preflight_warning,
)

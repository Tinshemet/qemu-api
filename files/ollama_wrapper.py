"""
ollama_wrapper.py — backward-compatibility shim

All logic has been moved to the layer modules. This file re-exports
everything so existing imports (from ollama_wrapper import ...) keep
working without changes.
"""

from ai.cli           import chat_loop, cli_direct     # noqa: F401
from ai.display       import console, _print_banner    # noqa: F401
from ai.fingerprint   import _tf_report                # noqa: F401
from ai.ollama_client import OLLAMA_URL, OLLAMA_MODEL, _call_ollama, _build_system_prompt  # noqa: F401
from sanitizer.sanitizer import _sanitise_args, _resolve_iso, _resolve_vm_name               # noqa: F401
from ai.session       import load_session, save_session, clear_session                    # noqa: F401
from executioner.tool_executor import execute_tool, manager                                        # noqa: F401
from ai.tools         import TOOLS                                                        # noqa: F401
from preflight.validator import (                                                             # noqa: F401
    _CUSTOM_MODE, set_custom_mode,
    _validate_with_internet, _validate_profile_for_host,
    _preflight_check, _show_preflight_warning,
)

if __name__ == "__main__":
    import sys
    argv    = sys.argv[1:]
    verbose = "-v" in argv or "--verbose" in argv
    argv    = [a for a in argv if a not in ("-v", "--verbose")]

    if "-cu" in argv:
        set_custom_mode(True)
        argv = [a for a in argv if a != "-cu"]
        console.print("[dim]Custom mode active — product verification disabled[/dim]")

    if argv:
        cli_direct(argv, verbose=verbose)
    else:
        chat_loop(verbose=verbose)

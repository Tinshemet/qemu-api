"""
ollama_wrapper.py — single re-export surface for all provider + shared symbols.
"""

from server.ai.cli           import chat_loop, cli_direct                    # noqa: F401
from shared.display          import console, _print_banner                   # noqa: F401
from shared.fingerprint      import _tf_report                               # noqa: F401
from server.ai.ollama_client import OLLAMA_URL, OLLAMA_MODEL, _call_ollama, _build_system_prompt  # noqa: F401
from shared.sanitizer.sanitizer import _sanitise_args, _resolve_iso, _resolve_vm_name               # noqa: F401
from server.ai.session       import load_session, save_session, clear_session                      # noqa: F401
from server.executor_client  import execute_tool, API_URL                    # noqa: F401
from server.ai.tools         import TOOLS                                    # noqa: F401
from shared.preflight.validator import (                                       # noqa: F401
    _CUSTOM_MODE, set_custom_mode,
    _validate_with_internet, _validate_profile_for_host,
    _preflight_check, _show_preflight_warning,
)

try:
    from client.executioner.tool_executor import manager                     # noqa: F401
except ImportError:
    manager = None                                                            # noqa: F811

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

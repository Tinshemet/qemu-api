"""
client_wrapper.py — gorgon Client Entry Point

Usage:
    gorgon                        AI chat (connects to server)
    gorgon <cmd> [args...]        Direct local QEMU command (no AI)
    gorgon -v                     AI chat with verbose tool output
    gorgon -cu                    AI chat with product verification disabled
    gorgon -cs                    Clear saved session before starting
    gorgon -tf <name>             Fingerprint report for a VM

Appearance is configured via CLI_config.json (same directory as this file):
    text_color   Hex color for body text, e.g. "#aaaaaa"
    font_size    Terminal font size hint, e.g. 13

Examples:
    gorgon                        Start AI chat session
    gorgon list                   List local VMs
    gorgon launch myvm sdl        Launch VM with SDL display
    gorgon help                   Show all direct commands
    gorgon -tf myvm               Fingerprint report for myvm
"""

import json
import os
import sys

_CLI_CFG_PATH = os.path.join(os.path.dirname(__file__), "CLI_config.json")


def _load_cli_config() -> dict:
    """Load the client CLI config (accent colour, font size), or defaults."""
    try:
        return json.load(open(_CLI_CFG_PATH))
    except Exception:
        return {}


def main() -> None:
    """Client entry point — parse flags and launch the chat client or direct CLI."""
    argv    = sys.argv[1:]
    verbose = "-v" in argv
    if verbose:
        argv = [a for a in argv if a != "-v"]

    custom_mode = "-cu" in argv
    if custom_mode:
        argv = [a for a in argv if a != "-cu"]

    clear_session = "-cs" in argv
    if clear_session:
        argv = [a for a in argv if a != "-cs"]

    if "-tf" in argv:
        idx = argv.index("-tf")
        vm_name = argv[idx + 1] if idx + 1 < len(argv) else None
        argv = argv[:idx] + argv[idx + 2:]
        if not vm_name:
            print("Usage: gorgon -tf <vm-name>")
            sys.exit(1)
        from client.cli.commands import fingerprint_report
        fingerprint_report(vm_name)
        return

    if custom_mode:
        from client.cli.commands import set_custom_mode_flag
        set_custom_mode_flag(True)

    if clear_session:
        from client.cli.commands import clear_session_flag
        clear_session_flag()

    cfg       = _load_cli_config()
    color_hex = cfg.get("text_color", "#aaaaaa")
    font_size = int(cfg.get("font_size", 13))

    if argv:
        from client.cli.commands import run
        run(argv, verbose=verbose)
    else:
        from client.ui.chat_client import chat_loop
        chat_loop(verbose=verbose, color_hex=color_hex, font_size=font_size)


if __name__ == "__main__":
    main()

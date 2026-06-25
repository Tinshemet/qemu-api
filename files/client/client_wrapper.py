"""
client_wrapper.py — qemu-api Client Entry Point

Usage:
    qemu-api                        AI chat (connects to server)
    qemu-api <cmd> [args...]        Direct local QEMU command (no AI)
    qemu-api -v                     AI chat with verbose tool output

Appearance is configured via CLI_config.json (same directory as this file):
    text_color   Hex color for body text, e.g. "#aaaaaa"
    font_size    Terminal font size hint, e.g. 13

Examples:
    qemu-api                        Start AI chat session
    qemu-api list                   List local VMs
    qemu-api launch myvm sdl        Launch VM with SDL display
    qemu-api help                   Show all direct commands
"""

import json
import os
import sys

_CLI_CFG_PATH = os.path.join(os.path.dirname(__file__), "CLI_config.json")


def _load_cli_config() -> dict:
    try:
        return json.load(open(_CLI_CFG_PATH))
    except Exception:
        return {}


def main():
    argv    = sys.argv[1:]
    verbose = "-v" in argv
    if verbose:
        argv = [a for a in argv if a != "-v"]

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

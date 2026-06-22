"""
client_wrapper.py — qemu-api Client Entry Point

Usage:
    qemu-api                  → AI chat (connects to server)
    qemu-api <cmd> [args...]  → Direct local QEMU command (no AI)
    qemu-api -v               → AI chat with verbose tool output
    qemu-api -v <cmd>         → Direct command with verbose output

Examples:
    qemu-api                   Start AI chat session
    qemu-api list              List local VMs
    qemu-api launch myvm sdl   Launch VM with SDL display
    qemu-api help              Show all direct commands
"""

import sys


def main():
    argv    = sys.argv[1:]
    verbose = "-v" in argv
    if verbose:
        argv = [a for a in argv if a != "-v"]

    if argv:
        from client.cli.commands import run
        run(argv, verbose=verbose)
    else:
        from client.ui.chat_client import chat_loop
        chat_loop(verbose=verbose)


if __name__ == "__main__":
    main()

"""
client_wrapper.py — gorgon Client Entry Point

Usage:
    gorgon                        AI chat (connects to server)
    gorgon <cmd> [args...]        Direct local QEMU command (no AI)
    gorgon -v                     AI chat with verbose tool output
    gorgon -cu                    AI chat with product verification disabled
    gorgon -cs                    Clear saved session before starting
    gorgon -tf <name>             Fingerprint report for a VM

Appearance is configured via config/CLI_config.json:
    text_color   Hex color for body text, e.g. "#aaaaaa"
    font_size    Terminal font size hint, e.g. 13

Examples:
    gorgon                        Start AI chat session
    gorgon list                   List local VMs
    gorgon launch myvm sdl        Launch VM with SDL display
    gorgon help                   Show all direct commands
    gorgon -tf myvm               Fingerprint report for myvm
"""

import sys


class ClientCLI:
    """The gorgon client entry point (the ``gorgon`` alias → ``client_wrapper.py``).

    ``run()`` parses the flags — ``-v`` (verbose), ``-cu`` (custom mode), ``-cs``
    (clear session), ``-tf <name>`` (fingerprint report, then stop) — and routes:
    remaining args → the direct local command CLI (``client.cli.commands.run``);
    no args → the chat client UI (``client.ui.chat_client.chat_loop``).
    """

    def run(self, argv=None) -> None:
        """Parse the flags and dispatch. ``argv`` defaults to ``sys.argv[1:]``."""
        argv = list(sys.argv[1:] if argv is None else argv)
        argv, verbose = self._parse_flags(argv)
        if argv is None:          # a flag (-tf) already handled the whole request
            return
        self._dispatch(argv, verbose)

    def _parse_flags(self, argv):
        """Strip -v/-cu/-cs (applying -cu/-cs side effects) and handle -tf. Returns
        (argv, verbose), or (None, verbose) when -tf already handled the request."""
        verbose = "-v" in argv
        argv = [a for a in argv if a != "-v"]
        custom_mode = "-cu" in argv
        argv = [a for a in argv if a != "-cu"]
        clear_session = "-cs" in argv
        argv = [a for a in argv if a != "-cs"]

        if "-tf" in argv:
            self._fingerprint(argv)
            return None, verbose

        if custom_mode:
            from client.cli.commands import set_custom_mode_flag
            set_custom_mode_flag(True)
        if clear_session:
            from client.cli.commands import clear_session_flag
            clear_session_flag()
        return argv, verbose

    def _fingerprint(self, argv) -> None:
        """Handle ``-tf <name>`` — print a VM fingerprint report (then the run stops)."""
        idx = argv.index("-tf")
        vm_name = argv[idx + 1] if idx + 1 < len(argv) else None
        if not vm_name:
            print("Usage: gorgon -tf <vm-name>")
            sys.exit(1)
        from client.cli.commands import fingerprint_report
        fingerprint_report(vm_name)

    def _dispatch(self, argv, verbose) -> None:
        """Route: remaining args → the direct local CLI; none → the chat client UI."""
        from client import config as _cfg
        if argv:
            from client.cli.commands import run
            run(argv, verbose=verbose)
        else:
            from client.ui.chat_client import chat_loop
            skin = self._active_skin({"text_color": _cfg.TEXT_COLOR, "font_size": _cfg.FONT_SIZE})
            chat_loop(verbose=verbose, color_hex=skin["text_color"], font_size=skin["font_size"])

    def _active_skin(self, base: dict) -> dict:
        """The active agent's appearance skin laid over the global defaults (``base``).
        Degrades to ``base`` if the skin/contract layer is unavailable."""
        try:
            from shared.skin import load_skin
            from orchestrator.ai.agent.contract import active_agent_key
            return load_skin(active_agent_key(), base)
        except Exception:
            return base


if __name__ == "__main__":
    ClientCLI().run()

"""
admin_tui.py — Real-time admin TUI for gorgon (entry point).

Fullscreen dashboard: VM table, event feed, command prompt.
Connects to the orchestrator over HTTP — run from any machine that can reach it.
Configure in admin/config/ (connection_config.json + admin_config.json).

Launched via the `gorgon-admin` alias:
    PYTHONPATH=<files> python3 admin/admin_tui.py

This module stays a thin entry point (the alias and docs reference this path);
the implementation lives in the `admin` package:
    config/         — every setting (defaults manifest + user overrides + loader)
    state           — shared mutable UI state
    api_client      — HTTP calls to the orchestrator
    server_control  — local orchestrator process control
    render          — curses colours + dashboard drawing
    commands        — typed-command dispatch
    keyboard        — keypress handling
    app             — the curses main loop
"""

from admin.app import main

if __name__ == "__main__":
    main()

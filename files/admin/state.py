"""
admin/state.py — Shared mutable UI state for the admin TUI.

These are the cross-module globals the original single-file TUI kept at module
scope. They live here so every module reads/writes the SAME objects: import the
module (``from admin import state``) and touch ``state.cmd_buf`` etc. — never
``from admin.state import cmd_buf`` (that would copy the binding and desync the
draw/dispatch/keyboard threads).

``lock`` guards ``cmd_buf`` / ``cmd_msg`` / ``help_mode`` (touched from both the
main curses thread and the dispatch worker thread). ``quit_event`` signals the
main loop to exit.
"""

import threading

cmd_buf   = ""
cmd_msg   = ""
help_mode = False

quit_event = threading.Event()
lock       = threading.Lock()
